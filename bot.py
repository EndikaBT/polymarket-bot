"""
bot.py — Bot de venta propio.

Contiene toda la lógica de posiciones: enriquecimiento, venta, canje,
seeding de avg_price desde historial de fills y el loop principal.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from db import (
    _add_redeemed,
    _db_conn,
    _db_lock,
    _delete_copy_position,
    _delete_hidden,
    _upsert_hidden,
    credit_budget,
    purge_settled_losses,
    record_close,
    save_config,
)
from state import (
    CLOB_HOST,
    DATA_HOST,
    DUST_THRESHOLD,
    HIDDEN_RECOVERY_TTL,
    SELL_PRICE_FLOOR_PCT,
    log,
    state,
)

# ─── Cliente CLOB ─────────────────────────────────────────────────────────────


def init_client() -> bool:
    try:
        from eth_account import Account
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.constants import POLYGON

        pk = state["credentials"].get("private_key", "")
        if not pk:
            log("No hay clave privada configurada.")
            return False

        signer_addr    = Account.from_key(pk).address
        configured_addr = (state["credentials"].get("address") or "").strip()
        if configured_addr and configured_addr.lower() != signer_addr.lower():
            funder   = configured_addr
            sig_type = 2
        else:
            funder   = None
            sig_type = 0

        client = ClobClient(CLOB_HOST, key=pk, chain_id=POLYGON,
                            signature_type=sig_type, funder=funder)
        client.set_api_creds(client.create_or_derive_api_key())
        state["client"] = client
        log(f"Cliente CLOB listo | signer={signer_addr} | funder={funder or signer_addr} | sig_type={sig_type}")
        return True
    except Exception as e:
        log(f"Error iniciando cliente CLOB: {e}")
        state["client"] = None
        return False


# ─── Posiciones propias ───────────────────────────────────────────────────────

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
                break
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


def enrich_positions(raw_positions: list) -> list:
    now = time.time()

    # ── Paso 1: parsear metadatos, decidir qué tokens necesitan consulta de precio ──
    parsed    = []
    fetch_ids = []

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

        # Resolución de avgPrice — orden de prioridad:
        # 0. Override manual (avg_price_overrides) — siempre gana.
        # 1. fill_seeded: precio confirmado desde historial real; solo se actualiza hacia arriba.
        # 2. Dentro de los 90 s de un copy buy el valor de la Data API puede estar corrupto.
        # 3. En otro caso, la Data API es autoritativa.
        override = state["avg_price_overrides"].get(token_id)
        if override and override >= 0.001:
            avg_price          = override
            avg_price_reliable = True
        else:
            cache       = state["avg_price_cache"]
            fill_seeded = token_id in state["fill_seeded"]
            copy_entry  = state["copy_positions"].get(token_id)
            bought_at   = copy_entry.get("bought_at", 0) if copy_entry else 0
            in_copy_win = bought_at and (now - bought_at) < 90

            if token_id not in state["known_positions"]:
                state["known_positions"].add(token_id)
                # Solo hacer seeding para copy trades recientes (bought_at conocido).
                # Las posiciones manuales ya tienen avgPrice estable en la Data API.
                if not fill_seeded and bought_at:
                    threading.Thread(
                        target=_seed_avg_price_from_fill,
                        args=(token_id, 0, bought_at),
                        daemon=True,
                    ).start()

            if fill_seeded or in_copy_win:
                if avg_price_api >= 0.01 and avg_price_api > cache.get(token_id, 0.0):
                    cache[token_id] = avg_price_api
            else:
                if avg_price_api >= 0.01:
                    cache[token_id] = avg_price_api

            avg_price          = cache.get(token_id, avg_price_api)
            avg_price_reliable = avg_price >= 0.01

        title = (
            raw.get("title") or raw.get("question") or
            raw.get("slug") or (f"{token_id[:20]}…" if token_id else "Desconocido")
        )

        entry = {
            "token_id":           token_id,
            "size":               size,
            "avg_price":          avg_price,
            "avg_price_reliable": avg_price_reliable,
            "cur_price_api":      cur_price_api,
            "title":              title,
            "outcome":            raw.get("outcome") or raw.get("side") or "",
            "redeemable":         bool(raw.get("redeemable", False)),
            "condition_id":       raw.get("conditionId", ""),
            "outcome_index":      int(raw.get("outcomeIndex", 0)),
            "is_hidden":          False,
            "hidden_meta":        None,
        }

        if token_id in state["hidden_tokens"]:
            meta   = state["hidden_positions"].get(token_id, {})
            reason = meta.get("reason", "")
            if reason != "perdida":
                continue  # oculto manualmente — ignorar completamente
            last = state["_hidden_check_ts"].get(token_id, 0.0)
            if now - last < HIDDEN_RECOVERY_TTL:
                continue
            entry["is_hidden"]   = True
            entry["hidden_meta"] = meta
            fetch_ids.append(token_id)
        else:
            fetch_ids.append(token_id)  # siempre consultar precio en vivo

        parsed.append(entry)

    # ── Paso 2: obtener todos los precios en paralelo ──────────────────────────
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

    # ── Paso 3: procesar resultados ───────────────────────────────────────────
    TAKER_FEE = 0.02
    result = []
    for entry in parsed:
        token_id           = entry["token_id"]
        live_price         = prices.get(token_id, 0.0)
        size               = entry["size"]
        avg_price          = entry["avg_price"]
        avg_price_reliable = entry["avg_price_reliable"]
        title              = entry["title"]
        outcome            = entry["outcome"]
        cur_price_api      = entry["cur_price_api"]

        if entry["is_hidden"]:
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
            else:
                continue  # sigue sin valor

        current = live_price if live_price > 0 else cur_price_api

        # Solo ocultar como "perdida" si tenemos precio confirmado (live_price > 0) y es < 0.01.
        # Si live_price == 0 pero cur_price_api también == 0 podría ser lag de la API:
        # en ese caso no ocultamos para evitar falsos negativos en posiciones recién compradas.
        price_confirmed = live_price > 0 or cur_price_api > 0
        if price_confirmed and current < 0.01:
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

        if avg_price_reliable and avg_price > 0:
            pnl_pct = (current - avg_price) / avg_price * 100
        else:
            pnl_pct = None

        cost       = round(size * avg_price, 2) if avg_price_reliable else None
        sell_value = round(size * current * (1 - TAKER_FEE), 2)
        net_profit = round(sell_value - cost, 2) if cost is not None else None

        target       = state["profit_targets"].get(token_id)
        already_sold = token_id in state["sold_tokens"]

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


# ─── Fill price desde historial ───────────────────────────────────────────────

def fetch_fill_price(token_id: str, min_ts: float, retries: int = 3, side: str = "SELL") -> float:
    """Consulta la Data API para el precio real de ejecución del trade más reciente
    del lado indicado (BUY o SELL) sobre token_id, posterior a min_ts.
    Devuelve 0.0 si no se encuentra."""
    address = state["credentials"].get("address", "").strip()
    if not address:
        return 0.0
    for _ in range(retries):
        try:
            time.sleep(1.5)
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
                ts_raw = t.get("timestamp") or t.get("createdAt") or t.get("ts") or ""
                try:
                    t_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
                except Exception:
                    try:
                        t_ts = float(ts_raw)
                    except Exception:
                        t_ts = 0.0
                if t_ts < min_ts - 5:
                    continue
                price = float(t.get("price") or 0)
                if price > 0:
                    log(f"[fill] Precio de ejecución real ({side}): {price:.4f}")
                    return price
        except Exception:
            pass
    return 0.0


# ─── Venta ────────────────────────────────────────────────────────────────────

def sell_position(token_id: str, size: float, price: float | None = None,
                  floor_override: float | None = None) -> tuple[bool, str]:
    """Vende shares via market order (FOK).

    `price` se usa como precio de referencia para el floor: la orden solo
    se ejecuta si el precio medio ≥ price * (1 - SELL_PRICE_FLOOR_PCT).
    `floor_override` (≥ 0) fija el floor directamente.
    """
    client = state.get("client")
    if not client:
        return False, "Cliente CLOB no inicializado"
    try:
        from py_clob_client_v2.clob_types import MarketOrderArgsV2 as MarketOrderArgs
        from py_clob_client_v2.clob_types import OrderType

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
        signed    = client.create_market_order(order_args)
        resp      = client.post_order(signed, OrderType.FOK)
        resp_dict = resp if isinstance(resp, dict) else (resp.__dict__ if hasattr(resp, "__dict__") else {})
        status    = str(resp_dict.get("status", "")).lower()

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


# ─── Canje on-chain ───────────────────────────────────────────────────────────

def redeem_position(token_id: str, title: str,
                    condition_id: str = "", outcome_index: int = -1) -> tuple[bool, str]:
    """Canjea una posición ganadora resuelta via el contrato CTF en Polygon."""
    pk = state["credentials"].get("private_key", "")
    if not pk:
        return False, "No hay clave privada"
    try:
        from eth_account import Account
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

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

        w3   = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        acct = Account.from_key(pk)

        CTF_ADDRESS  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        USDC_ADDRESS = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")  # pUSD (V2)
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

        ctf       = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
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


# ─── Auto-canje ───────────────────────────────────────────────────────────────

def check_and_redeem():
    """Encuentra una posición canjeable y la canjea. Salta si ya hay un canje en curso."""
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
                avg_p = pos.get("avg_price") or state["avg_price_cache"].get(token_id, 0)
                size  = pos.get("size", 0)
                record_close(pos["title"], pos.get("outcome", ""),
                             size, avg_p, 1.0, "canjeada", token_id)
                credit_budget(size, 1.0)
                _delete_copy_position(token_id)
                log(f"[redeem] ✓ Registrado en historial: {pos['title'][:45]}")
        finally:
            state["_redeeming"] = False
        break


# ─── Seeding de avg_price ─────────────────────────────────────────────────────

def _seed_avg_price_from_fill(token_id: str, amount_usdc: float, buy_ts: float) -> None:
    """Consulta el precio real de ejecución justo después de un BUY y lo guarda en caché.

    Se ejecuta en un hilo de fondo para no bloquear el loop de copy.
    En caso de éxito añade token_id a state["fill_seeded"] para que
    enrich_positions no deje que la Data API sobreescriba el precio confirmado.
    """
    try:
        fill = fetch_fill_price(token_id, buy_ts, retries=5, side="BUY")
        if fill and fill > 0:
            state["avg_price_cache"][token_id] = fill
            state["fill_seeded"].add(token_id)
            log(f"[avg_price] Seeded from fill (BUY): {token_id[:20]}… → {fill:.4f}")
        else:
            log(f"[avg_price] No fill price available for {token_id[:20]}…, "
                "Data API value will be used when it stabilises")
    except Exception as e:
        log(f"[avg_price] Error seeding from fill for {token_id[:20]}…: {e}")


# ─── Balance USDC on-chain ────────────────────────────────────────────────────

def _fetch_usdc_balance() -> float:
    """Devuelve el balance on-chain de USDC.e de la wallet configurada."""
    try:
        from eth_account import Account
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        pk      = state["credentials"].get("private_key", "")
        address = state["credentials"].get("address", "")
        if not pk:
            return 0.0
        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if not address:
            address = Account.from_key(pk).address
        addr = Web3.to_checksum_address(address)
        USDC = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")  # pUSD (V2)
        abi  = [{"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
                 "outputs": [{"name": "", "type": "uint256"}], "type": "function",
                 "stateMutability": "view"}]
        return w3.eth.contract(address=USDC, abi=abi).functions.balanceOf(addr).call() / 1_000_000
    except Exception as e:
        log(f"[balance] Error consultando USDC on-chain: {e}")
        return 0.0


# ─── Loop principal ───────────────────────────────────────────────────────────

def bot_loop():
    log("Bot iniciado — revisando posiciones cada 30 s.")
    while state["bot_running"]:
        try:
            raw      = fetch_positions()
            enriched = enrich_positions(raw)
            state["positions"]   = enriched
            state["last_update"] = datetime.now().isoformat()
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
