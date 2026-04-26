# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- File logging: all `log()` output is mirrored to `polymarket.log` via `RotatingFileHandler` (5 MB, 3 backups)
- Graceful shutdown on `SIGTERM` / `SIGINT` / `SIGBREAK` — bot threads stop cleanly
- Telegram notifications on position close (sell, redeem, loss) via `notifier.py`
- `/api/config/telegram` endpoint and UI card in Configuración section
- `/api/health` liveness probe (no auth required)
- `pyproject.toml` with ruff lint config (E/W/F/I rules, 120-char line length)
- `requirements.in` as editable dependency source; `requirements.txt` fully pinned via pip-compile
- `.pre-commit-config.yaml` running ruff lint + format on commit
- CI lint job (`.github/workflows/lint.yml`) running `ruff check .` on every push and PR

---

## [0.10.0] — 2026-04-24

### Added
- Persistent header bar with live portfolio value, USDC balance, session P&L, win/loss counters
- Sidebar navigation with section routing persisted to `localStorage`

---

## [0.9.0] — 2026-04-24

### Added
- Login system with CSRF protection, bcrypt password hashing, change-password UI
- Rate limiting: 5 failed attempts → 15-minute lockout per IP
- `start.bat` launcher for one-click startup

---

## [0.8.0] — 2026-04-18

### Added
- Slippage protection on manual sell: warns if price drops >8% before executing
- Parallel `get_best_bid` calls in `enrich_positions` (ThreadPoolExecutor)
- TTL cache for hidden-position recovery checks
- Threaded Flask + paginated history (10 rows, load-more)

---

## [0.7.0] — 2026-04-18

### Added
- SQLite persistence: all state stored in `polymarket.db` (WAL mode)
- P&L breakdown by period (today, week, month, all-time)
- Fill-price seeding: avg price confirmed from real trade history
- UI refactor: sortable history table, sidebar sections

### Fixed
- P&L `None` crash when avg price is unavailable
- Copy trading budget not credited on manual redeem

---

## [0.6.0] — 2026-04-17

### Added
- Copy-trading bot: mirrors BUY and SELL trades from tracked profiles
- Daily budget cap with proportional or fixed bet sizing
- Duplicate-buy protection via `copy_positions` dict
- Auto-hide positions with `current_price < 0.01` (lost markets)
- Near-resolved auto-sell at `current_price >= 0.95`

---

## [0.5.0] — 2026-04-17

### Added
- On-chain redemption via `redeemPositions()` on Conditional Tokens contract
- USDC approvals endpoint for CTF Exchange + Neg Risk CTF Exchange
- Polygon RPC integration (web3.py)
