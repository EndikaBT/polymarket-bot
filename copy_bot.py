"""
copy_bot.py — Bot de copy trading.

Contiene la resolución de perfiles, la gestión de presupuesto,
el procesamiento de actividad y el loop de polling.
"""

import json
import re
import threading
import time
from datetime import datetime

import requests

from bot import (
    _seed_avg_price_from_fill,
    check_and_redeem,
    fetch_fill_price,
    get_best_bid,
    sell_position,
)
from db import (
    _add_spent,
    _delete_copy_position,
    _insert_copy_trade,
    _upsert_copy_position,
    credit_budget,
    get_remaining_budget,
    save_config,
)
from state import DATA_HOST, SCRAPE_HEADERS, log, state

# ─── Resolución de perfiles ───────────────────────────────────────────────────


def resolve_profile_url(url: str) -> tuple[str, str]:
    """Resuelve una URL de perfil de Polymarket a (username, wallet_address)."""
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


# ─── Actividad y portfolio ────────────────────────────────────────────────────

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


# ─── Cálculo de apuesta ───────────────────────────────────────────────────────

def calculate_bet(their_usdc: float, profile_address: str) -> tuple[float, str | None]:
    """Devuelve (amount_usdc, skip_reason_o_None)."""
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


# ─── Ejecución de trades ──────────────────────────────────────────────────────

def execute_copy_trade(token_id: str, amount_usdc: float) -> tuple[bool, str]:
    """Lanza una orden de compra en mercado. Devuelve (success, message)."""
    client = state.get("client")
    if not client:
        return False, "Cliente CLOB no inicializado — configura tu clave privada"
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        order_args = MarketOrderArgs(token_id=token_id, amount=amount_usdc, side="BUY")
        signed     = client.create_market_order(order_args)
        try:
            maker    = getattr(signed, "maker", None) or (signed.get("maker") if isinstance(signed, dict) else "?")
            sig      = getattr(signed, "signature", None) or (signed.get("signature") if isinstance(signed, dict) else "?")
            sig_type = getattr(signed, "signatureType", None) or (signed.get("signatureType") if isinstance(signed, dict) else "?")
            log(f"[order] maker={maker} | sig_type={sig_type} | sig={str(sig)[:20]}…")
        except Exception:
            log(f"[order] raw={str(signed)[:120]}")
        resp = client.post_order(signed, OrderType.FOK)
        return True, str(resp)
    except Exception as e:
        return False, str(e)


def execute_copy_sell(token_id: str, title: str, profile_username: str) -> tuple[bool, str]:
    """Vende nuestra posición en copy para un token dado. Devuelve (success, message)."""
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


# ─── Procesamiento de actividad ───────────────────────────────────────────────

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

        title      = str(item.get("title") or item.get("question") or item.get("market") or "?")
        price      = float(item.get("price") or 0)
        size       = float(item.get("size") or 0)
        usdc_size  = float(item.get("usdcSize") or 0)
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


# ─── Loop de copy trading ─────────────────────────────────────────────────────

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
