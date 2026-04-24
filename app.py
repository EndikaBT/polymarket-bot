import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from functools import wraps

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

# ─── Cookie / session security ────────────────────────────────────────────────
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# ─── Auth helpers ─────────────────────────────────────────────────────────────

_login_lock     = threading.Lock()
_login_attempts: dict = {}   # ip -> [timestamp, ...]
MAX_ATTEMPTS    = 5
LOCKOUT_SECONDS = 15 * 60   # 15 min


def _get_or_create_secret_key() -> str:
    """Load secret key from DB, or generate + store one on first run."""
    with _db_conn() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key='secret_key'").fetchone()
    if row:
        return row["value"]
    key = secrets.token_hex(32)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO kv VALUES ('secret_key', ?)", (key,))
    return key


def _rate_limit_check(ip: str) -> tuple:
    """Returns (allowed: bool, wait_seconds: int)."""
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


def _get_csrf_token() -> str:
    """Return (and lazily create) the per-session CSRF token."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def _verify_csrf() -> bool:
    """Check CSRF token for state-changing requests."""
    token = session.get("csrf_token")
    if not token:
        return False
    # Accept from header (AJAX) or form field
    candidate = request.headers.get("X-CSRF-Token") or request.form.get("_csrf", "")
    return hmac.compare_digest(token, candidate)


@app.context_processor
def inject_csrf():
    return {"csrf_token": _get_csrf_token}


@app.before_request
def check_auth():
    """Redirect unauthenticated requests to /login. API routes get 401.
    Also enforces CSRF on all authenticated state-changing requests."""
    # /login is the only truly public endpoint (no auth, no CSRF needed)
    # /static serves files (Flask built-in, harmless)
    # /logout requires CSRF verification even if session happens to be gone
    if request.endpoint in ("login", "static"):
        _get_csrf_token()   # seed token so login form can embed it
        return

    # Logout: verify CSRF but don't require an active session
    # (worst case an unauthenticated POST just clears an empty session)
    if request.endpoint == "logout":
        if not _verify_csrf():
            return redirect(url_for("login"))
        return

    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "No autenticado"}), 401
        return redirect(url_for("login"))

    # CSRF check for every non-GET authenticated request
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not _verify_csrf():
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "CSRF inválido"}), 403
            return redirect(url_for("login"))


@app.after_request
def security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ─── Paths ────────────────────────────────────────────────────────────────────

DB_FILE     = os.path.join(os.path.dirname(__file__), "polymarket.db")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
BACKUP_FILE = os.path.join(os.path.dirname(__file__), "config.json.bak")

# ─── Estado global ───────────────────────────────────────────────────────────

state = {
    "credentials": {
        "private_key": "",
        "address": "",
        "chain_id": 137,
    },
    "client": None,
    "positions": [],
    "profit_targets": {},
    "bot_running": False,
    "bot_thread": None,
    "logs": [],
    "last_update": None,
    "sold_tokens": set(),
    "redeemed_tokens": set(),
    "avg_price_cache":     {},  # token_id -> float: best known avgPrice, updated by Data API / fill seeder
    "avg_price_overrides": {},  # token_id -> float: manually set by user — takes priority over everything
    "fill_seeded":         set(),  # token_ids whose avg_price was confirmed from real fill history
    "known_positions":     set(),  # token_ids seen at least once — used to detect new arrivals
    "hidden_tokens": set(),
    "hidden_positions": {},  # token_id -> {title, outcome, size, avg_price, cost, reason}
    "_hidden_check_ts": {},  # token_id -> float: last time recovery was checked
    # closed_positions and copy_trades removed — live in DB
    "session": {            # resets on every server restart
        "profit": 0.0,
        "won": 0,
        "lost": 0,
        "start": datetime.now().isoformat(),
    },
    # ── Copy trading ──────────────────────────────────────────────────────────
    "copy_profiles": {},   # address -> profile dict
    "copy_positions": {},  # token_id -> {size, market, profile, bought_at}
    "copy_settings": {
        # spent_today / budget_date removed — live in daily_budget table
        "mode": "fixed",
        "fixed_amount": 1.0,
        "daily_budget": 20.0,
        "min_price_filter": 0.0,   # skip buys where current price < this (0 = off)
    },
    "copy_running": False,
    "copy_thread": None,
}

CLOB_HOST = "https://clob.polymarket.com"
DATA_HOST = "https://data-api.polymarket.com"

# ─── SQLite helpers ───────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def _db_conn():
    """Open a short-lived WAL connection. Each call gets its own connection."""
    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """Create all tables if they don't exist yet."""
    with _db_lock:
        with _db_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS kv (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_budget (
                    date  TEXT PRIMARY KEY,
                    spent REAL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS closed_positions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TEXT,
                    title       TEXT,
                    outcome     TEXT,
                    size        REAL,
                    avg_price   REAL,
                    close_price REAL,
                    cost        REAL,
                    revenue     REAL,
                    profit      REAL,
                    type        TEXT,
                    token_id    TEXT,
                    price_verified INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS copy_trades_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         TEXT,
                    profile    TEXT,
                    market     TEXT,
                    side       TEXT,
                    their_usdc REAL,
                    our_amount REAL,
                    status     TEXT,
                    reason     TEXT
                );

                CREATE TABLE IF NOT EXISTS hidden_positions (
                    token_id  TEXT PRIMARY KEY,
                    title     TEXT,
                    outcome   TEXT,
                    size      REAL,
                    avg_price REAL,
                    cost      REAL,
                    reason    TEXT
                );

                CREATE TABLE IF NOT EXISTS copy_positions (
                    token_id  TEXT PRIMARY KEY,
                    size      REAL,
                    market    TEXT,
                    profile   TEXT,
                    bought_at REAL
                );

                CREATE TABLE IF NOT EXISTS redeemed_tokens (
                    token_id TEXT PRIMARY KEY
                );
            """)
        # Add columns to existing DBs that predate this schema version
        for col, definition in [
            ("token_id",       "TEXT"),
            ("price_verified", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE closed_positions ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists


# ─── DB write helpers (always use _db_lock) ───────────────────────────────────

def _save_settings():
    """Persist credentials, profit_targets, copy_settings (no spent/date), copy_profiles to kv."""
    cs = state["copy_settings"]
    settings_to_save = {k: cs[k] for k in ("mode", "fixed_amount", "daily_budget") if k in cs}
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('credentials', ?)",
                         (json.dumps(state["credentials"]),))
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('profit_targets', ?)",
                         (json.dumps(state["profit_targets"]),))
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('copy_settings', ?)",
                         (json.dumps(settings_to_save),))
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('copy_profiles', ?)",
                         (json.dumps(list(state["copy_profiles"].values())),))
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('avg_price_overrides', ?)",
                         (json.dumps(state["avg_price_overrides"]),))


# Backward-compatible alias — ~15 call sites use save_config() unchanged
save_config = _save_settings


def _upsert_hidden(token_id: str, meta: dict):
    """Insert or replace a row in hidden_positions and update in-memory caches."""
    state["hidden_tokens"].add(token_id)
    state["hidden_positions"][token_id] = meta
    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO hidden_positions "
                "(token_id, title, outcome, size, avg_price, cost, reason) "
                "VALUES (?,?,?,?,?,?,?)",
                (token_id, meta.get("title", ""), meta.get("outcome", ""),
                 meta.get("size", 0), meta.get("avg_price", 0),
                 meta.get("cost", 0), meta.get("reason", ""))
            )


def _delete_hidden(token_id: str):
    """Remove a token from hidden_positions and update in-memory caches."""
    state["hidden_tokens"].discard(token_id)
    state["hidden_positions"].pop(token_id, None)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("DELETE FROM hidden_positions WHERE token_id=?", (token_id,))


def _upsert_copy_position(token_id: str, data: dict):
    """Insert or replace a copy_positions row and update in-memory cache."""
    state["copy_positions"][token_id] = data
    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO copy_positions "
                "(token_id, size, market, profile, bought_at) VALUES (?,?,?,?,?)",
                (token_id, data.get("size", 0), data.get("market", ""),
                 data.get("profile", ""), data.get("bought_at", 0))
            )


def _delete_copy_position(token_id: str):
    """Remove a copy_positions row and update in-memory cache."""
    state["copy_positions"].pop(token_id, None)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("DELETE FROM copy_positions WHERE token_id=?", (token_id,))


def _add_redeemed(token_id: str):
    """Mark token as redeemed in DB and in-memory set."""
    state["redeemed_tokens"].add(token_id)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO redeemed_tokens (token_id) VALUES (?)", (token_id,))


def _add_spent(amount_usdc: float):
    """UPSERT today's budget row and add amount to spent."""
    today = date.today().isoformat()
    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO daily_budget (date, spent) VALUES (?, ?) "
                "ON CONFLICT(date) DO UPDATE SET spent = spent + excluded.spent",
                (today, amount_usdc)
            )


def _credit_spent(amount_usdc: float):
    """Decrement today's spent by amount_usdc (floor at 0)."""
    today = date.today().isoformat()
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO daily_budget (date, spent) VALUES (?, 0.0)", (today,))
            conn.execute(
                "UPDATE daily_budget SET spent = MAX(0.0, spent - ?) WHERE date=?",
                (amount_usdc, today)
            )


def _insert_copy_trade(record: dict):
    """Append a copy-trade log entry to copy_trades_log."""
    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO copy_trades_log "
                "(ts, profile, market, side, their_usdc, our_amount, status, reason) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (record.get("ts", ""), record.get("profile", ""),
                 record.get("market", ""), record.get("side", ""),
                 record.get("their_usdc", 0.0), record.get("our_amount", 0.0),
                 record.get("status", ""), record.get("reason", ""))
            )


# ─── Migration from config.json ───────────────────────────────────────────────

def migrate_from_json():
    """One-time migration. Only runs if config.json exists and kv table is empty."""
    if not os.path.exists(CONFIG_FILE):
        return
    with _db_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0] > 0:
            return  # already migrated
    print("[migrate] Migrando config.json → polymarket.db …")
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except Exception as e:
        print(f"[migrate] Error leyendo config.json: {e}")
        return

    with _db_lock:
        with _db_conn() as conn:
            # kv: credentials
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('credentials', ?)",
                         (json.dumps(data.get("credentials", {})),))
            # kv: profit_targets
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('profit_targets', ?)",
                         (json.dumps(data.get("profit_targets", {})),))
            # kv: copy_settings (drop spent_today/budget_date)
            cs = data.get("copy_settings", {})
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('copy_settings', ?)",
                         (json.dumps({k: cs[k] for k in ("mode", "fixed_amount", "daily_budget") if k in cs}),))
            # kv: copy_profiles
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('copy_profiles', ?)",
                         (json.dumps(data.get("copy_profiles", [])),))
            # daily_budget: migrate spent_today if it was today
            today = date.today().isoformat()
            old_date  = cs.get("budget_date", "")
            old_spent = float(cs.get("spent_today", 0.0))
            if old_date == today and old_spent > 0:
                conn.execute("INSERT OR REPLACE INTO daily_budget (date, spent) VALUES (?, ?)",
                             (today, old_spent))
            # hidden_positions
            for tid, meta in data.get("hidden_positions", {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO hidden_positions "
                    "(token_id, title, outcome, size, avg_price, cost, reason) VALUES (?,?,?,?,?,?,?)",
                    (tid, meta.get("title", ""), meta.get("outcome", ""),
                     meta.get("size", 0), meta.get("avg_price", 0),
                     meta.get("cost", 0), meta.get("reason", ""))
                )
            # hidden_tokens not already in hidden_positions
            for tid in data.get("hidden_tokens", []):
                conn.execute(
                    "INSERT OR IGNORE INTO hidden_positions "
                    "(token_id, title, outcome, size, avg_price, cost, reason) VALUES (?,?,?,?,?,?,?)",
                    (tid, "", "", 0, 0, 0, "?")
                )
            # copy_positions
            for tid, cp in data.get("copy_positions", {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO copy_positions "
                    "(token_id, size, market, profile, bought_at) VALUES (?,?,?,?,?)",
                    (tid, cp.get("size", 0), cp.get("market", ""),
                     cp.get("profile", ""), cp.get("bought_at", 0))
                )
            # redeemed_tokens
            for tid in data.get("redeemed_tokens", []):
                conn.execute("INSERT OR IGNORE INTO redeemed_tokens (token_id) VALUES (?)", (tid,))
            # closed_positions — reversed so oldest row gets lowest id
            for p in reversed(data.get("closed_positions", [])):
                conn.execute(
                    "INSERT INTO closed_positions "
                    "(ts, title, outcome, size, avg_price, close_price, cost, revenue, profit, type) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (p.get("ts", ""), p.get("title", ""), p.get("outcome", ""),
                     p.get("size", 0), p.get("avg_price", 0), p.get("close_price", 0),
                     p.get("cost", 0), p.get("revenue", 0), p.get("profit", 0), p.get("type", ""))
                )

    os.rename(CONFIG_FILE, BACKUP_FILE)
    print(f"[migrate] Migración completa. config.json renombrado a config.json.bak")


# ─── Load from DB (replaces load_config) ─────────────────────────────────────

def load_from_db():
    """Populate in-memory state from SQLite at startup."""
    try:
        with _db_conn() as conn:
            def kv_get(key, default):
                row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                return json.loads(row["value"]) if row else default

            state["credentials"].update(kv_get("credentials", {}))
            state["profit_targets"] = kv_get("profit_targets", {})
            state["avg_price_overrides"] = kv_get("avg_price_overrides", {})
            cs = kv_get("copy_settings", {})
            state["copy_settings"].update(cs)
            for p in kv_get("copy_profiles", []):
                state["copy_profiles"][p["address"]] = p

            # hidden_positions → populate both dict and set
            for row in conn.execute("SELECT * FROM hidden_positions").fetchall():
                r = dict(row)
                tid = r.pop("token_id")
                state["hidden_positions"][tid] = r
                state["hidden_tokens"].add(tid)

            # copy_positions
            for row in conn.execute("SELECT * FROM copy_positions").fetchall():
                r = dict(row)
                tid = r.pop("token_id")
                state["copy_positions"][tid] = r

            # redeemed_tokens
            for row in conn.execute("SELECT token_id FROM redeemed_tokens").fetchall():
                state["redeemed_tokens"].add(row["token_id"])

    except Exception as e:
        print(f"[db] Error en load_from_db: {e}")


# ─── Logging ─────────────────────────────────────────────────────────────────

def log(msg: str):
    entry = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    state["logs"].append(entry)
    if len(state["logs"]) > 200:
        state["logs"] = state["logs"][-200:]
    print(entry)


# ─── Cliente CLOB ─────────────────────────────────────────────────────────────

def init_client() -> bool:
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON

        pk = state["credentials"].get("private_key", "")
        if not pk:
            log("No hay clave privada configurada.")
            return False

        from eth_account import Account
        signer_addr = Account.from_key(pk).address

        configured_addr = (state["credentials"].get("address") or "").strip()
        if configured_addr and configured_addr.lower() != signer_addr.lower():
            funder   = configured_addr
            sig_type = 2
        else:
            funder   = None
            sig_type = 0

        client = ClobClient(CLOB_HOST, key=pk, chain_id=POLYGON, signature_type=sig_type, funder=funder)
        client.set_api_creds(client.create_or_derive_api_creds())
        state["client"] = client
        log(f"Cliente CLOB listo | signer={signer_addr} | funder={funder or signer_addr} | sig_type={sig_type}")
        return True
    except Exception as e:
        log(f"Error iniciando cliente CLOB: {e}")
        state["client"] = None
        return False


# ─── Posiciones propias ───────────────────────────────────────────────────────

DUST_THRESHOLD = 0.01


def fetch_positions() -> list:
    address = state["credentials"].get("address", "").strip()
    if not address:
        return []
    all_positions: list = []
    offset = 0
    limit  = 100
    try:
        while True:
            resp = requests.get(
                f"{DATA_HOST}/positions",
                params={"user": address.lower(), "limit": limit, "offset": offset},
                timeout=10,
            )
            if resp.status_code != 200:
                log(f"Error al obtener posiciones: HTTP {resp.status_code}")
                break
            data = resp.json()
            page = data if isinstance(data, list) else data.get("positions", [])
            if not page:
                break
            all_positions.extend(page)
            if len(page) < limit:
                break          # last page
            offset += limit
    except Exception as e:
        log(f"Error al obtener posiciones: {e}")
    return all_positions


def get_best_bid(token_id: str) -> float:
    if not token_id:
        return 0.0
    try:
        r = requests.get(
            f"{CLOB_HOST}/price",
            params={"token_id": token_id, "side": "SELL"},
            timeout=5,
        )
        if r.status_code == 200:
            price = float(r.json().get("price", 0))
            if price > 0:
                return price
        r2 = requests.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5)
        if r2.status_code == 200:
            return float(r2.json().get("mid", 0))
    except Exception:
        pass
    return 0.0


HIDDEN_RECOVERY_TTL = 60.0  # seconds between recovery checks for hidden positions


def enrich_positions(raw_positions: list) -> list:
    now = time.time()

    # ── Pass 1: parse metadata, decide which tokens need a price fetch ────────
    parsed     = []   # list of dicts with pre-parsed fields
    fetch_ids  = []   # token_ids that need get_best_bid()

    for raw in raw_positions:
        token_id = (
            raw.get("asset") or raw.get("tokenId") or
            raw.get("token_id") or raw.get("conditionId") or ""
        )
        size          = float(raw.get("size") or 0)
        avg_price_api = float(raw.get("avgPrice") or raw.get("averagePrice") or 0)
        cur_price_api = float(raw.get("curPrice") or 0)

        if size < DUST_THRESHOLD:
            continue
        if token_id in state["redeemed_tokens"]:
            continue

        # avgPrice resolution — priority order:
        # 0. Manual user override (avg_price_overrides) — always wins, never overwritten.
        # 1. fill_seeded: position has a confirmed fill price from trade history —
        #    protect it; the Data API may only update it upward (never down).
        # 2. Within 90 s of a copy buy the Data API may report a corrupt low value
        #    due to its ~60 s sync delay.  During that window prefer the cache.
        # 3. Otherwise the Data API is authoritative for positions we have no better
        #    source for — accept its value.
        #
        # For any position appearing for the first time, kick off a background seed
        # so that rule 1 applies on the next poll.
        override = state["avg_price_overrides"].get(token_id)
        if override and override >= 0.001:
            # User-set value — skip API/cache update entirely
            avg_price          = override
            avg_price_reliable = True
        else:
            cache        = state["avg_price_cache"]
            fill_seeded  = token_id in state["fill_seeded"]
            copy_entry   = state["copy_positions"].get(token_id)
            bought_at    = copy_entry.get("bought_at", 0) if copy_entry else 0
            in_copy_win  = bought_at and (now - bought_at) < 90

            # Trigger fill-price seeding the first time we see this position,
            # as long as it hasn't been seeded yet and has no manual override.
            if token_id not in state["known_positions"]:
                state["known_positions"].add(token_id)
                if not fill_seeded:
                    # Use a look-back of 7 days for manually opened positions;
                    # copy positions use their recorded bought_at timestamp.
                    seed_ts = bought_at if bought_at else now - 7 * 86400
                    threading.Thread(
                        target=_seed_avg_price_from_fill,
                        args=(token_id, 0, seed_ts),
                        daemon=True,
                    ).start()

            if fill_seeded or in_copy_win:
                # Confirmed fill price — only allow upward corrections from API
                if avg_price_api >= 0.01 and avg_price_api > cache.get(token_id, 0.0):
                    cache[token_id] = avg_price_api
            else:
                # No confirmed fill yet — accept Data API value as best available
                if avg_price_api >= 0.01:
                    cache[token_id] = avg_price_api

            avg_price          = cache.get(token_id, avg_price_api)
            avg_price_reliable = avg_price >= 0.01

        title = (
            raw.get("title") or raw.get("question") or
            raw.get("slug") or (f"{token_id[:20]}…" if token_id else "Desconocido")
        )

        entry = {
            "token_id":      token_id,
            "size":          size,
            "avg_price":     avg_price,
            "avg_price_reliable": avg_price_reliable,
            "cur_price_api": cur_price_api,
            "title":         title,
            "outcome":       raw.get("outcome") or raw.get("side") or "",
            "redeemable":    bool(raw.get("redeemable", False)),
            "condition_id":  raw.get("conditionId", ""),
            "outcome_index": int(raw.get("outcomeIndex", 0)),
            "is_hidden":     False,
            "hidden_meta":   None,
        }

        if token_id in state["hidden_tokens"]:
            meta   = state["hidden_positions"].get(token_id, {})
            reason = meta.get("reason", "")
            if reason != "perdida":
                continue  # manually hidden — skip entirely, no API call
            # Rate-limit recovery checks to once per HIDDEN_RECOVERY_TTL
            last = state["_hidden_check_ts"].get(token_id, 0.0)
            if now - last < HIDDEN_RECOVERY_TTL:
                continue   # checked recently, still skip
            entry["is_hidden"]   = True
            entry["hidden_meta"] = meta
            fetch_ids.append(token_id)
        else:
            # Skip CLOB price fetch for positions already reported as 0 by the
            # Data API — they are settled losses; treat price as 0 directly.
            if cur_price_api > 0:
                fetch_ids.append(token_id)

        parsed.append(entry)

    # ── Pass 2: fetch all prices in parallel ──────────────────────────────────
    prices: dict[str, float] = {}
    if fetch_ids:
        with ThreadPoolExecutor(max_workers=min(len(fetch_ids), 12)) as ex:
            futures = {ex.submit(get_best_bid, tid): tid for tid in fetch_ids}
            for future in as_completed(futures):
                tid = futures[future]
                try:
                    prices[tid] = future.result()
                except Exception:
                    prices[tid] = 0.0

    # ── Pass 3: process results ───────────────────────────────────────────────
    result = []
    for entry in parsed:
        token_id      = entry["token_id"]
        live_price    = prices.get(token_id, 0.0)
        size          = entry["size"]
        avg_price     = entry["avg_price"]
        avg_price_reliable = entry["avg_price_reliable"]
        title         = entry["title"]
        outcome       = entry["outcome"]
        cur_price_api = entry["cur_price_api"]

        if entry["is_hidden"]:
            # Update TTL regardless of result
            state["_hidden_check_ts"][token_id] = now
            meta = entry["hidden_meta"]
            if live_price >= 0.05:
                _delete_hidden(token_id)
                corrective_profit = round(meta.get("size", 0) * meta.get("avg_price", 0), 2)
                with _db_lock:
                    with _db_conn() as conn:
                        conn.execute(
                            "INSERT INTO closed_positions "
                            "(ts, title, outcome, size, avg_price, close_price, cost, revenue, profit, type) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (datetime.now().strftime("%Y-%m-%d %H:%M"),
                             title, outcome,
                             meta.get("size", 0), meta.get("avg_price", 0),
                             0.0, 0.0, 0.0, corrective_profit, "reactivada")
                        )
                log(f"[bot] Posición reactivada (precio recuperado a {live_price:.3f}): {title[:45]}")
                # fall through — enrich normally below
            else:
                continue  # still worthless

        current = live_price if live_price > 0 else cur_price_api

        # Auto-hide worthless losing positions
        if current < 0.01:
            is_new = token_id not in state["hidden_tokens"]
            meta = {
                "title": title, "outcome": outcome,
                "size": round(size, 4), "avg_price": round(avg_price, 4),
                "cost": round(size * avg_price, 2), "reason": "perdida",
            }
            _upsert_hidden(token_id, meta)
            if is_new:
                record_close(title, outcome, round(size, 4), round(avg_price, 4), 0.0, "perdida")
                log(f"[bot] Apuesta perdida ocultada y contabilizada: {title[:45]}")
            save_config()
            continue

        # Only compute P&L when avgPrice is reliable; otherwise show null
        if avg_price_reliable and avg_price > 0:
            pnl_pct = (current - avg_price) / avg_price * 100
        else:
            pnl_pct = None

        TAKER_FEE  = 0.02
        cost       = round(size * avg_price, 2) if avg_price_reliable else None
        sell_value = round(size * current * (1 - TAKER_FEE), 2)
        net_profit = round(sell_value - cost, 2) if cost is not None else None

        target       = state["profit_targets"].get(token_id)
        already_sold = token_id in state["sold_tokens"]

        # Fecha de apertura — disponible para copytrades
        copy_entry   = state["copy_positions"].get(token_id)
        bought_at_ts = copy_entry.get("bought_at") if copy_entry else None
        opened_at    = (
            datetime.fromtimestamp(bought_at_ts).strftime("%Y-%m-%d %H:%M")
            if bought_at_ts else None
        )

        result.append({
            "token_id":           token_id,
            "title":              title,
            "outcome":            outcome,
            "size":               round(size, 4),
            "avg_price":          round(avg_price, 4) if avg_price_reliable else None,
            "avg_price_reliable": avg_price_reliable,
            "current_price":      round(current, 4),
            "value":              round(size * current, 2),
            "cost":               cost,
            "sell_value":         sell_value,
            "net_profit":         net_profit,
            "pnl_pct":            round(pnl_pct, 2) if pnl_pct is not None else None,
            "target_pct":         target,
            "redeemable":         entry["redeemable"],
            "condition_id":       entry["condition_id"],
            "outcome_index":      entry["outcome_index"],
            "auto_sell_active":   target is not None and not already_sold and avg_price_reliable,
            "avg_price_override": token_id in state["avg_price_overrides"],
            "sold":               already_sold,
            "opened_at":          opened_at,
        })
    return result


# ─── Venta propia ─────────────────────────────────────────────────────────────

def fetch_fill_price(token_id: str, min_ts: float, retries: int = 3, side: str = "SELL") -> float:
    """Query the Data API for the actual fill price of the most recent trade of the given
    side (BUY or SELL) on token_id placed after min_ts (unix timestamp).
    Returns 0.0 if not found."""
    address = state["credentials"].get("address", "").strip()
    if not address:
        return 0.0
    for attempt in range(retries):
        try:
            time.sleep(1.5)   # give the trade time to appear in the API
            resp = requests.get(
                f"{DATA_HOST}/trades",
                params={"user": address.lower(), "limit": 10},
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            trades = resp.json()
            if not isinstance(trades, list):
                trades = trades.get("trades", [])
            for t in trades:
                t_token = t.get("asset") or t.get("tokenId") or t.get("token_id") or ""
                if t_token != token_id:
                    continue
                if (t.get("side") or "").upper() != side.upper():
                    continue
                # Parse timestamp — API returns ISO string or unix int/float
                ts_raw = t.get("timestamp") or t.get("createdAt") or t.get("ts") or ""
                try:
                    t_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
                except Exception:
                    try:
                        t_ts = float(ts_raw)
                    except Exception:
                        t_ts = 0.0
                if t_ts < min_ts - 5:   # 5-second tolerance
                    continue
                price = float(t.get("price") or 0)
                if price > 0:
                    log(f"[fill] Precio de ejecución real ({side}): {price:.4f} (vs estimado)")
                    return price
        except Exception:
            pass
    return 0.0


SELL_PRICE_FLOOR_PCT = 0.05  # FOK rejects if book can't fill at ≥ 95% of reference price

def sell_position(token_id: str, size: float, price: float | None = None,
                  floor_override: float | None = None) -> tuple[bool, str]:
    """Sell shares via market order (FOK).

    `price` is used as a reference price floor: the order will only fill if the
    average execution price is ≥ price * (1 - SELL_PRICE_FLOOR_PCT).  This prevents
    catastrophic fills in illiquid markets where the CLOB would otherwise walk the
    entire order book at near-zero prices.

    Pass `floor_override` (≥ 0) to set an exact floor directly instead of computing
    it from `price`.  Pass 0 to disable the floor entirely (any-price sell).
    """
    client = state.get("client")
    if not client:
        return False, "Cliente CLOB no inicializado"
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        # Determine floor: caller-supplied override takes precedence
        if floor_override is not None:
            floor = round(float(floor_override), 4)
        elif price and price > 0:
            floor = round(price * (1 - SELL_PRICE_FLOOR_PCT), 4)
        else:
            floor = 0

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=float(size),
            side="SELL",
            price=floor,
        )
        signed = client.create_market_order(order_args)
        resp   = client.post_order(signed, OrderType.FOK)

        # Normalise response to a dict so we can inspect the status
        resp_dict = resp if isinstance(resp, dict) else (resp.__dict__ if hasattr(resp, "__dict__") else {})
        status    = str(resp_dict.get("status", "")).lower()

        # Only treat as failure when the exchange explicitly reports a cancellation
        if status in ("cancelled", "canceled", "unmatched"):
            msg = f"Orden FOK cancelada — sin liquidez o precio mínimo no alcanzado (estado: {status})"
            log(f"[sell] {msg} — token: {token_id[:20]}…")
            return False, msg

        log(f"SELL ejecutado — token: {token_id[:20]}… size: {size} floor={floor} → {resp}")
        state["sold_tokens"].add(token_id)
        return True, str(resp)
    except Exception as e:
        log(f"Error al vender {token_id[:20]}…: {e}")
        return False, str(e)


# ─── Canje de posiciones resueltas ───────────────────────────────────────────

def redeem_position(token_id: str, title: str,
                    condition_id: str = "", outcome_index: int = -1) -> tuple[bool, str]:
    """Redeem a resolved winning position via the CTF contract on-chain."""
    pk = state["credentials"].get("private_key", "")
    if not pk:
        return False, "No hay clave privada"
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        from eth_account import Account

        if not condition_id or outcome_index < 0:
            address = state["credentials"].get("address", "").strip()
            resp = requests.get(
                f"{DATA_HOST}/positions",
                params={"user": address.lower()},
                timeout=10,
            )
            if resp.status_code != 200:
                return False, f"No se pudieron obtener posiciones (HTTP {resp.status_code})"
            for pos in (resp.json() if isinstance(resp.json(), list) else []):
                if str(pos.get("asset") or pos.get("tokenId") or "") == str(token_id):
                    condition_id  = pos.get("conditionId", "")
                    outcome_index = int(pos.get("outcomeIndex", 0))
                    break

        if not condition_id:
            return False, "conditionId no disponible — mercado no encontrado en Data API"

        index_set = 1 << outcome_index

        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        acct = Account.from_key(pk)

        CTF_ADDRESS  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        ZERO_BYTES32 = b"\x00" * 32

        CTF_ABI = [{
            "inputs": [
                {"name": "collateralToken",    "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId",        "type": "bytes32"},
                {"name": "indexSets",          "type": "uint256[]"},
            ],
            "name": "redeemPositions",
            "outputs": [],
            "type": "function",
            "stateMutability": "nonpayable",
        }]

        ctf      = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
        cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        tx = ctf.functions.redeemPositions(
            USDC_ADDRESS, ZERO_BYTES32, cid_bytes, [index_set]
        ).build_transaction({
            "from":     acct.address,
            "nonce":    w3.eth.get_transaction_count(acct.address, "pending"),
            "gas":      200_000,
            "gasPrice": w3.eth.gas_price,
            "chainId":  137,
        })
        signed  = w3.eth.account.sign_transaction(tx, pk)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt.status == 1:
            _add_redeemed(token_id)
            log(f"[redeem] ✓ Canjeado: {title[:45]} (tx: {tx_hash.hex()[:16]}…)")
            return True, tx_hash.hex()
        else:
            return False, f"Transacción fallida: {tx_hash.hex()}"

    except Exception as e:
        log(f"[redeem] Error canjeando {title[:40]}: {e}")
        return False, str(e)


# ─── Auto-canje ──────────────────────────────────────────────────────────────

state["_redeeming"] = False


def check_and_redeem():
    """Find one redeemable position and redeem it. Skips if a redeem is already in flight."""
    if state["_redeeming"]:
        return
    for pos in list(state["positions"]):
        token_id = pos.get("token_id", "")
        if not token_id:
            continue
        if not pos.get("redeemable"):
            continue
        if token_id in state["redeemed_tokens"]:
            continue
        state["_redeeming"] = True
        try:
            log(f"[redeem] Auto-canjeando: {pos['title'][:45]}…")
            ok, msg = redeem_position(token_id, pos["title"],
                                      pos.get("condition_id", ""),
                                      pos.get("outcome_index", -1))
            if ok:
                # Use avg_price_cache as fallback when pos["avg_price"] is None
                avg_p = pos.get("avg_price") or state["avg_price_cache"].get(token_id, 0)
                size  = pos.get("size", 0)
                record_close(pos["title"], pos.get("outcome", ""),
                             size, avg_p, 1.0, "canjeada", token_id)
                credit_budget(size, 1.0)  # canjeada = recibe 1 USDC por token
                _delete_copy_position(token_id)  # clean up if it was a copy position
                log(f"[redeem] ✓ Registrado en historial: {pos['title'][:45]}")
        finally:
            state["_redeeming"] = False
        break  # one per call — next cycle handles the rest


# ─── Loop del bot propio ──────────────────────────────────────────────────────

def bot_loop():
    log("Bot iniciado — revisando posiciones cada 30 s.")
    while state["bot_running"]:
        try:
            raw      = fetch_positions()
            enriched = enrich_positions(raw)
            state["positions"]    = enriched
            state["last_update"]  = datetime.now().isoformat()
            active_ids = {p["asset"] if "asset" in p else p.get("tokenId", "") for p in raw}
            purge_settled_losses(active_ids)
            check_and_redeem()

            for pos in enriched:
                if pos["sold"]:
                    continue
                token_id = pos["token_id"]

                if pos.get("redeemable"):
                    continue

                if pos["current_price"] >= 0.95 and token_id not in state["sold_tokens"]:
                    log(
                        f"[bot] Precio {pos['current_price']:.4f} → mercado casi resuelto, "
                        f"vendiendo: {pos['title'][:40]}…"
                    )
                    sell_ts = time.time()
                    ok, msg = sell_position(token_id, pos["size"], pos["current_price"])
                    if ok:
                        fill = fetch_fill_price(token_id, sell_ts) or pos["current_price"]
                        credit_budget(pos["size"], fill)
                        record_close(pos["title"], pos["outcome"], pos["size"],
                                     pos["avg_price"], fill, "vendida", token_id)
                        _delete_copy_position(token_id)
                    else:
                        log(f"Error al vender posición resuelta: {msg}")
                    continue

                copy_entry = state["copy_positions"].get(token_id)
                if copy_entry:
                    age = time.time() - copy_entry.get("bought_at", 0)
                    if age < 60:
                        continue

                target = pos.get("target_pct")
                if target is not None and pos["pnl_pct"] is not None and pos["pnl_pct"] >= target:
                    log(
                        f"Objetivo alcanzado: {pos['title'][:40]} — "
                        f"P&L {pos['pnl_pct']:.1f}% ≥ {target}% → vendiendo…"
                    )
                    sell_ts = time.time()
                    ok, msg = sell_position(pos["token_id"], pos["size"], pos["current_price"])
                    if ok:
                        fill = fetch_fill_price(pos["token_id"], sell_ts) or pos["current_price"]
                        credit_budget(pos["size"], fill)
                        avg = pos["avg_price"] or state["avg_price_cache"].get(pos["token_id"], 0)
                        record_close(pos["title"], pos["outcome"], pos["size"],
                                     avg, fill, "vendida", pos["token_id"])
                        _delete_copy_position(pos["token_id"])
                    else:
                        log(f"Error al vender automáticamente: {msg}")
        except Exception as e:
            log(f"Error en el loop: {e}")

        time.sleep(1.0)

    log("Bot detenido.")


# ─── Copy Trading — Helpers ───────────────────────────────────────────────────

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def resolve_profile_url(url: str) -> tuple[str, str]:
    """Resolve a Polymarket profile URL to (username, wallet_address)."""
    m = re.search(r"polymarket\.com/@([^/?#]+)", url)
    if not m:
        raise ValueError("URL inválida — usa formato: https://polymarket.com/@username")

    username = m.group(1)
    resp = requests.get(
        f"https://polymarket.com/@{username}",
        headers=SCRAPE_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    html = resp.text

    nd_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if nd_match:
        try:
            next_data = json.loads(nd_match.group(1))
            data_str  = json.dumps(next_data)
            for pattern in [
                rf'"username"\s*:\s*"{re.escape(username)}"[^{{}}]{{0,400}}"address"\s*:\s*"(0x[a-fA-F0-9]{{40}})"',
                r'"address"\s*:\s*"(0x[a-fA-F0-9]{40})"[^{}]{0,400}"username"\s*:\s*"'
                + re.escape(username) + r'"',
                r'"proxyWallet"\s*:\s*"(0x[a-fA-F0-9]{40})"',
                r'"walletAddress"\s*:\s*"(0x[a-fA-F0-9]{40})"',
            ]:
                m2 = re.search(pattern, data_str, re.I | re.DOTALL)
                if m2:
                    return username, m2.group(1).lower()
            addresses = re.findall(r"0x[a-fA-F0-9]{40}", data_str)
            if addresses:
                return username, addresses[0].lower()
        except (json.JSONDecodeError, Exception):
            pass

    for pattern in [
        r'"address"\s*:\s*"(0x[a-fA-F0-9]{40})"',
        r'"proxyWallet"\s*:\s*"(0x[a-fA-F0-9]{40})"',
        r'"walletAddress"\s*:\s*"(0x[a-fA-F0-9]{40})"',
    ]:
        m3 = re.search(pattern, html, re.I)
        if m3:
            return username, m3.group(1).lower()

    raise ValueError(
        f"No se pudo resolver la dirección de @{username}. "
        "Prueba pegando directamente la dirección de wallet (0x…) en lugar de la URL."
    )


def get_user_activity(address: str, limit: int = 20) -> list:
    resp = requests.get(
        f"{DATA_HOST}/activity",
        params={"user": address, "limit": limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        timeout=10,
    )
    if resp.status_code == 429:
        raise RuntimeError("RATE_LIMITED")
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def get_portfolio_value(address: str) -> float:
    try:
        resp = requests.get(f"{DATA_HOST}/value", params={"user": address}, timeout=10)
        if resp.status_code != 200:
            return 0.0
        data = resp.json()
        if isinstance(data, dict):
            return float(data.get("value") or data.get("portfolioValue") or 0)
        return float(data) if data else 0.0
    except Exception:
        return 0.0


# ─── Copy Trading — Budget ────────────────────────────────────────────────────

def get_spent_today() -> float:
    """Read today's spent from DB (no lock needed — WAL allows concurrent reads)."""
    today = date.today().isoformat()
    with _db_conn() as conn:
        row = conn.execute("SELECT spent FROM daily_budget WHERE date=?", (today,)).fetchone()
        return float(row["spent"]) if row else 0.0


def get_remaining_budget() -> float:
    budget = state["copy_settings"]["daily_budget"]
    return max(0.0, budget - get_spent_today())


def credit_budget(size: float, price: float) -> float:
    """Reduce spent by floor(size * price) when a position is sold. Returns amount credited."""
    recovered = math.floor(size * price)
    if recovered <= 0:
        return 0.0
    today = date.today().isoformat()
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO daily_budget (date, spent) VALUES (?, 0.0)", (today,))
            conn.execute(
                "UPDATE daily_budget SET spent = MAX(0.0, spent - ?) WHERE date=?",
                (recovered, today)
            )
    spent = get_spent_today()
    log(f"[budget] Venta recupera ${recovered:.0f} → gastado hoy ${spent:.2f}")
    return float(recovered)


TAKER_FEE = 0.02


def record_close(title: str, outcome: str, size: float, avg_price: float,
                 close_price: float, close_type: str, token_id: str = ""):
    """Write a closed position to the DB and update session stats."""
    avg_price  = float(avg_price  or 0)   # guard against None from unreliable API data
    close_price = float(close_price or 0)
    size        = float(size       or 0)
    cost    = round(size * avg_price, 2)
    revenue = round(
        size * close_price * (1 - TAKER_FEE) if close_type != "canjeada" else size * close_price,
        2,
    )
    profit = round(revenue - cost, 2)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO closed_positions "
                "(ts, title, outcome, size, avg_price, close_price, cost, revenue, profit, type, token_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M"), title, outcome,
                 round(size, 4), round(avg_price, 4), round(close_price, 4),
                 cost, revenue, profit, close_type, token_id)
            )
    sess = state["session"]
    sess["profit"] = round(sess["profit"] + profit, 2)
    if close_type == "perdida":
        sess["lost"] += 1
    else:
        sess["won"]  += 1 if profit >= 0 else 0
        sess["lost"] += 0 if profit >= 0 else 1


def purge_settled_losses(active_token_ids: set):
    """Remove hidden losing positions that Polymarket has already settled."""
    settled = [tid for tid in list(state["hidden_tokens"]) if tid not in active_token_ids]
    for tid in settled:
        _delete_hidden(tid)
        _delete_copy_position(tid)  # clean up if it was a copy position
    if settled:
        log(f"[bot] {len(settled)} apuesta(s) perdida(s) liquidada(s) por Polymarket (ya contabilizadas)")


def calculate_bet(their_usdc: float, profile_address: str) -> tuple[float, str | None]:
    """Returns (amount_usdc, skip_reason_or_None)."""
    s         = state["copy_settings"]
    remaining = get_remaining_budget()

    if remaining < 1.0:
        return 0.0, f"Presupuesto diario agotado (${s['daily_budget']:.2f}/día)"

    if s["mode"] == "fixed":
        amount = s["fixed_amount"]
    else:
        profile       = state["copy_profiles"].get(profile_address, {})
        portfolio_val = profile.get("portfolio_value", 0.0)
        if portfolio_val <= 0:
            amount = s["fixed_amount"]
        else:
            proportion = their_usdc / portfolio_val
            amount     = proportion * s["daily_budget"]

    if amount < 1.0:
        return 0.0, f"Apuesta calculada ${amount:.2f} < mínimo $1.00"

    amount = min(round(amount, 2), remaining)

    if amount < 1.0:
        return 0.0, f"Presupuesto restante insuficiente (${remaining:.2f})"

    return amount, None


# ─── Copy Trading — Ejecución ────────────────────────────────────────────────

def execute_copy_trade(token_id: str, amount_usdc: float) -> tuple[bool, str]:
    """Place a BUY market order. Returns (success, message)."""
    client = state.get("client")
    if not client:
        return False, "Cliente CLOB no inicializado — configura tu clave privada"
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        order_args = MarketOrderArgs(token_id=token_id, amount=amount_usdc, side="BUY")
        signed     = client.create_market_order(order_args)
        try:
            maker     = getattr(signed, "maker", None) or (signed.get("maker") if isinstance(signed, dict) else "?")
            sig       = getattr(signed, "signature", None) or (signed.get("signature") if isinstance(signed, dict) else "?")
            sig_type  = getattr(signed, "signatureType", None) or (signed.get("signatureType") if isinstance(signed, dict) else "?")
            log(f"[order] maker={maker} | sig_type={sig_type} | sig={str(sig)[:20]}…")
        except Exception:
            log(f"[order] raw={str(signed)[:120]}")
        resp = client.post_order(signed, OrderType.FOK)
        return True, str(resp)
    except Exception as e:
        return False, str(e)


def _seed_avg_price_from_fill(token_id: str, amount_usdc: float, buy_ts: float) -> None:
    """Fetch the actual fill price right after a BUY and seed avg_price_cache.

    This runs in a background thread so it doesn't block the copy loop.
    It's the authoritative source for avg_price — it fires before the Data API
    poll can cache a corrupt sync value (which can persist for ~60 s).

    On success the token_id is added to state["fill_seeded"] so enrich_positions
    knows not to let the Data API overwrite the confirmed fill price.
    """
    try:
        fill = fetch_fill_price(token_id, buy_ts, retries=5, side="BUY")
        if fill and fill > 0:
            state["avg_price_cache"][token_id] = fill   # authoritative fill price — always overwrite
            state["fill_seeded"].add(token_id)
            log(f"[avg_price] Seeded from fill (BUY): {token_id[:20]}… → {fill:.4f}")
        else:
            log(f"[avg_price] No fill price available for {token_id[:20]}…, "
                "Data API value will be used when it stabilises")
    except Exception as e:
        log(f"[avg_price] Error seeding from fill for {token_id[:20]}…: {e}")


# ─── Copy Trading — Procesamiento de actividad ───────────────────────────────

def execute_copy_sell(token_id: str, title: str, profile_username: str) -> tuple[bool, str]:
    """Sell our copy position for a given token. Returns (success, message)."""
    address  = state["credentials"].get("address", "").strip()
    our_size = 0.0

    if address:
        try:
            resp = requests.get(
                f"{DATA_HOST}/positions",
                params={"user": address.lower()},
                timeout=10,
            )
            if resp.status_code == 200:
                positions = resp.json()
                if isinstance(positions, list):
                    for pos in positions:
                        pos_token = (pos.get("asset") or pos.get("tokenId") or
                                     pos.get("token_id") or "")
                        if pos_token == token_id:
                            our_size = float(pos.get("size") or 0)
                            break
        except Exception:
            pass

    if our_size <= 0.01:
        return False, "No tenemos posición en este token"

    sell_ts = time.time()
    ok, msg = sell_position(token_id, our_size)
    if ok:
        _delete_copy_position(token_id)
        fill = fetch_fill_price(token_id, sell_ts) or get_best_bid(token_id)
        if fill > 0:
            credit_budget(our_size, fill)
    return ok, "" if ok else msg


def process_copy_activity(profile: dict, activity: list):
    addr      = profile["address"]
    last_seen = profile.get("last_seen_id")

    new_trades = []
    for item in activity:
        if item.get("type") != "TRADE":
            continue
        item_id = str(item.get("id") or item.get("transactionHash") or "")
        if not item_id:
            continue
        if item_id == last_seen:
            break
        new_trades.append(item)

    if not new_trades:
        return

    most_recent_id = str(new_trades[0].get("id") or new_trades[0].get("transactionHash") or "")
    if most_recent_id:
        state["copy_profiles"][addr]["last_seen_id"] = most_recent_id
        save_config()

    if last_seen is None:
        log(f"[copy] @{profile['username']} inicializado — {len(new_trades)} trade(s) existente(s) ignorado(s)")
        return

    for item in reversed(new_trades):
        side     = (item.get("side") or "BUY").upper()
        token_id = item.get("asset") or item.get("tokenId") or ""
        if not token_id:
            continue

        title     = str(item.get("title") or item.get("question") or item.get("market") or "?")
        price     = float(item.get("price") or 0)
        size      = float(item.get("size") or 0)
        usdc_size = float(item.get("usdcSize") or 0)
        their_usdc = usdc_size if usdc_size > 0 else (price * size)

        record = {
            "ts":         datetime.now().strftime("%H:%M:%S"),
            "profile":    profile["username"],
            "market":     title[:60],
            "side":       side,
            "their_usdc": round(their_usdc, 2),
            "our_amount": 0.0,
            "status":     "",
            "reason":     "",
        }

        # ── SELL ─────────────────────────────────────────────────────────────
        if side == "SELL":
            if token_id not in state["copy_positions"]:
                continue

            ok, msg = execute_copy_sell(token_id, title, profile["username"])
            if ok:
                record["status"] = "executed"
                record["side"]   = "SELL"
                log(f"[copy] SELL @{profile['username']} | {title[:35]} ✓")
            else:
                record["status"] = "failed"
                record["reason"] = msg
                log(f"[copy] SELL FAIL @{profile['username']} | {title[:35]} → {msg}")

            _insert_copy_trade(record)
            continue

        # ── BUY ──────────────────────────────────────────────────────────────
        if token_id in state["copy_positions"]:
            log(f"[copy] SKIP duplicado @{profile['username']} | {title[:35]} — ya tenemos posición")
            continue

        our_amount, skip_reason = calculate_bet(their_usdc, addr)
        record["our_amount"] = our_amount

        if skip_reason:
            record["status"] = "skipped"
            record["reason"] = skip_reason
            log(f"[copy] SKIP @{profile['username']} | {title[:35]} ${their_usdc:.2f} → {skip_reason}")
        else:
            min_price = float(state["copy_settings"].get("min_price_filter") or 0)
            if min_price > 0:
                current_bid = get_best_bid(token_id)
                if current_bid > 0 and current_bid < min_price:
                    record["status"] = "skipped"
                    record["reason"] = f"precio {current_bid:.2f} < mínimo {min_price:.2f}"
                    log(f"[copy] SKIP (precio bajo) @{profile['username']} | {title[:35]} — {current_bid:.2f} < {min_price:.2f}")
                    _insert_copy_trade(record)
                    continue

            buy_ts = time.time()
            success, msg = execute_copy_trade(token_id, our_amount)
            if success:
                _add_spent(our_amount)
                _upsert_copy_position(token_id, {
                    "size":      our_amount,
                    "market":    title[:60],
                    "profile":   profile["username"],
                    "bought_at": buy_ts,
                })
                # Seed avg_price_cache from the actual fill before the Data API
                # can poison the cache with its ~60 s sync delay value
                threading.Thread(
                    target=_seed_avg_price_from_fill,
                    args=(token_id, our_amount, buy_ts),
                    daemon=True,
                ).start()
                record["status"] = "executed"
                log(f"[copy] BUY @{profile['username']} | {title[:35]} ${our_amount:.2f} ✓")
            else:
                record["status"] = "failed"
                record["reason"] = msg
                log(f"[copy] FAIL @{profile['username']} | {title[:35]} ${our_amount:.2f} → {msg}")

        _insert_copy_trade(record)


# ─── Copy Trading — Loop ─────────────────────────────────────────────────────

def copy_trade_loop():
    log("[copy] Loop de copy trading iniciado.")
    backoff:           dict[str, tuple[float, float]] = {}
    portfolio_refresh: dict[str, float]               = {}

    last_redeem_check = 0.0
    while state["copy_running"]:
        now = time.time()
        if not state["bot_running"] and now - last_redeem_check > 30:
            check_and_redeem()
            last_redeem_check = now

        profiles = [p for p in state["copy_profiles"].values() if p.get("active", True)]
        now      = time.time()

        for i, profile in enumerate(profiles):
            if not state["copy_running"]:
                break

            addr = profile["address"]
            if addr in backoff and now < backoff[addr][0]:
                continue

            if i > 0:
                time.sleep(0.3)

            try:
                activity = get_user_activity(addr)
                process_copy_activity(profile, activity)
                backoff.pop(addr, None)

                last_refresh = portfolio_refresh.get(addr, 0)
                if now - last_refresh > 60:
                    val = get_portfolio_value(addr)
                    if val > 0:
                        state["copy_profiles"][addr]["portfolio_value"] = val
                        save_config()
                    portfolio_refresh[addr] = now

            except RuntimeError as e:
                if "RATE_LIMITED" in str(e):
                    prev_delay = backoff.get(addr, (0, 5))[1]
                    delay      = min(prev_delay * 2 if addr in backoff else 5, 60)
                    backoff[addr] = (now + delay, delay)
                    log(f"[copy] Rate limit en @{profile['username']} — esperando {delay:.0f}s")
                else:
                    log(f"[copy] Error polling @{profile['username']}: {e}")
            except Exception as e:
                log(f"[copy] Error polling @{profile['username']}: {e}")

        time.sleep(1.0)

    log("[copy] Loop de copy trading detenido.")


# ─── Rutas API — Bot propio ───────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_get_config():
    creds = dict(state["credentials"])
    if creds.get("private_key"):
        creds["private_key"] = "••••••••"
    return jsonify(
        {
            "credentials":    creds,
            "has_private_key": bool(state["credentials"].get("private_key")),
            "client_ready":   state["client"] is not None,
            "profit_targets": state["profit_targets"],
        }
    )


@app.route("/api/config", methods=["POST"])
def api_set_config():
    data  = request.get_json(force=True)
    creds = data.get("credentials", {})
    for k, v in creds.items():
        if k == "private_key" and v == "••••••••":
            continue
        state["credentials"][k] = v
    save_config()
    init_client()
    return jsonify({"ok": True, "client_ready": state["client"] is not None})


@app.route("/api/positions", methods=["GET"])
def api_positions():
    raw      = fetch_positions()
    enriched = enrich_positions(raw)
    state["positions"]   = enriched
    state["last_update"] = datetime.now().isoformat()
    return jsonify({"positions": enriched, "last_update": state["last_update"]})


@app.route("/api/positions/raw", methods=["GET"])
def api_positions_raw():
    """Return raw position data from the Data API for debugging avgPrice issues.

    Optional query param ?q=<substring> filters by title/token_id (case-insensitive).
    """
    raw   = fetch_positions()
    q     = (request.args.get("q") or "").lower()
    cache = state["avg_price_cache"]
    out   = []
    for r in raw:
        token_id = (r.get("asset") or r.get("tokenId") or r.get("token_id") or "")
        title    = (r.get("title") or r.get("question") or r.get("slug") or "")
        if q and q not in title.lower() and q not in token_id.lower():
            continue
        out.append({
            "token_id":      token_id,
            "title":         title,
            "outcome":       r.get("outcome") or r.get("side") or "",
            "size":          float(r.get("size") or 0),
            "avgPrice_api":  float(r.get("avgPrice") or r.get("averagePrice") or 0),
            "curPrice_api":  float(r.get("curPrice") or 0),
            "cached_avg":    cache.get(token_id),
            "redeemable":    bool(r.get("redeemable", False)),
        })
    return jsonify(out)


@app.route("/api/avg-price", methods=["POST"])
def api_set_avg_price():
    """Manually override the avg purchase price for a position.

    Body: { token_id, avg_price }   (avg_price=null or 0 clears the override)
    """
    data      = request.get_json(force=True)
    token_id  = data.get("token_id")
    avg_raw   = data.get("avg_price")
    if not token_id:
        return jsonify({"ok": False, "error": "token_id requerido"})

    overrides = state["avg_price_overrides"]
    if avg_raw is None or avg_raw == "" or float(avg_raw or 0) <= 0:
        overrides.pop(token_id, None)
        log(f"[avg_price] Override eliminado para {token_id[:20]}…")
    else:
        val = round(float(avg_raw), 6)
        overrides[token_id] = val
        # Also seed cache so existing callers get the right value immediately
        state["avg_price_cache"][token_id] = val
        log(f"[avg_price] Override manual: {token_id[:20]}… → {val:.4f}")

    save_config()
    return jsonify({"ok": True})


@app.route("/api/target", methods=["POST"])
def api_set_target():
    data     = request.get_json(force=True)
    token_id = data.get("token_id")
    target   = data.get("target_pct")
    if not token_id:
        return jsonify({"ok": False, "error": "token_id requerido"})
    if target is None or target == "":
        state["profit_targets"].pop(token_id, None)
    else:
        state["profit_targets"][token_id] = float(target)
        state["sold_tokens"].discard(token_id)
    save_config()
    return jsonify({"ok": True})


@app.route("/api/hide", methods=["POST"])
def api_hide():
    data     = request.get_json(force=True)
    token_id = data.get("token_id")
    hide     = data.get("hide", True)
    if not token_id:
        return jsonify({"ok": False})
    if hide:
        size      = float(data.get("size", 0))
        avg_price = float(data.get("avg_price", 0))
        meta = {
            "title":     data.get("title", ""),
            "outcome":   data.get("outcome", ""),
            "size":      size,
            "avg_price": avg_price,
            "cost":      round(size * avg_price, 2),
            "reason":    "manual",
        }
        _upsert_hidden(token_id, meta)
    else:
        _delete_hidden(token_id)
    return jsonify({"ok": True})


@app.route("/api/positions/closed", methods=["GET"])
def api_closed_positions():
    limit  = int(request.args.get("limit",  10))
    offset = int(request.args.get("offset",  0))
    with _db_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM closed_positions").fetchone()[0]
        rows  = conn.execute(
            "SELECT * FROM closed_positions ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    return jsonify({"rows": [dict(r) for r in rows], "total": total})


@app.route("/api/positions/closed/<int:row_id>/verify", methods=["POST"])
def api_verify_close_price(row_id):
    """Fetch the actual fill price from the Data API and update the DB row."""
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM closed_positions WHERE id=?", (row_id,)
        ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Registro no encontrado"})

    row = dict(row)
    token_id = row.get("token_id", "")
    if not token_id:
        return jsonify({"ok": False, "error": "No hay token_id — venta registrada antes de este fix"})

    # Parse ts to unix timestamp for fetch_fill_price
    try:
        ts_dt = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M")
        min_ts = ts_dt.timestamp() - 120   # allow 2 min before recorded ts
    except Exception:
        min_ts = 0.0

    address = state["credentials"].get("address", "").strip()
    fill_price = 0.0
    try:
        resp = requests.get(
            f"{DATA_HOST}/trades",
            params={"user": address.lower(), "limit": 50},
            timeout=10,
        )
        if resp.status_code == 200:
            trades = resp.json()
            if not isinstance(trades, list):
                trades = trades.get("trades", [])
            for t in trades:
                t_token = t.get("asset") or t.get("tokenId") or t.get("token_id") or ""
                if t_token != token_id:
                    continue
                if (t.get("side") or "").upper() != "SELL":
                    continue
                ts_raw = t.get("timestamp") or t.get("createdAt") or ""
                try:
                    t_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
                except Exception:
                    try:
                        t_ts = float(ts_raw)
                    except Exception:
                        t_ts = 0.0
                if t_ts < min_ts:
                    continue
                price = float(t.get("price") or 0)
                if price > 0:
                    fill_price = price
                    break
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    if fill_price <= 0:
        return jsonify({"ok": False, "error": "No se encontró el trade en la Data API"})

    size      = float(row["size"])
    avg_price = float(row["avg_price"] or 0)
    close_type = row["type"]
    cost      = round(size * avg_price, 2)
    revenue   = round(size * fill_price * (1 - TAKER_FEE) if close_type != "canjeada" else size * fill_price, 2)
    profit    = round(revenue - cost, 2)

    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "UPDATE closed_positions SET close_price=?, cost=?, revenue=?, profit=?, price_verified=1 WHERE id=?",
                (round(fill_price, 4), cost, revenue, profit, row_id)
            )

    log(f"[verify] Precio verificado para '{row['title'][:40]}': {row['close_price']:.4f} → {fill_price:.4f}")
    return jsonify({
        "ok":           True,
        "close_price":  round(fill_price, 4),
        "cost":         cost,
        "revenue":      revenue,
        "profit":       profit,
        "price_verified": True,
    })


@app.route("/api/positions/hidden", methods=["GET"])
def api_hidden_positions():
    result = []
    for token_id, meta in state["hidden_positions"].items():
        result.append({"token_id": token_id, **meta})
    # Include token_ids in hidden_tokens that may lack full metadata
    for token_id in state["hidden_tokens"]:
        if token_id not in state["hidden_positions"]:
            result.append({"token_id": token_id, "title": token_id[:30] + "…",
                           "outcome": "", "size": 0, "avg_price": 0, "cost": 0, "reason": "?"})
    return jsonify(result)


@app.route("/api/positions/hidden/<token_id>/check-trade", methods=["GET"])
def api_hidden_check_trade(token_id):
    """Check if there is any SELL or redemption trade for this token in the Data API."""
    address = state["credentials"].get("address", "").strip()
    if not address:
        return jsonify({"ok": False, "error": "Dirección no configurada"})
    try:
        resp = requests.get(
            f"{DATA_HOST}/trades",
            params={"user": address.lower(), "limit": 100},
            timeout=10,
        )
        if resp.status_code != 200:
            return jsonify({"ok": False, "error": f"Data API HTTP {resp.status_code}"})
        trades = resp.json()
        if not isinstance(trades, list):
            trades = trades.get("trades", [])

        for t in trades:
            t_token = t.get("asset") or t.get("tokenId") or t.get("token_id") or ""
            if t_token != token_id:
                continue
            side  = (t.get("side") or "").upper()
            if side not in ("SELL", "REDEEM", "MERGE"):
                continue
            price  = float(t.get("price") or 0)
            size   = float(t.get("size") or 0)
            ts_raw = t.get("timestamp") or t.get("createdAt") or ""
            return jsonify({
                "ok":    True,
                "found": True,
                "side":  side,
                "price": round(price, 4),
                "size":  round(size, 4),
                "ts":    str(ts_raw),
                "usdc":  round(price * size, 2),
            })

        return jsonify({"ok": True, "found": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/redeem", methods=["POST"])
def api_redeem():
    data     = request.get_json(force=True)
    token_id = data.get("token_id")
    title    = data.get("title", "?")
    if not token_id:
        return jsonify({"ok": False, "error": "token_id requerido"})
    if token_id in state["redeemed_tokens"]:
        return jsonify({"ok": True, "msg": "Ya canjeado"})
    condition_id  = data.get("condition_id", "")
    outcome_index = int(data.get("outcome_index", -1))
    ok, msg       = redeem_position(token_id, title, condition_id, outcome_index)
    if ok:
        pos  = next((p for p in state["positions"] if p["token_id"] == token_id), {})
        size = float(pos.get("size") or 0)
        avg_p = pos.get("avg_price") or state["avg_price_cache"].get(token_id, 0)
        record_close(title, pos.get("outcome", ""), size, avg_p, 1.0, "canjeada", token_id)
        credit_budget(size, 1.0)  # canjeada = recibe 1 USDC por token
    return jsonify({"ok": ok, "error": msg if not ok else "", "tx": msg if ok else ""})


SLIPPAGE_WARN = 0.08  # warn if fresh price is >8% below the UI price

@app.route("/api/sell", methods=["POST"])
def api_sell():
    data     = request.get_json(force=True)
    token_id = data.get("token_id")
    size     = data.get("size")
    price    = data.get("price")
    force    = bool(data.get("force", False))   # bypass slippage check
    # floor: explicit per-share floor price set by user (overrides the auto-computed one)
    floor_raw = data.get("floor")
    floor_override = float(floor_raw) if floor_raw is not None else None
    if not token_id or not size:
        return jsonify({"ok": False, "error": "token_id y size requeridos"})
    size_f  = float(size)
    price_f = float(price) if price else 0.0

    # ── Slippage pre-check + get fresh reference price ───────────────────────
    # Always fetch the live price (even when force=True) so we pass it as the
    # price floor to sell_position — this is what protects against illiquid fills.
    # Skip slippage check when an explicit floor override is set (user already
    # knows what floor they want).
    fresh = get_best_bid(token_id) if price_f > 0 else 0.0

    if price_f > 0 and not force and floor_override is None and fresh > 0:
        if fresh < price_f * (1 - SLIPPAGE_WARN):
            drop_pct = round((price_f - fresh) / price_f * 100, 1)
            log(f"[sell] Slippage detectado en '{token_id[:20]}': "
                f"UI={price_f:.4f} actual={fresh:.4f} (−{drop_pct}%)")
            return jsonify({
                "ok":          False,
                "slippage":    True,
                "sent_price":  round(price_f, 4),
                "fresh_price": round(fresh, 4),
                "diff_pct":    drop_pct,
            })

    # Use the fresh price as the reference floor (more current than UI price).
    # Falls back to price_f if the API call failed.
    ref_price = fresh if fresh > 0 else price_f

    pos     = next((p for p in state["positions"] if p["token_id"] == token_id), {})
    sell_ts = time.time()
    ok, msg = sell_position(token_id, size_f, ref_price if ref_price > 0 else None,
                            floor_override=floor_override)
    if ok:
        # Fetch actual fill price from Data API; fall back to UI price if unavailable
        fill = fetch_fill_price(token_id, sell_ts) or price_f
        if fill > 0:
            credit_budget(size_f, fill)
        record_close(pos.get("title", token_id[:30]), pos.get("outcome", ""),
                     size_f, pos.get("avg_price") or 0, fill or price_f, "vendida", token_id)
    return jsonify({"ok": ok, "error": msg if not ok else ""})


def _fetch_usdc_balance() -> float:
    """Return the on-chain USDC.e balance of the configured wallet address."""
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        pk      = state["credentials"].get("private_key", "")
        address = state["credentials"].get("address", "")
        if not pk:
            return 0.0
        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        # Use the configured address (main wallet). Fall back to key-derived address
        # only if no address is set (they are the same in EOA/signature_type=0 mode).
        if not address:
            from eth_account import Account
            address = Account.from_key(pk).address
        addr = Web3.to_checksum_address(address)
        USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        abi  = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf",
                 "outputs":[{"name":"","type":"uint256"}],"type":"function","stateMutability":"view"}]
        return w3.eth.contract(address=USDC, abi=abi).functions.balanceOf(addr).call() / 1_000_000
    except Exception as e:
        log(f"[balance] Error consultando USDC on-chain: {e}")
        return 0.0


def _pnl_for_period(where_sql: str, params: tuple = ()) -> dict:
    """Query closed_positions for a time window and return profit/won/lost."""
    q = f"""
        SELECT
            COALESCE(SUM(profit), 0.0)                                              AS profit,
            COALESCE(SUM(CASE WHEN profit >= 0 AND type != 'perdida' THEN 1 END), 0) AS won,
            COALESCE(SUM(CASE WHEN profit <  0 OR  type  = 'perdida' THEN 1 END), 0) AS lost
        FROM closed_positions
        {('WHERE ' + where_sql) if where_sql else ''}
    """
    with _db_conn() as conn:
        row = conn.execute(q, params).fetchone()
    return {
        "profit": round(float(row["profit"]), 2),
        "won":    int(row["won"]),
        "lost":   int(row["lost"]),
    }


@app.route("/api/session", methods=["GET"])
def api_session():
    return jsonify({
        "balance": round(_fetch_usdc_balance(), 2),
        **_pnl_for_period("date(ts) = date('now')"),   # daily (backward-compat field names)
    })


@app.route("/api/stats", methods=["GET"])
def api_stats():
    balance = _fetch_usdc_balance()
    open_positions = [p for p in state.get("positions", []) if not p.get("sold")]
    # Current market value of open positions
    open_value = sum(p.get("value", 0) or 0 for p in open_positions)
    # Money committed to open positions (what was actually paid)
    open_cost  = sum(p.get("cost",  0) or 0 for p in open_positions)
    return jsonify({
        "balance":    round(balance, 2),
        "open_value": round(open_value, 2),
        "open_cost":  round(open_cost, 2),
        "total":      round(balance + open_value, 2),
        "daily":      _pnl_for_period("date(ts) = date('now')"),
        "weekly":     _pnl_for_period("ts >= date('now', '-6 days')"),
        "monthly":    _pnl_for_period("strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')"),
        "all_time":   _pnl_for_period(""),
    })


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    if state["bot_running"]:
        return jsonify({"running": True, "msg": "Ya estaba en marcha"})
    if not state["client"]:
        init_client()
    state["bot_running"] = True
    t = threading.Thread(target=bot_loop, daemon=True)
    state["bot_thread"] = t
    t.start()
    return jsonify({"running": True})


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    state["bot_running"] = False
    return jsonify({"running": False})


@app.route("/api/bot/status", methods=["GET"])
def api_bot_status():
    return jsonify(
        {
            "running":      state["bot_running"],
            "client_ready": state["client"] is not None,
            "last_update":  state.get("last_update"),
            "logs":         state["logs"][-30:],
        }
    )


# ─── Rutas API — Copy Trading ────────────────────────────────────────────────

@app.route("/api/copy/profiles", methods=["GET"])
def api_copy_profiles():
    profiles = list(state["copy_profiles"].values())
    safe = [{k: v for k, v in p.items() if k != "last_seen_id"} for p in profiles]
    return jsonify(safe)


@app.route("/api/copy/profiles", methods=["POST"])
def api_copy_add_profile():
    data = request.get_json(force=True)
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URL requerida"}), 400

    try:
        username, address = resolve_profile_url(url)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if address in state["copy_profiles"]:
        return jsonify({"ok": False, "error": f"@{username} ya está siendo seguido"}), 400

    state["copy_profiles"][address] = {
        "url":             url,
        "username":        username,
        "address":         address,
        "portfolio_value": 0.0,
        "last_seen_id":    None,
        "active":          True,
    }

    try:
        val = get_portfolio_value(address)
        if val > 0:
            state["copy_profiles"][address]["portfolio_value"] = val
    except Exception:
        pass

    save_config()
    log(f"[copy] Perfil añadido: @{username} ({address[:10]}…)")
    return jsonify({"ok": True, "username": username, "address": address})


@app.route("/api/copy/profiles/<address>", methods=["DELETE"])
def api_copy_remove_profile(address):
    if address in state["copy_profiles"]:
        username = state["copy_profiles"][address].get("username", address)
        del state["copy_profiles"][address]
        save_config()
        log(f"[copy] Perfil eliminado: @{username}")
    return jsonify({"ok": True})


@app.route("/api/copy/settings", methods=["GET"])
def api_copy_get_settings():
    s         = state["copy_settings"]
    spent     = get_spent_today()
    remaining = get_remaining_budget()
    return jsonify(
        {
            "mode":                   s.get("mode", "fixed"),
            "fixed_amount":           s.get("fixed_amount", 1.0),
            "daily_budget":           s.get("daily_budget", 20.0),
            "min_price_filter":       s.get("min_price_filter", 0.0),
            "spent_today":            round(spent, 2),
            "remaining":              round(remaining, 2),
        }
    )


@app.route("/api/copy/settings", methods=["PUT"])
def api_copy_update_settings():
    data = request.get_json(force=True)
    s    = state["copy_settings"]

    if "mode" in data:
        if data["mode"] not in ("proportional", "fixed"):
            return jsonify({"ok": False, "error": "mode debe ser 'proportional' o 'fixed'"}), 400
        s["mode"] = data["mode"]
    if "fixed_amount" in data:
        v = float(data["fixed_amount"])
        if v < 1:
            return jsonify({"ok": False, "error": "fixed_amount mínimo $1"}), 400
        s["fixed_amount"] = v
    if "daily_budget" in data:
        v = float(data["daily_budget"])
        if v < 1:
            return jsonify({"ok": False, "error": "daily_budget mínimo $1"}), 400
        s["daily_budget"] = v
    if "min_price_filter" in data:
        v = float(data["min_price_filter"])
        if v < 0 or v >= 1:
            return jsonify({"ok": False, "error": "min_price_filter debe estar entre 0 y 1"}), 400
        s["min_price_filter"] = v
    save_config()
    return jsonify({"ok": True, "remaining": round(get_remaining_budget(), 2)})


@app.route("/api/copy/budget/reset", methods=["POST"])
def api_budget_reset():
    """Reset today's spent to zero."""
    today = date.today().isoformat()
    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO daily_budget (date, spent) VALUES (?, 0.0)",
                (today,)
            )
    log("[budget] Gastado hoy reiniciado a $0")
    return jsonify({"ok": True, "remaining": round(get_remaining_budget(), 2)})


@app.route("/api/copy/trades", methods=["GET"])
def api_copy_trades():
    """Return last 50 copy trade records from DB."""
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM copy_trades_log ORDER BY id ASC LIMIT 50"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/copy/trades", methods=["DELETE"])
def api_copy_clear_trades():
    """Delete all copy trade log entries."""
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("DELETE FROM copy_trades_log")
    return jsonify({"ok": True})


@app.route("/api/copy/start", methods=["POST"])
def api_copy_start():
    if state["copy_running"]:
        return jsonify({"running": True})
    if not state["client"]:
        init_client()
    state["copy_running"] = True
    t = threading.Thread(target=copy_trade_loop, daemon=True)
    state["copy_thread"] = t
    t.start()
    return jsonify({"running": True})


@app.route("/api/copy/stop", methods=["POST"])
def api_copy_stop():
    state["copy_running"] = False
    return jsonify({"running": False})


@app.route("/api/copy/diagnose", methods=["GET"])
def api_copy_diagnose():
    client = state.get("client")
    if not client:
        return jsonify({"error": "cliente no inicializado"})
    result = {}
    try:
        result["signer_address"] = client.get_address()
    except Exception as e:
        result["signer_address_error"] = str(e)
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        bal   = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        if hasattr(bal, "__dict__"):
            bal = bal.__dict__
        usdc   = int(bal.get("balance", 0)) / 1_000_000 if isinstance(bal, dict) else 0
        result["usdc_balance"] = f"${usdc:.2f}"
        result["allowances"]   = bal.get("allowances", {}) if isinstance(bal, dict) else str(bal)
        usdc_ok = not any(int(v) == 0 for v in (bal.get("allowances", {}) or {}).values()) if isinstance(bal, dict) else False
    except Exception as e:
        result["balance_error"] = str(e)
        usdc_ok = False
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        from eth_account import Account
        pk = state["credentials"].get("private_key", "")
        CTF_ADDRESS       = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        CTF_EXCHANGE      = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
        NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
        ERC1155_ABI = [{"inputs":[{"name":"account","type":"address"},{"name":"operator","type":"address"}],
                        "name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],
                        "type":"function","stateMutability":"view"}]
        w3   = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        acct = Account.from_key(pk)
        ctf  = w3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_ABI)
        ctf_approved      = ctf.functions.isApprovedForAll(acct.address, CTF_EXCHANGE).call()
        neg_risk_approved = ctf.functions.isApprovedForAll(acct.address, NEG_RISK_EXCHANGE).call()
        result["ctf_sell_approved"]      = ctf_approved
        result["neg_risk_sell_approved"] = neg_risk_approved
        sell_ok = ctf_approved and neg_risk_approved
    except Exception as e:
        result["ctf_approve_check_error"] = str(e)
        sell_ok = False
    result["needs_approval"]      = not (usdc_ok and sell_ok)
    result["needs_sell_approval"] = not sell_ok
    try:
        orders = client.get_orders()
        result["open_orders"] = len(orders) if isinstance(orders, list) else orders
    except Exception as e:
        result["orders_error"] = str(e)
    return jsonify(result)


@app.route("/api/copy/approve", methods=["POST"])
def api_copy_approve():
    """Send on-chain approve transactions so Polymarket can spend USDC (buy) and outcome tokens (sell)."""
    pk = state["credentials"].get("private_key", "")
    if not pk:
        return jsonify({"ok": False, "error": "No hay clave privada"})
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        from eth_account import Account

        POLYGON_RPC       = "https://polygon-bor-rpc.publicnode.com"
        USDC_ADDRESS      = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        CTF_ADDRESS       = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        CTF_EXCHANGE      = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
        NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
        NEG_RISK_ADAPTER  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

        MAX_UINT256 = 2**256 - 1

        ERC20_ABI   = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
                        "name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function",
                        "stateMutability":"nonpayable"}]
        ERC1155_ABI = [{"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],
                        "name":"setApprovalForAll","outputs":[],"type":"function",
                        "stateMutability":"nonpayable"}]

        w3        = Web3(Web3.HTTPProvider(POLYGON_RPC))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        acct      = Account.from_key(pk)
        usdc      = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
        ctf       = w3.eth.contract(address=CTF_ADDRESS,  abi=ERC1155_ABI)

        txs       = []
        nonce     = w3.eth.get_transaction_count(acct.address, "pending")
        gas_price = w3.eth.gas_price

        def send_tx(fn, label):
            nonlocal nonce
            tx = fn.build_transaction({
                "from": acct.address, "nonce": nonce,
                "gas": 100_000, "gasPrice": gas_price, "chainId": 137,
            })
            signed  = w3.eth.account.sign_transaction(tx, pk)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            status  = "ok" if receipt.status == 1 else "failed"
            log(f"[approve] {label} → {status} (tx: {tx_hash.hex()[:16]}…)")
            txs.append({"label": label, "status": status, "tx": tx_hash.hex()})
            nonce += 1

        send_tx(usdc.functions.approve(CTF_EXCHANGE, MAX_UINT256),       "USDC→CTF_Exchange")
        send_tx(usdc.functions.approve(NEG_RISK_EXCHANGE, MAX_UINT256),  "USDC→NegRisk_Exchange")
        send_tx(ctf.functions.setApprovalForAll(CTF_EXCHANGE, True),     "CTF_tokens→CTF_Exchange")
        send_tx(ctf.functions.setApprovalForAll(NEG_RISK_EXCHANGE, True),"CTF_tokens→NegRisk_Exchange")

        client = state.get("client")
        if client:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            try:
                client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))
            except Exception:
                pass

        return jsonify({"ok": True, "transactions": txs})
    except Exception as e:
        log(f"[approve] Error: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/copy/status", methods=["GET"])
def api_copy_status():
    s     = state["copy_settings"]
    spent = get_spent_today()
    return jsonify(
        {
            "running":          state["copy_running"],
            "profile_count":    sum(1 for p in state["copy_profiles"].values() if p.get("active")),
            "spent_today":      round(spent, 2),
            "daily_budget":     s.get("daily_budget", 20.0),
            "remaining_budget": round(get_remaining_budget(), 2),
        }
    )


# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    with _db_conn() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key='password_hash'").fetchone()
    has_password = row is not None
    error = None

    if request.method == "POST":
        ip = request.remote_addr or "unknown"

        if not has_password:
            # First-run setup
            pw  = request.form.get("password", "")
            pw2 = request.form.get("password2", "")
            if len(pw) < 8:
                error = "La contraseña debe tener mínimo 8 caracteres"
            elif pw != pw2:
                error = "Las contraseñas no coinciden"
            else:
                h = generate_password_hash(pw)
                with _db_lock:
                    with _db_conn() as conn:
                        conn.execute("INSERT OR REPLACE INTO kv VALUES ('password_hash', ?)", (h,))
                session.clear()
                session["authenticated"] = True
                session["csrf_token"] = secrets.token_hex(32)
                session.permanent = True
                return redirect(url_for("index"))
        else:
            allowed, wait = _rate_limit_check(ip)
            if not allowed:
                m, s = divmod(wait, 60)
                error = f"Demasiados intentos. Espera {m}m {s}s"
            else:
                pw = request.form.get("password", "")
                if check_password_hash(row["value"], pw):
                    _clear_attempts(ip)
                    session.clear()
                    session["authenticated"] = True
                    session["csrf_token"] = secrets.token_hex(32)
                    session.permanent = True
                    return redirect(url_for("index"))
                else:
                    _record_failed(ip)
                    remaining = MAX_ATTEMPTS - len(_login_attempts.get(ip, []))
                    error = f"Contraseña incorrecta — {max(0, remaining)} intentos restantes"

    return render_template("login.html", has_password=has_password, error=error)


@app.route("/api/auth/change-password", methods=["POST"])
def api_change_password():
    data      = request.get_json(force=True, silent=True) or {}
    current   = data.get("current", "")
    new_pw    = data.get("new", "")
    confirm   = data.get("confirm", "")

    if len(new_pw) < 8:
        return jsonify({"ok": False, "error": "Mínimo 8 caracteres"}), 400
    if new_pw != confirm:
        return jsonify({"ok": False, "error": "Las contraseñas no coinciden"}), 400

    with _db_conn() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key='password_hash'").fetchone()
    if not row or not check_password_hash(row["value"], current):
        return jsonify({"ok": False, "error": "Contraseña actual incorrecta"}), 403

    h = generate_password_hash(new_pw)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('password_hash', ?)", (h,))
    log("[auth] Contraseña cambiada")
    return jsonify({"ok": True})


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Arranque ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    migrate_from_json()
    load_from_db()
    if state["credentials"].get("private_key"):
        init_client()
    app.secret_key = _get_or_create_secret_key()
    app.permanent_session_lifetime = timedelta(hours=12)
    print("Abriendo en http://localhost:5000")
    # Open browser automatically after the server is up
    def _open_browser():
        time.sleep(1.5)
        import webbrowser
        webbrowser.open("http://localhost:5000")
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
