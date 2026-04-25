"""
db.py — Capa de persistencia SQLite.

Expone helpers de escritura atómica, migración desde config.json,
carga inicial desde DB, y funciones de negocio que tocan únicamente
la base de datos (record_close, credit_budget, …).
"""

import json
import math
import os
import sqlite3
import threading
from datetime import date, datetime

from state import TAKER_FEE, log, state

# ─── Paths ────────────────────────────────────────────────────────────────────

DB_FILE     = os.path.join(os.path.dirname(__file__), "polymarket.db")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
BACKUP_FILE = os.path.join(os.path.dirname(__file__), "config.json.bak")

# ─── Conexión ─────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def _db_conn():
    """Abre una conexión WAL de vida corta. Cada llamada obtiene su propia conexión."""
    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ─── Esquema ──────────────────────────────────────────────────────────────────

def init_db():
    """Crea todas las tablas si no existen."""
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
        # Añadir columnas a DBs existentes anteriores a esta versión de esquema
        for col, definition in [
            ("token_id",       "TEXT"),
            ("price_verified", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE closed_positions ADD COLUMN {col} {definition}")
            except Exception:
                pass  # la columna ya existe


# ─── Persistencia de configuración ───────────────────────────────────────────

def _save_settings():
    """Persiste credenciales, profit_targets, copy_settings y copy_profiles en kv."""
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


# Alias compatible con los ~15 call sites que usan save_config()
save_config = _save_settings


# ─── Helpers de escritura ─────────────────────────────────────────────────────

def _upsert_hidden(token_id: str, meta: dict):
    """Inserta o reemplaza una fila en hidden_positions y actualiza los caches en memoria."""
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
    """Elimina un token de hidden_positions y actualiza los caches en memoria."""
    state["hidden_tokens"].discard(token_id)
    state["hidden_positions"].pop(token_id, None)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("DELETE FROM hidden_positions WHERE token_id=?", (token_id,))


def _upsert_copy_position(token_id: str, data: dict):
    """Inserta o reemplaza una fila en copy_positions y actualiza el cache en memoria."""
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
    """Elimina una fila de copy_positions y actualiza el cache en memoria."""
    state["copy_positions"].pop(token_id, None)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("DELETE FROM copy_positions WHERE token_id=?", (token_id,))


def _add_redeemed(token_id: str):
    """Marca un token como canjeado en la DB y en el set en memoria."""
    state["redeemed_tokens"].add(token_id)
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO redeemed_tokens (token_id) VALUES (?)", (token_id,))


def _add_spent(amount_usdc: float):
    """UPSERT de la fila del día y suma amount al gasto."""
    today = date.today().isoformat()
    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO daily_budget (date, spent) VALUES (?, ?) "
                "ON CONFLICT(date) DO UPDATE SET spent = spent + excluded.spent",
                (today, amount_usdc)
            )


def _credit_spent(amount_usdc: float):
    """Resta amount del gasto de hoy (mínimo 0)."""
    today = date.today().isoformat()
    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO daily_budget (date, spent) VALUES (?, 0.0)", (today,))
            conn.execute(
                "UPDATE daily_budget SET spent = MAX(0.0, spent - ?) WHERE date=?",
                (amount_usdc, today)
            )


def _insert_copy_trade(record: dict):
    """Añade una entrada al log de copy trades."""
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


# ─── Migración desde config.json ─────────────────────────────────────────────

def migrate_from_json():
    """Migración única. Solo se ejecuta si config.json existe y la tabla kv está vacía."""
    if not os.path.exists(CONFIG_FILE):
        return
    with _db_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0] > 0:
            return  # ya migrado
    print("[migrate] Migrando config.json → polymarket.db …")
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except Exception as e:
        print(f"[migrate] Error leyendo config.json: {e}")
        return

    with _db_lock:
        with _db_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('credentials', ?)",
                         (json.dumps(data.get("credentials", {})),))
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('profit_targets', ?)",
                         (json.dumps(data.get("profit_targets", {})),))
            cs = data.get("copy_settings", {})
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('copy_settings', ?)",
                         (json.dumps({k: cs[k] for k in ("mode", "fixed_amount", "daily_budget") if k in cs}),))
            conn.execute("INSERT OR REPLACE INTO kv VALUES ('copy_profiles', ?)",
                         (json.dumps(data.get("copy_profiles", [])),))
            today     = date.today().isoformat()
            old_date  = cs.get("budget_date", "")
            old_spent = float(cs.get("spent_today", 0.0))
            if old_date == today and old_spent > 0:
                conn.execute("INSERT OR REPLACE INTO daily_budget (date, spent) VALUES (?, ?)",
                             (today, old_spent))
            for tid, meta in data.get("hidden_positions", {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO hidden_positions "
                    "(token_id, title, outcome, size, avg_price, cost, reason) VALUES (?,?,?,?,?,?,?)",
                    (tid, meta.get("title", ""), meta.get("outcome", ""),
                     meta.get("size", 0), meta.get("avg_price", 0),
                     meta.get("cost", 0), meta.get("reason", ""))
                )
            for tid in data.get("hidden_tokens", []):
                conn.execute(
                    "INSERT OR IGNORE INTO hidden_positions "
                    "(token_id, title, outcome, size, avg_price, cost, reason) VALUES (?,?,?,?,?,?,?)",
                    (tid, "", "", 0, 0, 0, "?")
                )
            for tid, cp in data.get("copy_positions", {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO copy_positions "
                    "(token_id, size, market, profile, bought_at) VALUES (?,?,?,?,?)",
                    (tid, cp.get("size", 0), cp.get("market", ""),
                     cp.get("profile", ""), cp.get("bought_at", 0))
                )
            for tid in data.get("redeemed_tokens", []):
                conn.execute("INSERT OR IGNORE INTO redeemed_tokens (token_id) VALUES (?)", (tid,))
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


# ─── Carga inicial desde DB ───────────────────────────────────────────────────

def load_from_db():
    """Puebla el estado en memoria desde SQLite al arrancar."""
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

            for row in conn.execute("SELECT * FROM hidden_positions").fetchall():
                r = dict(row)
                tid = r.pop("token_id")
                state["hidden_positions"][tid] = r
                state["hidden_tokens"].add(tid)

            for row in conn.execute("SELECT * FROM copy_positions").fetchall():
                r = dict(row)
                tid = r.pop("token_id")
                state["copy_positions"][tid] = r

            for row in conn.execute("SELECT token_id FROM redeemed_tokens").fetchall():
                state["redeemed_tokens"].add(row["token_id"])

    except Exception as e:
        print(f"[db] Error en load_from_db: {e}")


# ─── Presupuesto diario ───────────────────────────────────────────────────────

def get_spent_today() -> float:
    """Lee el gasto de hoy desde DB (sin lock — WAL permite lecturas concurrentes)."""
    today = date.today().isoformat()
    with _db_conn() as conn:
        row = conn.execute("SELECT spent FROM daily_budget WHERE date=?", (today,)).fetchone()
        return float(row["spent"]) if row else 0.0


def get_remaining_budget() -> float:
    budget = state["copy_settings"]["daily_budget"]
    return max(0.0, budget - get_spent_today())


def credit_budget(size: float, price: float) -> float:
    """Reduce el gasto en floor(size * price) cuando se vende una posición. Devuelve el importe acreditado."""
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


# ─── Cierre de posiciones ─────────────────────────────────────────────────────

def record_close(title: str, outcome: str, size: float, avg_price: float,
                 close_price: float, close_type: str, token_id: str = ""):
    """Escribe una posición cerrada en la DB y actualiza las estadísticas de sesión."""
    avg_price   = float(avg_price   or 0)
    close_price = float(close_price or 0)
    size        = float(size        or 0)
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
    """Elimina posiciones ocultas perdidas que Polymarket ya ha liquidado."""
    settled = [tid for tid in list(state["hidden_tokens"]) if tid not in active_token_ids]
    for tid in settled:
        _delete_hidden(tid)
        _delete_copy_position(tid)
    if settled:
        log(f"[bot] {len(settled)} apuesta(s) perdida(s) liquidada(s) por Polymarket (ya contabilizadas)")


# ─── Estadísticas por período ─────────────────────────────────────────────────

def _pnl_for_period(where_sql: str, params: tuple = ()) -> dict:
    """Consulta closed_positions para una ventana temporal y devuelve profit/won/lost."""
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
