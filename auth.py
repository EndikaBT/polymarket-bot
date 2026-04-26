"""
auth.py — Autenticación, CSRF y rate limiting.

Exporta funciones puras que app.py registra en la instancia Flask
(before_request, after_request, context_processor) y helpers usados
en las rutas de login/logout.
"""

import hmac
import secrets
import threading
import time

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

from db import _db_conn, _db_lock

# ─── Rate limiting ────────────────────────────────────────────────────────────

_login_lock     = threading.Lock()
_login_attempts: dict = {}   # ip -> [timestamp, …]
MAX_ATTEMPTS    = 5
LOCKOUT_SECONDS = 15 * 60   # 15 min


def _rate_limit_check(ip: str) -> tuple:
    """Devuelve (allowed: bool, wait_seconds: int)."""
    now = time.time()
    with _login_lock:
        ts = [t for t in _login_attempts.get(ip, []) if now - t < LOCKOUT_SECONDS]
        _login_attempts[ip] = ts
        if len(ts) >= MAX_ATTEMPTS:
            return False, int(LOCKOUT_SECONDS - (now - ts[0]))
        return True, 0


def _record_failed(ip: str):
    with _login_lock:
        _login_attempts.setdefault(ip, []).append(time.time())


def _clear_attempts(ip: str):
    with _login_lock:
        _login_attempts.pop(ip, None)


def get_remaining_attempts(ip: str) -> int:
    return MAX_ATTEMPTS - len(_login_attempts.get(ip, []))


# ─── CSRF ─────────────────────────────────────────────────────────────────────

def _get_csrf_token() -> str:
    """Devuelve (y crea si no existe) el token CSRF de la sesión actual."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def _verify_csrf() -> bool:
    """Comprueba el token CSRF en peticiones que modifican estado."""
    token = session.get("csrf_token")
    if not token:
        return False
    candidate = request.headers.get("X-CSRF-Token") or request.form.get("_csrf", "")
    return hmac.compare_digest(token, candidate)


# ─── Clave secreta Flask ──────────────────────────────────────────────────────

def _get_or_create_secret_key() -> str:
    """Carga la clave secreta desde DB o genera y persiste una nueva en el primer arranque."""
    with _db_conn() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key='secret_key'").fetchone()
    if row:
        return row["value"]
    key = secrets.token_hex(32)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO kv VALUES ('secret_key', ?)", (key,))
    return key


# ─── Hooks Flask ─────────────────────────────────────────────────────────────

def check_auth():
    """`app.before_request`: redirige peticiones no autenticadas a /login."""
    # Endpoints públicos — sin autenticación ni CSRF
    if request.endpoint in ("login", "static", "api_health"):
        _get_csrf_token()   # siembra el token para que el formulario pueda incrustarlo
        return

    if request.endpoint == "logout":
        if not _verify_csrf():
            return redirect(url_for("login"))
        return

    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "No autenticado"}), 401
        return redirect(url_for("login"))

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not _verify_csrf():
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "CSRF inválido"}), 403
            return redirect(url_for("login"))


def security_headers(response):
    """`app.after_request`: añade cabeceras de seguridad a todas las respuestas."""
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def inject_csrf():
    """`app.context_processor`: inyecta csrf_token en todos los templates."""
    return {"csrf_token": _get_csrf_token}
