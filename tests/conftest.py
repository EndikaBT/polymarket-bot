"""
conftest.py — Fixtures compartidos por todos los tests.

Estrategia de aislamiento:
  - `reset_state`: restaura el dict global `state` antes y después de cada test.
  - `tmp_db`: redirige DB_FILE a un fichero SQLite temporal; crea el esquema limpio.
  - `flask_app`: instancia Flask configurada para testing con la DB temporal.
  - `client`: test client sin sesión iniciada.
  - `auth_client`: test client con sesión autenticada y token CSRF listo.
"""

import secrets
from datetime import datetime

import pytest


# ─── Reset del estado global ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_state():
    """Restaura state y _login_attempts a valores vacíos en cada test."""
    import auth
    import state as st

    _defaults = {
        "credentials":        {"private_key": "", "address": "", "chain_id": 137},
        "client":             None,
        "positions":          [],
        "profit_targets":     {},
        "bot_running":        False,
        "bot_thread":         None,
        "logs":               [],
        "last_update":        None,
        "sold_tokens":        set(),
        "redeemed_tokens":    set(),
        "avg_price_cache":    {},
        "avg_price_overrides": {},
        "fill_seeded":        set(),
        "known_positions":    set(),
        "hidden_tokens":      set(),
        "hidden_positions":   {},
        "_hidden_check_ts":   {},
        "_redeeming":         False,
        "session":            {"profit": 0.0, "won": 0, "lost": 0,
                               "start": datetime.now().isoformat()},
        "copy_profiles":      {},
        "copy_positions":     {},
        "copy_settings":      {"mode": "fixed", "fixed_amount": 1.0,
                               "daily_budget": 20.0, "min_price_filter": 0.0},
        "copy_running":       False,
        "copy_thread":        None,
    }
    st.state.update(_defaults)
    auth._login_attempts.clear()
    yield
    st.state.update(_defaults)
    auth._login_attempts.clear()


# ─── Base de datos temporal ───────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Usa un fichero SQLite temporal en lugar de polymarket.db."""
    import db
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "test.db"))
    db.init_db()
    return str(tmp_path / "test.db")


# ─── Flask app de testing ─────────────────────────────────────────────────────

@pytest.fixture
def flask_app(tmp_db):
    import app as flask_module
    flask_module.app.config.update(
        TESTING=True,
        SECRET_KEY="pytest-secret-key",
        WTF_CSRF_ENABLED=False,
    )
    flask_module.app.secret_key = "pytest-secret-key"
    return flask_module.app


@pytest.fixture
def client(flask_app):
    with flask_app.test_client() as c:
        yield c


@pytest.fixture
def auth_client(flask_app):
    """Test client con sesión autenticada y un CSRF token conocido."""
    csrf = secrets.token_hex(16)
    with flask_app.test_client() as c:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf_token"]    = csrf
        yield c, csrf
