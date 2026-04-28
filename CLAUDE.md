# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Permissions

- Make any code change and push it to a PR without asking for permission first.
- **Never merge to `main`** without explicit user approval.
- Don't ask for confirmation before editing files, creating branches, or opening PRs.

## Git workflow

**Never push directly to `main`.** Always create a feature branch and open a PR:
```bash
git checkout -b feat/my-change
# make changes
git add <files>
git commit -m "description"
git push -u origin feat/my-change
gh pr create --title "..." --body "..."
```

### Commits and PRs — attribution

- **Do not** add `Co-Authored-By` trailers to commits.
- **Do not** add `🤖 Generated with Claude Code` footers to PR bodies.
- **Do not** mention Claude, AI, or any tool in commit messages, PR titles, or PR bodies.
- Write commits and PR descriptions as if the repository owner wrote them directly.

## Running the app

```bash
pip install -r requirements.txt
python app.py
```

Opens on `http://localhost:5000`. There are no tests and no build step.

## Architecture

Five-module Flask app. `state.py` holds the shared in-memory dict; everything persists to `polymarket.db` (SQLite) via `save_config()` / `load_from_db()`.

| File | Responsibility |
|------|---------------|
| `state.py` | Shared `state` dict, constants (`CLOB_HOST`, `TAKER_FEE`, …), `log()` |
| `db.py` | SQLite helpers, schema, migration from config.json, `record_close`, `credit_budget` |
| `auth.py` | CSRF tokens, rate limiting, `check_auth` / `security_headers` Flask hooks |
| `bot.py` | `init_client`, `fetch_positions`, `enrich_positions`, `sell_position`, `redeem_position`, `bot_loop` |
| `copy_bot.py` | Profile resolution, activity polling, `execute_copy_trade`, `copy_trade_loop` |
| `app.py` | Flask `app`, all `@app.route` handlers, startup block |

Import chain (no circular deps): `state ← db ← auth ← bot ← copy_bot ← app`

### Two independent bots, one shared CLOB client

**Sell bot** (`bot_loop` thread) — polls the user's own positions every 1 s. Sells via market FOK order when a manually set profit target is hit, and sells immediately when `current_price >= 0.95` (near-resolved winner). Auto-hides positions with `current_price < 0.01` (lost markets).

**Copy-trading bot** (`copy_trade_loop` thread) — polls each tracked profile's activity every 1 s. Mirrors BUY trades (with budget cap) and SELL trades. Pauses BUYs when daily budget < $1 but keeps polling to mirror SELLs. Duplicate-buy protection via `copy_positions` dict.

### Key state fields
- `sold_tokens` / `redeemed_tokens` / `hidden_tokens` — sets, persisted as lists in config
- `copy_positions` — `{token_id: {size, market, profile, bought_at}}` — positions opened by copy bot; new copy buys are skipped in sell-bot for 60 s (Data API avgPrice sync delay)
- `copy_settings.spent_today` — reduced by `credit_budget()` whenever any position is sold

### External APIs
- `clob.polymarket.com` — order placement and live prices (`/price?side=SELL`)
- `data-api.polymarket.com` — user positions and activity feed
- `gamma-api.polymarket.com` — market metadata for on-chain redemption (conditionId, clobTokenIds)
- Polygon RPC: `https://polygon-bor-rpc.publicnode.com` (polygon-rpc.com returns 403)

### On-chain interactions (web3.py)
- **pUSD approvals** (`/api/copy/approve`): ERC20 `approve()` to CTF Exchange + Neg Risk CTF Exchange, plus ERC1155 `setApprovalForAll()` on Conditional Tokens contract (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`) to both exchanges — required before any trade
- **Redemption** (`redeem_position()`): calls `redeemPositions()` on the CTF contract; looks up `conditionId` and outcome index from Gamma API first

### Wallet setup (Polymarket V2)
- pUSD (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`) on Polygon — replaced USDC.e in the V2 migration
- CTF Exchange V2: `0xE111180000d2663C0091e4f400237545B87B996B`
- Neg Risk Exchange V2: `0xe2222d279d744050d28e00520010520000310F59`
- CTF Contract (unchanged): `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- `signature_type=0` (EOA) when private key derives to the same address as configured; `signature_type=2` (proxy) otherwise — auto-detected in `init_client()`
- `sell_position()` and `execute_copy_trade()` use `MarketOrderArgsV2` + `OrderType.FOK` (py-clob-client-v2)

### UI
Single-page Bootstrap 5 dark theme (`templates/index.html`). No framework — vanilla JS with `setInterval` polling (`/api/bot/status` every 5 s, positions every 5 s, copy status every 3 s). SSE is not used.
