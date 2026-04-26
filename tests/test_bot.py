"""
test_bot.py — Tests del bot de venta y lógica de posiciones.

Las llamadas externas (CLOB, Data API, threads) se mockean para
mantener los tests rápidos y deterministas.
"""

from unittest.mock import MagicMock

import pytest

# ─── sell_position ────────────────────────────────────────────────────────────

class TestSellPosition:
    def test_no_client_returns_error(self):
        from bot import sell_position
        from state import state

        state["client"] = None
        ok, msg = sell_position("token123", 10.0)

        assert ok is False
        assert "no inicializado" in msg.lower()

    def test_cancelled_status_returns_false(self):
        """Un estado 'cancelled' del exchange se traduce en fallo."""
        from bot import sell_position
        from state import state

        mock_resp = {"status": "cancelled"}
        mock_client = MagicMock()
        mock_client.create_market_order.return_value = MagicMock()
        mock_client.post_order.return_value = mock_resp
        state["client"] = mock_client

        ok, msg = sell_position("tok1", 10.0, price=0.50)

        assert ok is False
        assert "cancelada" in msg.lower()

    def test_successful_sell_adds_to_sold_tokens(self):
        from bot import sell_position
        from state import state

        mock_resp = {"status": "matched", "orderId": "abc123"}
        mock_client = MagicMock()
        mock_client.create_market_order.return_value = MagicMock()
        mock_client.post_order.return_value = mock_resp
        state["client"] = mock_client

        ok, _ = sell_position("tok_sell", 10.0, price=0.50)

        assert ok is True
        assert "tok_sell" in state["sold_tokens"]

    def test_floor_override_disables_pct_floor(self):
        """floor_override=0 permite venta a cualquier precio."""
        from bot import sell_position
        from state import state

        captured = {}
        mock_client = MagicMock()
        mock_client.create_market_order.side_effect = (
            lambda args: captured.update({"price": args.price}) or MagicMock()
        )
        mock_client.post_order.return_value = {"status": "matched"}
        state["client"] = mock_client

        sell_position("tok2", 5.0, price=0.50, floor_override=0.0)
        assert captured.get("price") == pytest.approx(0.0)


# ─── enrich_positions ─────────────────────────────────────────────────────────

def _make_raw(token_id, size="100", avg="0.30", cur="0.50", **kwargs):
    """Helper: construye un dict de posición cruda mínimo."""
    return {
        "asset":        token_id,
        "size":         size,
        "avgPrice":     avg,
        "curPrice":     cur,
        "title":        kwargs.get("title", "Test Market"),
        "outcome":      kwargs.get("outcome", "YES"),
        "redeemable":   kwargs.get("redeemable", False),
        "conditionId":  kwargs.get("conditionId", ""),
        "outcomeIndex": kwargs.get("outcomeIndex", 0),
    }


class TestEnrichPositions:
    @pytest.fixture(autouse=True)
    def mock_external(self, monkeypatch):
        """Elimina llamadas HTTP en todos los tests de este bloque.
        _seed_avg_price_from_fill se convierte en no-op para no lanzar
        peticiones reales; ThreadPoolExecutor sigue usando threads reales."""
        monkeypatch.setattr("bot.get_best_bid", lambda tid: 0.50)
        monkeypatch.setattr("bot._seed_avg_price_from_fill", lambda *a, **kw: None)

    def test_override_wins_over_api_price(self, tmp_db):
        from bot import enrich_positions
        from state import state

        state["avg_price_overrides"]["tok_ov"] = 0.35
        result = enrich_positions([_make_raw("tok_ov", avg="0.10")])

        assert len(result) == 1
        assert result[0]["avg_price"]          == pytest.approx(0.35)
        assert result[0]["avg_price_override"] is True

    def test_fill_seeded_blocks_lower_api_value(self, tmp_db):
        """Una posición fill_seeded no baja su avg_price aunque la API devuelva menos."""
        from bot import enrich_positions
        from state import state

        state["fill_seeded"].add("tok_fs")
        state["avg_price_cache"]["tok_fs"] = 0.40   # precio confirmado

        enrich_positions([_make_raw("tok_fs", avg="0.10")])  # API dice 0.10

        assert state["avg_price_cache"]["tok_fs"] == pytest.approx(0.40)  # sin cambios

    def test_fill_seeded_accepts_higher_api_value(self, tmp_db):
        """Una posición fill_seeded sí acepta un avgPrice mayor de la API."""
        from bot import enrich_positions
        from state import state

        state["fill_seeded"].add("tok_up")
        state["avg_price_cache"]["tok_up"] = 0.30

        enrich_positions([_make_raw("tok_up", avg="0.50")])  # API dice 0.50

        assert state["avg_price_cache"]["tok_up"] == pytest.approx(0.50)

    def test_dust_positions_excluded(self, tmp_db):
        from bot import enrich_positions

        result = enrich_positions([_make_raw("tok_dust", size="0.005")])
        assert result == []

    def test_redeemed_token_excluded(self, tmp_db):
        from bot import enrich_positions
        from state import state

        state["redeemed_tokens"].add("tok_red")
        result = enrich_positions([_make_raw("tok_red")])
        assert result == []

    def test_pnl_pct_calculation(self, tmp_db):
        """pnl_pct = (current - avg) / avg * 100."""
        from bot import enrich_positions
        from state import state

        state["fill_seeded"].add("tok_pnl")
        state["avg_price_cache"]["tok_pnl"] = 0.40  # avg price conocido

        result = enrich_positions([_make_raw("tok_pnl", avg="0.40")])
        # get_best_bid mockeado devuelve 0.50
        assert len(result) == 1
        assert result[0]["pnl_pct"] == pytest.approx(25.0)  # (0.50-0.40)/0.40*100


# ─── calculate_bet (copy bot) ─────────────────────────────────────────────────

class TestCalculateBet:
    def test_fixed_mode_returns_fixed_amount(self, tmp_db):
        from copy_bot import calculate_bet
        from state import state

        state["copy_settings"].update(
            {"mode": "fixed", "fixed_amount": 5.0, "daily_budget": 50.0}
        )
        amount, reason = calculate_bet(100.0, "0xabc")

        assert amount == pytest.approx(5.0)
        assert reason is None

    def test_budget_exhausted_returns_skip(self, tmp_db):
        from copy_bot import calculate_bet
        from db import _add_spent
        from state import state

        state["copy_settings"]["daily_budget"] = 10.0
        _add_spent(10.0)

        amount, reason = calculate_bet(100.0, "0xabc")

        assert amount == 0.0
        assert reason is not None
        assert "presupuesto" in reason.lower()

    def test_proportional_mode(self, tmp_db):
        from copy_bot import calculate_bet
        from state import state

        state["copy_settings"].update(
            {"mode": "proportional", "fixed_amount": 1.0, "daily_budget": 100.0}
        )
        state["copy_profiles"]["0xabc"] = {"address": "0xabc", "portfolio_value": 1000.0}

        # Ellos apuestan 100 en un portfolio de 1000 → 10% → 10 de nuestro budget
        amount, reason = calculate_bet(100.0, "0xabc")

        assert amount == pytest.approx(10.0)
        assert reason is None

    def test_calculated_below_minimum_is_skipped(self, tmp_db):
        from copy_bot import calculate_bet
        from state import state

        state["copy_settings"].update(
            {"mode": "fixed", "fixed_amount": 0.50, "daily_budget": 50.0}
        )
        amount, reason = calculate_bet(1.0, "0xabc")

        assert amount == 0.0
        assert reason is not None

    def test_caps_at_remaining_budget(self, tmp_db):
        from copy_bot import calculate_bet
        from db import _add_spent
        from state import state

        state["copy_settings"].update(
            {"mode": "fixed", "fixed_amount": 10.0, "daily_budget": 15.0}
        )
        _add_spent(12.0)   # solo quedan 3

        amount, reason = calculate_bet(100.0, "0xabc")

        assert amount == pytest.approx(3.0)
        assert reason is None
