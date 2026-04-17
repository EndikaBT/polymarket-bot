# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

Single-file Flask app (`app.py`) with an in-memory `state` dict as the source of truth. Everything persists to `config.json` on disk via `save_config()` / `load_config()`.

### Two independent bots, one shared CLOB client

**Sell bot** (`bot_loop` thread) — polls the user's own positions every 1 s. Auto-assigns a random 25–30% profit target to new positions, sells via market FOK order when target is hit, and sells immediately when `current_price >= 0.95` (near-resolved winner). Auto-hides positions with `current_price < 0.01` (lost markets).

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
- **USDC approvals** (`/api/copy/approve`): ERC20 `approve()` to CTF Exchange + Neg Risk CTF Exchange, plus ERC1155 `setApprovalForAll()` on Conditional Tokens contract (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`) to both exchanges — required before any trade
- **Redemption** (`redeem_position()`): calls `redeemPositions()` on the CTF contract; looks up `conditionId` and outcome index from Gamma API first

### Wallet setup
- USDC.e (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`) on Polygon — not native USDC
- `signature_type=0` (EOA) when private key derives to the same address as configured; `signature_type=2` (proxy) otherwise — auto-detected in `init_client()`
- `sell_position()` and `execute_copy_trade()` use `MarketOrderArgs` + `OrderType.FOK`

### UI
Single-page Bootstrap 5 dark theme (`templates/index.html`). No framework — vanilla JS with `setInterval` polling (`/api/bot/status` every 5 s, positions every 5 s, copy status every 3 s). SSE is not used.
