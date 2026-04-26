"""Módulo de notificaciones Telegram.

Envía un mensaje cuando se cierra una posición (vendida, canjeada o perdida).
La configuración se almacena en la DB bajo las claves 'telegram_bot_token' y
'telegram_chat_id' del KV store, y se carga en state["telegram"] al arrancar.
"""
import threading

import requests

_lock = threading.Lock()

# Rellena app.py al arrancar vía load_telegram_config()
_config: dict = {"bot_token": "", "chat_id": "", "enabled": False}


def configure(bot_token: str, chat_id: str) -> None:
    """Actualiza la configuración Telegram en memoria."""
    with _lock:
        _config["bot_token"] = bot_token.strip()
        _config["chat_id"] = chat_id.strip()
        _config["enabled"] = bool(bot_token.strip() and chat_id.strip())


def is_configured() -> bool:
    return _config["enabled"]


def send(text: str) -> bool:
    """Envía *text* al chat configurado. Devuelve True si tiene éxito."""
    with _lock:
        token = _config.get("bot_token", "")
        chat_id = _config.get("chat_id", "")
        enabled = _config.get("enabled", False)

    if not enabled:
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        return resp.ok
    except Exception:
        return False


def notify_close(
    title: str,
    outcome: str,
    size: float,
    profit: float,
    close_type: str,
) -> None:
    """Notificación Telegram fire-and-forget al cerrar una posición."""
    if not is_configured():
        return

    emoji = {
        "vendida": "💰",
        "canjeada": "🏆",
        "perdida": "💸",
    }.get(close_type, "📋")

    sign = "+" if profit >= 0 else ""
    msg = (
        f"{emoji} <b>{close_type.upper()}</b>\n"
        f"📌 {title}\n"
        f"🎯 {outcome}\n"
        f"📦 {size:.2f} shares\n"
        f"{'✅' if profit >= 0 else '❌'} P&L: <b>{sign}{profit:.2f} USDC</b>"
    )
    threading.Thread(target=send, args=(msg,), daemon=True).start()
