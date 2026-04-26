"""
app.py — Aplicación Flask: rutas HTTP y arranque.

Toda la lógica de negocio vive en los módulos especializados:
  state.py    — estado global y constantes
  db.py       — capa SQLite
  auth.py     — autenticación y CSRF
  bot.py      — bot de venta propio
  copy_bot.py — bot de copy trading
"""

import atexit
import secrets
import signal
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from auth import (
    _clear_attempts,
    _get_or_create_secret_key,
    _rate_limit_check,
    _record_failed,
    check_auth,
    get_remaining_attempts,
    inject_csrf,
    security_headers,
)
from bot import (
    _fetch_usdc_balance,
    bot_loop,
    enrich_positions,
    fetch_fill_price,
    fetch_positions,
    get_best_bid,
    init_client,
    redeem_position,
    sell_position,
)
from copy_bot import (
    copy_trade_loop,
    get_portfolio_value,
    resolve_profile_url,
)
import notifier
from db import (
    _db_conn,
    _db_lock,
    _delete_hidden,
    _pnl_for_period,
    _upsert_hidden,
    credit_budget,
    get_remaining_budget,
    get_spent_today,
    init_db,
    load_from_db,
    load_telegram_config,
    migrate_from_json,
    record_close,
    save_config,
    save_telegram_config,
)
from state import DATA_HOST, SLIPPAGE_WARN, TAKER_FEE, log, setup_file_logging, state

# ─── Aplicación Flask ─────────────────────────────────────────────────────────

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

app.before_request(check_auth)
app.after_request(security_headers)
app.context_processor(inject_csrf)


# ─── Rutas — Vistas ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ─── Rutas API — Configuración ────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_get_config():
    creds = dict(state["credentials"])
    if creds.get("private_key"):
        creds["private_key"] = "••••••••"
    return jsonify({
        "credentials":     creds,
        "has_private_key": bool(state["credentials"].get("private_key")),
        "client_ready":    state["client"] is not None,
        "profit_targets":  state["profit_targets"],
    })


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


@app.route("/api/config/telegram", methods=["GET"])
def api_telegram_get():
    cfg = state["telegram"]
    token = cfg.get("bot_token", "")
    return jsonify({
        "bot_token": "••••" + token[-4:] if len(token) > 4 else ("••••" if token else ""),
        "chat_id": cfg.get("chat_id", ""),
        "configured": notifier.is_configured(),
    })


@app.route("/api/config/telegram", methods=["POST"])
def api_telegram_set():
    data = request.get_json(force=True, silent=True) or {}
    bot_token = data.get("bot_token", "")
    chat_id = data.get("chat_id", "")
    if bot_token.startswith("••••"):
        bot_token = state["telegram"].get("bot_token", "")
    save_telegram_config(bot_token, chat_id)
    log(f"[telegram] Configuración actualizada — configurado: {notifier.is_configured()}")
    return jsonify({"ok": True, "configured": notifier.is_configured()})


@app.route("/api/config/telegram/test", methods=["POST"])
def api_telegram_test():
    if not notifier.is_configured():
        return jsonify({"ok": False, "error": "Telegram no configurado"}), 400
    ok = notifier.send("✅ Polymarket Bot — notificaciones funcionando correctamente")
    return jsonify({"ok": ok, "error": None if ok else "Error al enviar mensaje. Verifica el token y chat_id."})


@app.route("/api/health", methods=["GET"])
def api_health():
    """Sonda de disponibilidad — no requiere autenticación."""
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat() + "Z"})


# ─── Rutas API — Posiciones ───────────────────────────────────────────────────

@app.route("/api/positions", methods=["GET"])
def api_positions():
    raw      = fetch_positions()
    enriched = enrich_positions(raw)
    state["positions"]   = enriched
    state["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return jsonify({"positions": enriched, "last_update": state["last_update"]})


@app.route("/api/positions/raw", methods=["GET"])
def api_positions_raw():
    """Datos crudos de la Data API para depurar problemas de avgPrice.

    Parámetro opcional ?q=<substring> filtra por título/token_id (insensible a mayúsculas).
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
            "token_id":     token_id,
            "title":        title,
            "outcome":      r.get("outcome") or r.get("side") or "",
            "size":         float(r.get("size") or 0),
            "avgPrice_api": float(r.get("avgPrice") or r.get("averagePrice") or 0),
            "curPrice_api": float(r.get("curPrice") or 0),
            "cached_avg":   cache.get(token_id),
            "redeemable":   bool(r.get("redeemable", False)),
        })
    return jsonify(out)


@app.route("/api/avg-price", methods=["POST"])
def api_set_avg_price():
    """Override manual del avg purchase price de una posición.

    Body: { token_id, avg_price }   (avg_price=null o 0 elimina el override)
    """
    data     = request.get_json(force=True)
    token_id = data.get("token_id")
    avg_raw  = data.get("avg_price")
    if not token_id:
        return jsonify({"ok": False, "error": "token_id requerido"})

    overrides = state["avg_price_overrides"]
    if avg_raw is None or avg_raw == "" or float(avg_raw or 0) <= 0:
        overrides.pop(token_id, None)
        log(f"[avg_price] Override eliminado para {token_id[:20]}…")
    else:
        val = round(float(avg_raw), 6)
        overrides[token_id] = val
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
    """Obtiene el precio real de ejecución desde la Data API y actualiza la fila en DB."""
    with _db_conn() as conn:
        row = conn.execute("SELECT * FROM closed_positions WHERE id=?", (row_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Registro no encontrado"})

    row      = dict(row)
    token_id = row.get("token_id", "")
    if not token_id:
        return jsonify({"ok": False, "error": "No hay token_id — venta registrada antes de este fix"})

    try:
        from datetime import datetime as _dt
        ts_dt  = _dt.strptime(row["ts"], "%Y-%m-%d %H:%M")
        min_ts = ts_dt.timestamp() - 120
    except Exception:
        min_ts = 0.0

    address    = state["credentials"].get("address", "").strip()
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
                    from datetime import datetime as _dt
                    t_ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
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

    size       = float(row["size"])
    avg_price  = float(row["avg_price"] or 0)
    close_type = row["type"]
    cost       = round(size * avg_price, 2)
    revenue    = round(
        size * fill_price * (1 - TAKER_FEE) if close_type != "canjeada" else size * fill_price,
        2,
    )
    profit = round(revenue - cost, 2)

    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "UPDATE closed_positions SET close_price=?, cost=?, revenue=?, profit=?, price_verified=1 WHERE id=?",
                (round(fill_price, 4), cost, revenue, profit, row_id)
            )

    log(f"[verify] Precio verificado para '{row['title'][:40]}': {row['close_price']:.4f} → {fill_price:.4f}")
    return jsonify({
        "ok":             True,
        "close_price":    round(fill_price, 4),
        "cost":           cost,
        "revenue":        revenue,
        "profit":         profit,
        "price_verified": True,
    })


@app.route("/api/positions/hidden", methods=["GET"])
def api_hidden_positions():
    result = []
    for token_id, meta in state["hidden_positions"].items():
        result.append({"token_id": token_id, **meta})
    for token_id in state["hidden_tokens"]:
        if token_id not in state["hidden_positions"]:
            result.append({"token_id": token_id, "title": token_id[:30] + "…",
                           "outcome": "", "size": 0, "avg_price": 0, "cost": 0, "reason": "?"})
    return jsonify(result)


@app.route("/api/positions/hidden/<token_id>/check-trade", methods=["GET"])
def api_hidden_check_trade(token_id):
    """Comprueba si hay algún trade de SELL o redención para este token en la Data API."""
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
            side = (t.get("side") or "").upper()
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
        pos   = next((p for p in state["positions"] if p["token_id"] == token_id), {})
        size  = float(pos.get("size") or 0)
        avg_p = pos.get("avg_price") or state["avg_price_cache"].get(token_id, 0)
        record_close(title, pos.get("outcome", ""), size, avg_p, 1.0, "canjeada", token_id)
        credit_budget(size, 1.0)
    return jsonify({"ok": ok, "error": msg if not ok else "", "tx": msg if ok else ""})


SLIPPAGE_WARN_ROUTE = SLIPPAGE_WARN


@app.route("/api/sell", methods=["POST"])
def api_sell():
    data     = request.get_json(force=True)
    token_id = data.get("token_id")
    size     = data.get("size")
    price    = data.get("price")
    force    = bool(data.get("force", False))
    floor_raw      = data.get("floor")
    floor_override = float(floor_raw) if floor_raw is not None else None
    if not token_id or not size:
        return jsonify({"ok": False, "error": "token_id y size requeridos"})
    size_f  = float(size)
    price_f = float(price) if price else 0.0

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

    ref_price = fresh if fresh > 0 else price_f
    pos       = next((p for p in state["positions"] if p["token_id"] == token_id), {})
    sell_ts   = time.time()
    ok, msg   = sell_position(token_id, size_f, ref_price if ref_price > 0 else None,
                              floor_override=floor_override)
    if ok:
        fill = fetch_fill_price(token_id, sell_ts) or price_f
        if fill > 0:
            credit_budget(size_f, fill)
        record_close(pos.get("title", token_id[:30]), pos.get("outcome", ""),
                     size_f, pos.get("avg_price") or 0, fill or price_f, "vendida", token_id)
    return jsonify({"ok": ok, "error": msg if not ok else ""})


# ─── Rutas API — Estadísticas ─────────────────────────────────────────────────

@app.route("/api/session", methods=["GET"])
def api_session():
    return jsonify({
        "balance": round(_fetch_usdc_balance(), 2),
        **_pnl_for_period("date(ts) = date('now')"),
    })


@app.route("/api/stats", methods=["GET"])
def api_stats():
    balance        = _fetch_usdc_balance()
    open_positions = [p for p in state.get("positions", []) if not p.get("sold")]
    open_value     = sum(p.get("value", 0) or 0 for p in open_positions)
    open_cost      = sum(p.get("cost",  0) or 0 for p in open_positions)
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


# ─── Rutas API — Bot propio ───────────────────────────────────────────────────

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
    return jsonify({
        "running":      state["bot_running"],
        "client_ready": state["client"] is not None,
        "last_update":  state.get("last_update"),
        "logs":         state["logs"][-30:],
    })


# ─── Rutas API — Copy Trading ─────────────────────────────────────────────────

@app.route("/api/copy/profiles", methods=["GET"])
def api_copy_profiles():
    profiles = list(state["copy_profiles"].values())
    safe     = [{k: v for k, v in p.items() if k != "last_seen_id"} for p in profiles]
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
    return jsonify({
        "mode":             s.get("mode", "fixed"),
        "fixed_amount":     s.get("fixed_amount", 1.0),
        "daily_budget":     s.get("daily_budget", 20.0),
        "min_price_filter": s.get("min_price_filter", 0.0),
        "spent_today":      round(spent, 2),
        "remaining":        round(remaining, 2),
    })


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
    """Reinicia el gasto de hoy a cero."""
    from datetime import date
    today = date.today().isoformat()
    with _db_lock:
        with _db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO daily_budget (date, spent) VALUES (?, 0.0)", (today,)
            )
    log("[budget] Gastado hoy reiniciado a $0")
    return jsonify({"ok": True, "remaining": round(get_remaining_budget(), 2)})


@app.route("/api/copy/trades", methods=["GET"])
def api_copy_trades():
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM copy_trades_log ORDER BY id ASC LIMIT 50"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/copy/trades", methods=["DELETE"])
def api_copy_clear_trades():
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
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        bal  = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
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
        from eth_account import Account
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        pk = state["credentials"].get("private_key", "")
        CTF_ADDRESS       = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        CTF_EXCHANGE      = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
        NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
        ERC1155_ABI = [{"inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
                        "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}],
                        "type": "function", "stateMutability": "view"}]
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
    """Envía transacciones on-chain de approve para que Polymarket pueda gastar USDC y tokens."""
    pk = state["credentials"].get("private_key", "")
    if not pk:
        return jsonify({"ok": False, "error": "No hay clave privada"})
    try:
        from eth_account import Account
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        POLYGON_RPC       = "https://polygon-bor-rpc.publicnode.com"
        USDC_ADDRESS      = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        CTF_ADDRESS       = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        CTF_EXCHANGE      = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
        NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
        MAX_UINT256       = 2**256 - 1

        ERC20_ABI   = [{"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
                        "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function",
                        "stateMutability": "nonpayable"}]
        ERC1155_ABI = [{"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
                        "name": "setApprovalForAll", "outputs": [], "type": "function",
                        "stateMutability": "nonpayable"}]

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

        send_tx(usdc.functions.approve(CTF_EXCHANGE, MAX_UINT256),        "USDC→CTF_Exchange")
        send_tx(usdc.functions.approve(NEG_RISK_EXCHANGE, MAX_UINT256),   "USDC→NegRisk_Exchange")
        send_tx(ctf.functions.setApprovalForAll(CTF_EXCHANGE, True),      "CTF_tokens→CTF_Exchange")
        send_tx(ctf.functions.setApprovalForAll(NEG_RISK_EXCHANGE, True), "CTF_tokens→NegRisk_Exchange")

        client = state.get("client")
        if client:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
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
    return jsonify({
        "running":          state["copy_running"],
        "profile_count":    sum(1 for p in state["copy_profiles"].values() if p.get("active")),
        "spent_today":      round(spent, 2),
        "daily_budget":     s.get("daily_budget", 20.0),
        "remaining_budget": round(get_remaining_budget(), 2),
    })


# ─── Rutas Auth ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    with _db_conn() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key='password_hash'").fetchone()
    has_password = row is not None
    error = None

    if request.method == "POST":
        ip = request.remote_addr or "unknown"

        if not has_password:
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
                m, s_sec = divmod(wait, 60)
                error = f"Demasiados intentos. Espera {m}m {s_sec}s"
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
                    remaining = get_remaining_attempts(ip)
                    error = f"Contraseña incorrecta — {max(0, remaining)} intentos restantes"

    return render_template("login.html", has_password=has_password, error=error)


@app.route("/api/auth/change-password", methods=["POST"])
def api_change_password():
    data    = request.get_json(force=True, silent=True) or {}
    current = data.get("current", "")
    new_pw  = data.get("new", "")
    confirm = data.get("confirm", "")

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


# ─── Apagado limpio ───────────────────────────────────────────────────────────

_shutdown_event = threading.Event()


def _graceful_shutdown(signum=None, frame=None) -> None:  # noqa: ARG001
    """Detiene los hilos de bots antes de que el proceso termine."""
    if _shutdown_event.is_set():
        return
    _shutdown_event.set()
    log("[app] Señal de apagado recibida — deteniendo bots...")
    state["bot_running"] = False
    state["copy_running"] = False
    for attr in ("bot_thread", "copy_thread"):
        t = state.get(attr)
        if t and t.is_alive():
            t.join(timeout=3)
    log("[app] Apagado limpio completado")


atexit.register(_graceful_shutdown)

for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, _graceful_shutdown)
    except (OSError, ValueError):
        pass
if hasattr(signal, "SIGBREAK"):
    try:
        signal.signal(signal.SIGBREAK, _graceful_shutdown)  # type: ignore[attr-defined]
    except (OSError, ValueError):
        pass


# ─── Arranque ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_file_logging()
    init_db()
    migrate_from_json()
    load_from_db()
    load_telegram_config()
    if state["credentials"].get("private_key"):
        init_client()
    app.secret_key = _get_or_create_secret_key()
    app.permanent_session_lifetime = timedelta(hours=12)
    print("Abriendo en http://localhost:5000")

    def _open_browser():
        time.sleep(1.5)
        import webbrowser
        webbrowser.open("http://localhost:5000")

    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
