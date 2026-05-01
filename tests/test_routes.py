"""
test_routes.py — Tests de los endpoints Flask.

Verifica autenticación, CSRF, y que los endpoints devuelven
las estructuras y códigos HTTP correctos.
"""

import json

import pytest

# ─── Autenticación ────────────────────────────────────────────────────────────

class TestAuthGating:
    def test_api_returns_401_without_session(self, client):
        for endpoint in ["/api/positions", "/api/bot/status", "/api/stats",
                         "/api/copy/status", "/api/config"]:
            resp = client.get(endpoint)
            assert resp.status_code == 401, f"{endpoint} debería devolver 401"

    def test_index_redirects_to_login(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_authenticated_can_reach_index(self, auth_client):
        c, _ = auth_client
        resp = c.get("/")
        assert resp.status_code == 200


# ─── CSRF ─────────────────────────────────────────────────────────────────────

class TestCsrf:
    def test_post_without_csrf_returns_403(self, auth_client):
        c, _ = auth_client
        resp = c.post("/api/target",
                      data=json.dumps({"token_id": "tok1", "target_pct": 20.0}),
                      content_type="application/json")
        assert resp.status_code == 403

    def test_post_with_wrong_csrf_returns_403(self, auth_client):
        c, _ = auth_client
        resp = c.post("/api/target",
                      data=json.dumps({"token_id": "tok1", "target_pct": 20.0}),
                      content_type="application/json",
                      headers={"X-CSRF-Token": "wrong-token"})
        assert resp.status_code == 403

    def test_post_with_valid_csrf_succeeds(self, auth_client):
        c, csrf = auth_client
        resp = c.post("/api/target",
                      data=json.dumps({"token_id": "tok1", "target_pct": 20.0}),
                      content_type="application/json",
                      headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


# ─── /api/bot/status ─────────────────────────────────────────────────────────

class TestBotStatus:
    def test_returns_expected_fields(self, auth_client):
        c, _ = auth_client
        resp = c.get("/api/bot/status")
        data = resp.get_json()

        assert resp.status_code == 200
        assert "running"      in data
        assert "client_ready" in data
        assert "logs"         in data
        assert data["running"] is False


# ─── /api/config ─────────────────────────────────────────────────────────────

class TestConfig:
    def test_get_masks_private_key(self, auth_client):
        from state import state
        c, _ = auth_client
        state["credentials"]["private_key"] = "real_secret_key"

        resp = c.get("/api/config")
        data = resp.get_json()

        assert resp.status_code == 200
        assert data["credentials"]["private_key"] == "••••••••"
        assert data["has_private_key"] is True

    def test_get_no_key_shows_empty(self, auth_client):
        c, _ = auth_client
        resp = c.get("/api/config")
        data = resp.get_json()

        assert data["has_private_key"] is False


# ─── /api/target ─────────────────────────────────────────────────────────────

class TestTarget:
    def test_set_target(self, auth_client):
        from state import state
        c, csrf = auth_client

        resp = c.post("/api/target",
                      json={"token_id": "tok1", "target_pct": 25.0},
                      headers={"X-CSRF-Token": csrf})

        assert resp.status_code == 200
        assert state["profit_targets"]["tok1"] == pytest.approx(25.0)

    def test_clear_target(self, auth_client):
        from state import state
        c, csrf = auth_client
        state["profit_targets"]["tok1"] = 25.0

        resp = c.post("/api/target",
                      json={"token_id": "tok1", "target_pct": None},
                      headers={"X-CSRF-Token": csrf})

        assert resp.status_code == 200
        assert "tok1" not in state["profit_targets"]

    def test_missing_token_id_returns_error(self, auth_client):
        c, csrf = auth_client
        resp = c.post("/api/target",
                      json={"target_pct": 20.0},
                      headers={"X-CSRF-Token": csrf})

        assert resp.get_json()["ok"] is False


# ─── /api/avg-price ───────────────────────────────────────────────────────────

class TestAvgPrice:
    def test_set_override(self, auth_client):
        from state import state
        c, csrf = auth_client

        resp = c.post("/api/avg-price",
                      json={"token_id": "tok1", "avg_price": 0.42},
                      headers={"X-CSRF-Token": csrf})

        assert resp.status_code == 200
        assert state["avg_price_overrides"]["tok1"] == pytest.approx(0.42)
        assert state["avg_price_cache"]["tok1"]     == pytest.approx(0.42)

    def test_clear_override(self, auth_client):
        from state import state
        c, csrf = auth_client
        state["avg_price_overrides"]["tok1"] = 0.42

        resp = c.post("/api/avg-price",
                      json={"token_id": "tok1", "avg_price": 0},
                      headers={"X-CSRF-Token": csrf})

        assert resp.status_code == 200
        assert "tok1" not in state["avg_price_overrides"]

    def test_missing_token_id_returns_error(self, auth_client):
        c, csrf = auth_client
        resp = c.post("/api/avg-price",
                      json={"avg_price": 0.42},
                      headers={"X-CSRF-Token": csrf})
        assert resp.get_json()["ok"] is False


# ─── /api/hide ────────────────────────────────────────────────────────────────

class TestHide:
    def test_hide_position(self, auth_client):
        from state import state
        c, csrf = auth_client

        resp = c.post("/api/hide",
                      json={"token_id": "tok1", "hide": True,
                            "title": "Test", "outcome": "YES",
                            "size": 10.0, "avg_price": 0.50},
                      headers={"X-CSRF-Token": csrf})

        assert resp.status_code == 200
        assert "tok1" in state["hidden_tokens"]

    def test_unhide_position(self, auth_client):
        from db import _upsert_hidden
        from state import state
        c, csrf = auth_client

        meta = {"title": "T", "outcome": "Y", "size": 5.0,
                "avg_price": 0.3, "cost": 1.5, "reason": "manual"}
        _upsert_hidden("tok1", meta)

        resp = c.post("/api/hide",
                      json={"token_id": "tok1", "hide": False},
                      headers={"X-CSRF-Token": csrf})

        assert resp.status_code == 200
        assert "tok1" not in state["hidden_tokens"]


# ─── /api/stats ───────────────────────────────────────────────────────────────

class TestStats:
    def test_returns_expected_structure(self, auth_client, monkeypatch):
        monkeypatch.setattr("app._get_cached_balance",
                            lambda: {"pusd": 80.0, "usdce": 20.0, "total": 100.0})
        c, _ = auth_client

        resp = c.get("/api/stats")
        data = resp.get_json()

        assert resp.status_code == 200
        for key in ("balance", "balance_pusd", "balance_usdce",
                    "open_value", "open_cost", "total",
                    "daily", "weekly", "monthly", "all_time"):
            assert key in data, f"Falta campo: {key}"

    def test_balance_comes_from_wallet(self, auth_client, monkeypatch):
        monkeypatch.setattr("app._get_cached_balance",
                            lambda: {"pusd": 42.5, "usdce": 0.0, "total": 42.5})
        c, _ = auth_client

        resp = c.get("/api/stats")
        assert resp.get_json()["balance"] == pytest.approx(42.5)

    def test_balance_breakdown_in_response(self, auth_client, monkeypatch):
        monkeypatch.setattr("app._get_cached_balance",
                            lambda: {"pusd": 29.59, "usdce": 2.50, "total": 32.09})
        c, _ = auth_client

        resp = c.get("/api/stats")
        data = resp.get_json()
        assert data["balance"]       == pytest.approx(32.09)
        assert data["balance_pusd"]  == pytest.approx(29.59)
        assert data["balance_usdce"] == pytest.approx(2.50)


# ─── /api/sell ────────────────────────────────────────────────────────────────

class TestSell:
    def test_missing_params_returns_error(self, auth_client):
        c, csrf = auth_client
        resp = c.post("/api/sell",
                      json={"token_id": "tok1"},   # falta size
                      headers={"X-CSRF-Token": csrf})
        assert resp.get_json()["ok"] is False

    def test_slippage_warning(self, auth_client, monkeypatch):
        """Devuelve slippage=True si el precio actual cayó > 8% respecto al de la UI."""
        monkeypatch.setattr("app.get_best_bid", lambda tid: 0.50)
        c, csrf = auth_client

        # UI price = 0.60, fresh = 0.50 → caída del 16.7%
        resp = c.post("/api/sell",
                      json={"token_id": "tok1", "size": 10.0, "price": 0.60},
                      headers={"X-CSRF-Token": csrf})
        data = resp.get_json()

        assert data["ok"]       is False
        assert data["slippage"] is True
        assert "fresh_price"    in data


# ─── /api/copy/settings ───────────────────────────────────────────────────────

class TestCopySettings:
    def test_get_returns_settings(self, auth_client):
        c, _ = auth_client
        resp = c.get("/api/copy/settings")
        data = resp.get_json()

        assert resp.status_code == 200
        for key in ("mode", "fixed_amount", "daily_budget", "spent_today", "remaining"):
            assert key in data

    def test_put_updates_mode(self, auth_client):
        from state import state
        c, csrf = auth_client

        resp = c.put("/api/copy/settings",
                     json={"mode": "proportional"},
                     headers={"X-CSRF-Token": csrf})

        assert resp.status_code == 200
        assert state["copy_settings"]["mode"] == "proportional"

    def test_put_rejects_invalid_mode(self, auth_client):
        c, csrf = auth_client
        resp = c.put("/api/copy/settings",
                     json={"mode": "invalid"},
                     headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 400
