"""
test_db.py — Tests de la capa de base de datos.

Verifica el cálculo de cost/revenue/profit, el presupuesto diario,
las posiciones ocultas y las consultas de PnL por período.
"""

import pytest


# ─── record_close ─────────────────────────────────────────────────────────────

class TestRecordClose:
    def test_sell_profit_math(self, tmp_db):
        """cost = size*avg, revenue = size*price*(1-fee), profit = revenue-cost."""
        from db import _db_conn, record_close

        record_close("Test Market", "YES", 100.0, 0.30, 0.50, "vendida", "tok1")

        with _db_conn() as conn:
            row = dict(conn.execute(
                "SELECT * FROM closed_positions ORDER BY id DESC LIMIT 1"
            ).fetchone())

        assert row["cost"]    == pytest.approx(30.0)          # 100 * 0.30
        assert row["revenue"] == pytest.approx(49.0)          # 100 * 0.50 * 0.98
        assert row["profit"]  == pytest.approx(19.0)          # 49 - 30

    def test_sell_loss_math(self, tmp_db):
        from db import _db_conn, record_close

        record_close("Losing Market", "NO", 50.0, 0.60, 0.20, "vendida", "tok2")

        with _db_conn() as conn:
            row = dict(conn.execute(
                "SELECT * FROM closed_positions ORDER BY id DESC LIMIT 1"
            ).fetchone())

        assert row["cost"]    == pytest.approx(30.0)          # 50 * 0.60
        assert row["revenue"] == pytest.approx(9.8)           # 50 * 0.20 * 0.98
        assert row["profit"]  == pytest.approx(-20.2)         # 9.8 - 30

    def test_canje_no_taker_fee(self, tmp_db):
        """Las posiciones canjeadas reciben 1 USDC por token sin comisión."""
        from db import _db_conn, record_close

        record_close("Won Market", "YES", 50.0, 0.40, 1.0, "canjeada", "tok3")

        with _db_conn() as conn:
            row = dict(conn.execute(
                "SELECT * FROM closed_positions ORDER BY id DESC LIMIT 1"
            ).fetchone())

        assert row["revenue"] == pytest.approx(50.0)          # sin fee
        assert row["profit"]  == pytest.approx(30.0)          # 50 - 20

    def test_perdida_records_loss(self, tmp_db):
        """Posiciones perdidas se registran con profit negativo."""
        from db import _db_conn, record_close

        record_close("Lost Market", "YES", 20.0, 0.50, 0.0, "perdida", "tok4")

        with _db_conn() as conn:
            row = dict(conn.execute(
                "SELECT * FROM closed_positions ORDER BY id DESC LIMIT 1"
            ).fetchone())

        assert row["profit"] == pytest.approx(-10.0)          # 0 - 10

    def test_updates_session_won(self, tmp_db):
        from db import record_close
        from state import state

        record_close("Win", "YES", 10.0, 0.50, 0.80, "vendida", "tok5")
        # revenue = 10 * 0.80 * 0.98 = 7.84 | cost = 5.0 | profit = 2.84
        assert state["session"]["won"]    == 1
        assert state["session"]["lost"]   == 0
        assert state["session"]["profit"] == pytest.approx(2.84)

    def test_updates_session_lost(self, tmp_db):
        from db import record_close
        from state import state

        record_close("Loss", "NO", 10.0, 0.50, 0.10, "vendida", "tok6")
        assert state["session"]["won"]  == 0
        assert state["session"]["lost"] == 1

    def test_none_avg_price_treated_as_zero(self, tmp_db):
        """avg_price=None no debe lanzar excepción."""
        from db import _db_conn, record_close

        record_close("Market", "YES", 10.0, None, 0.50, "vendida", "tok7")

        with _db_conn() as conn:
            row = dict(conn.execute(
                "SELECT * FROM closed_positions ORDER BY id DESC LIMIT 1"
            ).fetchone())

        assert row["cost"] == pytest.approx(0.0)


# ─── Presupuesto diario ───────────────────────────────────────────────────────

class TestBudget:
    def test_get_spent_today_empty(self, tmp_db):
        from db import get_spent_today
        assert get_spent_today() == pytest.approx(0.0)

    def test_add_spent_accumulates(self, tmp_db):
        from db import _add_spent, get_spent_today
        _add_spent(5.0)
        _add_spent(3.0)
        assert get_spent_today() == pytest.approx(8.0)

    def test_credit_budget_reduces_spent(self, tmp_db):
        from db import _add_spent, credit_budget, get_spent_today
        _add_spent(10.0)
        credit_budget(6.0, 1.0)          # floor(6 * 1) = 6
        assert get_spent_today() == pytest.approx(4.0)

    def test_credit_budget_floors_at_zero(self, tmp_db):
        from db import _add_spent, credit_budget, get_spent_today
        _add_spent(2.0)
        credit_budget(100.0, 1.0)        # no puede pasar de 0
        assert get_spent_today() == pytest.approx(0.0)

    def test_get_remaining_budget(self, tmp_db):
        from db import _add_spent, get_remaining_budget
        from state import state
        state["copy_settings"]["daily_budget"] = 20.0
        _add_spent(7.0)
        assert get_remaining_budget() == pytest.approx(13.0)

    def test_credit_budget_returns_amount(self, tmp_db):
        from db import _add_spent, credit_budget
        _add_spent(10.0)
        recovered = credit_budget(5.5, 1.0)    # floor(5.5) = 5
        assert recovered == pytest.approx(5.0)


# ─── Posiciones ocultas ───────────────────────────────────────────────────────

class TestHiddenPositions:
    def test_upsert_hidden_updates_state(self, tmp_db):
        from db import _upsert_hidden
        from state import state

        meta = {"title": "Test", "outcome": "YES", "size": 10.0,
                "avg_price": 0.5, "cost": 5.0, "reason": "perdida"}
        _upsert_hidden("tok1", meta)

        assert "tok1" in state["hidden_tokens"]
        assert state["hidden_positions"]["tok1"]["title"] == "Test"

    def test_delete_hidden_cleans_state(self, tmp_db):
        from db import _delete_hidden, _upsert_hidden
        from state import state

        meta = {"title": "Test", "outcome": "YES", "size": 10.0,
                "avg_price": 0.5, "cost": 5.0, "reason": "perdida"}
        _upsert_hidden("tok1", meta)
        _delete_hidden("tok1")

        assert "tok1" not in state["hidden_tokens"]
        assert "tok1" not in state["hidden_positions"]

    def test_purge_settled_losses(self, tmp_db):
        """Elimina posiciones ocultas que ya no están activas en la Data API."""
        from db import _upsert_hidden, purge_settled_losses
        from state import state

        meta = {"title": "Old", "outcome": "NO", "size": 5.0,
                "avg_price": 0.3, "cost": 1.5, "reason": "perdida"}
        _upsert_hidden("settled_tok", meta)
        assert "settled_tok" in state["hidden_tokens"]

        # El token no está en active_ids → debe eliminarse
        purge_settled_losses(active_token_ids=set())
        assert "settled_tok" not in state["hidden_tokens"]


# ─── PnL por período ──────────────────────────────────────────────────────────

class TestPnlForPeriod:
    def test_empty_db_returns_zeros(self, tmp_db):
        from db import _pnl_for_period
        result = _pnl_for_period("")
        assert result == {"profit": 0.0, "won": 0, "lost": 0}

    def test_aggregates_multiple_closes(self, tmp_db):
        from db import _pnl_for_period, record_close
        record_close("Win1", "YES", 10.0, 0.40, 0.80, "vendida", "t1")
        record_close("Win2", "YES", 10.0, 0.40, 0.80, "vendida", "t2")
        record_close("Loss", "NO",  10.0, 0.40, 0.00, "perdida", "t3")
        result = _pnl_for_period("")
        assert result["won"]  == 2
        assert result["lost"] == 1
        assert result["profit"] != 0.0
