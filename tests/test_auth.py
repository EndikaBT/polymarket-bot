"""
test_auth.py — Tests de autenticación, CSRF y rate limiting.
"""

import pytest
from werkzeug.security import generate_password_hash


# ─── CSRF helpers ─────────────────────────────────────────────────────────────

class TestCsrfHelpers:
    def test_get_csrf_token_creates_token(self, flask_app):
        with flask_app.test_request_context("/"):
            from flask import session
            from auth import _get_csrf_token

            token = _get_csrf_token()
            assert isinstance(token, str)
            assert len(token) == 64          # secrets.token_hex(32) → 64 hex chars
            assert session["csrf_token"] == token

    def test_get_csrf_token_is_idempotent(self, flask_app):
        with flask_app.test_request_context("/"):
            from auth import _get_csrf_token

            t1 = _get_csrf_token()
            t2 = _get_csrf_token()
            assert t1 == t2

    def test_verify_csrf_valid_header(self, flask_app):
        with flask_app.test_request_context(
            "/api/test", method="POST",
            headers={"X-CSRF-Token": "mytoken"},
        ):
            from flask import session
            from auth import _verify_csrf

            session["csrf_token"] = "mytoken"
            assert _verify_csrf() is True

    def test_verify_csrf_wrong_token(self, flask_app):
        with flask_app.test_request_context(
            "/api/test", method="POST",
            headers={"X-CSRF-Token": "wrong"},
        ):
            from flask import session
            from auth import _verify_csrf

            session["csrf_token"] = "correct"
            assert _verify_csrf() is False

    def test_verify_csrf_missing_session_token(self, flask_app):
        with flask_app.test_request_context(
            "/api/test", method="POST",
            headers={"X-CSRF-Token": "anything"},
        ):
            from auth import _verify_csrf
            assert _verify_csrf() is False


# ─── Rate limiting ────────────────────────────────────────────────────────────

class TestRateLimiting:
    def _setup_password(self, db_file):
        import sqlite3
        h = generate_password_hash("correct_password")
        with sqlite3.connect(db_file) as conn:
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('password_hash', ?)", (h,))

    def test_failed_attempt_is_recorded(self, client, tmp_db):
        import auth
        self._setup_password(tmp_db)

        client.post("/login", data={"password": "wrong"})
        assert len(auth._login_attempts.get("127.0.0.1", [])) == 1

    def test_lockout_after_max_attempts(self, client, tmp_db):
        import auth
        from auth import MAX_ATTEMPTS
        self._setup_password(tmp_db)

        for _ in range(MAX_ATTEMPTS):
            client.post("/login", data={"password": "wrong"})

        allowed, wait = auth._rate_limit_check("127.0.0.1")
        assert allowed is False
        assert wait > 0

    def test_successful_login_clears_attempts(self, client, tmp_db):
        import auth
        self._setup_password(tmp_db)

        # Falla primero
        client.post("/login", data={"password": "wrong"})
        assert len(auth._login_attempts.get("127.0.0.1", [])) == 1

        # Login correcto limpia el contador
        client.post("/login", data={"password": "correct_password"})
        assert "127.0.0.1" not in auth._login_attempts

    def test_get_remaining_attempts(self):
        import auth
        auth._login_attempts["1.2.3.4"] = [1.0, 2.0]   # 2 intentos fallidos
        remaining = auth.get_remaining_attempts("1.2.3.4")
        assert remaining == auth.MAX_ATTEMPTS - 2


# ─── Login / Logout ───────────────────────────────────────────────────────────

class TestLoginLogout:
    def _setup_password(self, db_file, password="testpass123"):
        import sqlite3
        h = generate_password_hash(password)
        with sqlite3.connect(db_file) as conn:
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('password_hash', ?)", (h,))

    def test_login_page_loads(self, client, tmp_db):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_correct_password_creates_session(self, client, tmp_db):
        self._setup_password(tmp_db)
        resp = client.post("/login", data={"password": "testpass123"},
                           follow_redirects=False)
        assert resp.status_code == 302   # redirect al dashboard
        with client.session_transaction() as sess:
            assert sess.get("authenticated") is True

    def test_wrong_password_no_session(self, client, tmp_db):
        self._setup_password(tmp_db)
        client.post("/login", data={"password": "wrongpassword"})
        with client.session_transaction() as sess:
            assert not sess.get("authenticated")

    def test_logout_clears_session(self, auth_client, flask_app):
        c, csrf = auth_client
        c.post("/logout", headers={"X-CSRF-Token": csrf})
        with c.session_transaction() as sess:
            assert not sess.get("authenticated")

    def test_first_run_setup_requires_matching_passwords(self, client, tmp_db):
        """Sin password en DB, el formulario pide confirmación."""
        resp = client.post("/login", data={
            "password":  "newpass123",
            "password2": "different456",
        })
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert not sess.get("authenticated")

    def test_first_run_setup_success(self, client, tmp_db):
        resp = client.post("/login", data={
            "password":  "newpass123",
            "password2": "newpass123",
        }, follow_redirects=False)
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("authenticated") is True


# ─── change-password ──────────────────────────────────────────────────────────

class TestChangePassword:
    def test_change_password_success(self, auth_client, tmp_db):
        import sqlite3
        c, csrf = auth_client

        h = generate_password_hash("oldpass123")
        with sqlite3.connect(tmp_db) as conn:
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('password_hash', ?)", (h,))

        resp = c.post("/api/auth/change-password",
                      json={"current": "oldpass123", "new": "newpass456", "confirm": "newpass456"},
                      headers={"X-CSRF-Token": csrf})

        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_change_password_wrong_current(self, auth_client, tmp_db):
        import sqlite3
        c, csrf = auth_client

        h = generate_password_hash("realpass")
        with sqlite3.connect(tmp_db) as conn:
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('password_hash', ?)", (h,))

        resp = c.post("/api/auth/change-password",
                      json={"current": "wrongpass", "new": "newpass456", "confirm": "newpass456"},
                      headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 403

    def test_change_password_too_short(self, auth_client):
        c, csrf = auth_client
        resp = c.post("/api/auth/change-password",
                      json={"current": "x", "new": "short", "confirm": "short"},
                      headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 400

    def test_change_password_mismatch(self, auth_client):
        c, csrf = auth_client
        resp = c.post("/api/auth/change-password",
                      json={"current": "x", "new": "newpass456", "confirm": "different"},
                      headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 400
