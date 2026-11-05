# CLAUDE.md

Orientation for Claude Code sessions in this repo. The user-facing overview (rules for entries,
exits, sizing, setup, commands) is in `README.md`; read it too. This file covers how the code
fits together and what to watch out for.

## What this is

An autonomous long/short **paper**-trading agent on Windows.

- **TradingAgents decides.** The [TradingAgents](https://github.com/TauricResearch/TradingAgents)
  multi-agent LLM framework (pip dependency, Anthropic API key from `.env`) analyzes each stock
  and returns a rating (Buy / Overweight / Hold / Underweight / Sell), entry/stop/target and an
  executive summary.
- **Python plans.** `TradingAgent.plan()` turns those decisions into *intents*: the exact Alpaca
  MCP calls to make, written to a plan JSON. Python **never places orders**: `alpaca.py` is a
  read-only REST client used for syncing.
- **Claude executes.** The `/trade` skill (`.claude/skills/trade/SKILL.md`) reads a plan and makes
  those MCP calls verbatim. The agent runs it headless: `claude -p "/trade <plan path>" --permission-mode auto`.
- **Sync** reconciles the SQLite journal with Alpaca's real orders (fills, exits, P&L).

Keep that split. Any new trading behavior goes into `plan()` as an intent. It never goes into
Python order calls or into the skill's judgment.

## Layout (`src/trading_journey/`)

| File | Role |
|---|---|
| `config.py` | `Settings` from `.env` (models, scheduling, risk, SMTP, data dir) and the TradingAgents config |
| `decision.py` | Runs TradingAgents for one ticker, parses its output into `Decision`. `BULLISH`/`BEARISH` rating sets. `recommends_short()` |
| `agent.py` | `TradingAgent`: `plan()` (screen, analyze in parallel, `_decide` per ticker, intents) and `sync()` |
| `runner.py` | The long-running `trading-journey agent` process: daily scan at `SCAN_TIME` (NY), hourly housekeeping, sync every 5 min, launches headless Claude, hosts the dashboard |
| `screener.py` | Morning scoring of the universe (relative volume, gap/ATR, trend, earnings), with a long/short `bias` |
| `scoring.py` | Per-ticker track record, scan priority, the order calls get capital in |
| `sizing.py` | Stop/target sanity rules (`finalize_levels`) and position sizing |
| `store.py` | SQLite journal: `decisions`, `entries`, `trades`, `equity_snapshots`, `runs`, `kv`. Columns added later go in `_migrate()` |
| `dashboard.py` + `web/index.html` | Dashboard: `build_state()` → `/api/state` JSON, plus a single static page (no build step, re-read on every request) |
| `usage.py` | LangChain callback counting tokens per model. `PRICES` ($/Mtok) gives each decision's `cost` |
| `market_data.py` | yfinance prices/ATR, market calendar |
| `universe.py` | ~50 default tickers with sectors (override: `universe.yaml`) |
| `notify.py` | SMTP alert email (out-of-credit) |
| `cli.py` | `trading-journey` entry point: `agent`, `dashboard`, `plan`, `sync`, `status`, `screen`, `report`, `test-email` |

## Flow of one day

1. **09:45 NY:** `runner` calls `plan(scan=True)`. The screener ranks the universe. TradingAgents
   analyzes held positions first, then candidates up to `MAX_ANALYSES` in all (10: 8 held → 2
   candidates; this cap is what bounds the API cost), `SCAN_WORKERS` at a time (each
   ~5 min). All analyses finish, then `_decide` runs in priority order and builds intents.
2. If the plan has intents, `runner` starts headless Claude with `/trade <plan>`. Claude places
   the orders, then runs `sync` and `status`.
3. **Every other market hour:** `plan(scan=False)` does housekeeping without an LLM (trailing stop,
   lost-stop `protect`, time exits, expired limit entries).
4. Each plan's id is `YYYYMMDDHHMM-xxxx` (UTC). Decisions, entries and runs carry `plan_id`, so a
   scan's API cost is `SUM(decisions.cost) GROUP BY plan_id` (`Store.scan_costs`).

Intent kinds: `buy`, `short`, `exit`, `raise_stop`, `update_target`, `protect`, `cancel_entry`.
Every entry uses `client_order_id = tj-<plan_id>-<ticker>`, so sync finds it even if Claude never
reports back.

## Rules that are easy to break

- **Sell/Underweight is not automatically a short.** On a stock we don't hold, the portfolio
  manager almost always means "stay out". A short is planned only if `recommends_short(summary)`
  is true, meaning shorting is mentioned and never negated. It's deliberately biased toward no
  short: a missed trade is cheap, an unwanted short isn't.
- **Bracket legs are GTC**, even for market entries. Day legs would expire and leave a position
  unprotected.
- **Kill switch:** a `STOP` file in the data dir halts every plan. Never work around it.
- **Live trading** needs `ALPACA_PAPER_TRADE=false` *and* `LIVE_CONFIRM=I_UNDERSTAND`. Don't flip it.
- **Never commit `.env`.** It holds the Anthropic, Alpaca and SMTP secrets. Don't print it
  either: grep it for setting names only.
- In `/trade`, never call `close_all_positions`, `cancel_all_orders`, crypto/option tools or
  `update_account_config`.

## Where things live

- **Data dir** `%LOCALAPPDATA%\trading-journey\` (kept outside OneDrive, which corrupts SQLite):
  `trading_journey.sqlite3`, `agent.log`, `runner.out.log`/`runner.err.log`, `plans\*.json`,
  `runs\*` (headless Claude logs), and the `STOP` file. Override with `TJ_DATA_DIR`.
- **Windows username is in Hebrew** (`מאור סבן`). Quote paths, and expect mojibake in some
  console output. Prefer `%LOCALAPPDATA%` / `os.path.expandvars` to typing the path.
- **The agent** runs from a scheduled task, "trading-journey agent" (`scripts/install-autostart.ps1`
  → `scripts/start-agent.ps1`), one minute after login.
- **Dashboard:** http://127.0.0.1:8050. `/api/state`, `/api/plan`, `/api/decision`, `/api/run-log`
  (GET). `/api/kill`, `/api/sync` (POST, from this PC with a localhost Origin only).
  `DASHBOARD_SHARE=tailscale` also serves GETs to Tailscale addresses (100.64.0.0/10), and
  `client_allowed()` turns away every other address: there's no login, so keep it that way.
  `trading-journey dashboard --remote http://<pc>:8050` serves the local `index.html` with another
  PC's `/api/*` data (read-only, no `.env`), for working on the page (README, "Sharing the dashboard").

## Working on the code

- **Install:** editable (`pip install -e .[dev]` into `.venv`), so source edits are what runs.
  - The running agent only picks up Python changes and `.env` changes when restarted:
    `Get-Process trading-journey | Stop-Process`, then `Start-ScheduledTask -TaskName "trading-journey agent"`.
    Don't restart during the 09:45 NY scan or while a headless Claude run is placing orders.
  - Changes to `web/index.html` show on a browser refresh.
- **Tests:** `.venv/Scripts/python.exe -m pytest -q`. They're fully offline. `tests/conftest.py`
  has a fake Alpaca (`alpaca.execute(intent)` simulates Claude placing a plan), a `make_agent`
  fixture with canned decisions, `decision(...)` and `run_cycle(...)`. Bearish `decision()`
  fixtures default to a summary that asks for a short. Add tests next to the feature
  (`test_shorts.py`, `test_dashboard.py`, `test_usage.py`, ...).
- **Real data:** the journal can be read for checks:
  `Store(SETTINGS.db_path)` from `trading_journey.store` / `trading_journey.config`.
  Read only; don't write to it from ad-hoc scripts.
- **A real analysis without trading:** `trading-journey plan --ticker NVDA --dry-run --force`.
  It costs API money.
- **Cost:** TradingAgents has two model tiers. `TA_DEEP_MODEL` (research and portfolio managers,
  the crucial calls) is `claude-opus-5-5`. `TA_QUICK_MODEL` (analysts, debaters, trader: ~15 calls,
  ~95% of the tokens) is `claude-haiku-5-5`. That's about $0.13 per analysis, ~$1.30 per scan of
  10 (`MAX_ANALYSES`), ~$27 a month. The user chose this: Opus on the crucial calls, the cheap
  model on the groundwork (Sonnet there is ~$0.80 per analysis). Keep `usage.PRICES`
  current when adding models. Unknown models are priced as Opus.
- **Git:** work happens on `master`, pushed to `origin` (github.com/maor2030/trading-journey).
  `main` only holds the initial commit. Commit or push only when asked. Pull first.
- **Style:** match the existing code. Short docstrings that say *why*. Settings go through
  `Settings` + `.env.example` + README.
