"""
state.py — Estado global compartido, constantes y función de log.

Todos los módulos importan desde aquí; este fichero no importa de ningún
otro módulo propio para evitar dependencias circulares.
"""

import logging
import logging.handlers
from datetime import datetime

# ─── Estado global ────────────────────────────────────────────────────────────

state: dict = {
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
    "avg_price_cache":     {},   # token_id -> float: mejor avgPrice conocido
    "avg_price_overrides": {},   # token_id -> float: fijado manualmente por el usuario
    "fill_seeded":         set(),  # token_ids confirmados desde historial real de fills
    "known_positions":     set(),  # token_ids vistos al menos una vez
    "hidden_tokens": set(),
    "hidden_positions": {},      # token_id -> {title, outcome, size, avg_price, cost, reason}
    "_hidden_check_ts": {},      # token_id -> float: última comprobación de recuperación
    "_redeeming": False,
    "session": {                 # se resetea en cada reinicio del servidor
        "profit": 0.0,
        "won": 0,
        "lost": 0,
        "start": datetime.now().isoformat(),
    },
    # ── Copy trading ──────────────────────────────────────────────────────────
    "copy_profiles": {},         # address -> profile dict
    "copy_positions": {},        # token_id -> {size, market, profile, bought_at}
    "copy_settings": {
        "mode": "fixed",
        "fixed_amount": 1.0,
        "daily_budget": 20.0,
        "min_price_filter": 0.0,
        "max_price_filter": 0.0,  # 0 = desactivado; ej. 0.90 = saltar si precio ≥ 90¢
    },
    "copy_running": False,
    "copy_thread": None,
    # ── Telegram notifications ────────────────────────────────────────────────
    "telegram": {
        "bot_token": "",
        "chat_id": "",
    },
}

# ─── Constantes ───────────────────────────────────────────────────────────────

CLOB_HOST            = "https://clob.polymarket.com"
DATA_HOST            = "https://data-api.polymarket.com"
DUST_THRESHOLD       = 0.01
SELL_PRICE_FLOOR_PCT = 0.05    # FOK rechaza si el libro no cubre ≥ 95 % del precio de referencia
TAKER_FEE            = 0.02
SLIPPAGE_WARN        = 0.08    # avisa si el precio cae > 8 % respecto al de la UI
HIDDEN_RECOVERY_TTL  = 60.0    # segundos entre comprobaciones de recuperación

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─── Logging ──────────────────────────────────────────────────────────────────

_logger = logging.getLogger("pmbot")


def setup_file_logging(log_path: str = "polymarket.log") -> None:
    """Adjunta un RotatingFileHandler al logger 'pmbot'.

    Los archivos rotan a 5 MB; se conservan 3 copias de seguridad.
    Se llama una vez en el arranque desde app.py.
    """
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)


def log(msg: str) -> None:
    entry = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    state["logs"].append(entry)
    if len(state["logs"]) > 200:
        state["logs"] = state["logs"][-200:]
    print(entry)
    _logger.info(msg)
