# trading-journey

An autonomous **long/short** paper-trading agent, run from a Claude Code session.

- **TradingAgents decides.** [TradingAgents](https://github.com/TauricResearch/TradingAgents), a multi-agent LLM framework, analyzes each stock with your Anthropic API key.
- **Claude executes.** Your Claude Code session places the orders through the [Alpaca MCP](https://alpaca.markets/mcp-server) on an Alpaca paper account.

> For research and education only. This is not financial advice. Run it on paper and measure it before you consider real money.

## How it runs

`trading-journey agent` is a background process (start it with `/trade-agent` in Claude Code).

```
scan     →  once a day at 09:45 New York (16:45 Israel): screens the universe, runs
            TradingAgents (Anthropic API key) on the held positions and the best-screened
            candidates, 3 at a time, then gives the free slots to the strongest calls first
hourly   →  housekeeping, no LLM: trailing stops, lost stop-losses, time exits,
            expired limit entries
claude -p "/trade <plan>" --permission-mode auto
         →  only when a plan has orders: a headless Claude Code session (your Claude
            account) places them through the Alpaca MCP
sync     →  journals fills, exits and P&L from Alpaca's real orders (every 5 minutes)
```

If the PC wakes up late, the scan runs at the first chance, unless the close is less than 90 minutes away.

It also serves the **dashboard at http://127.0.0.1:8050**:
- account KPIs, the equity curve and sector exposure;
- positions, with stop→target progress and whether each stop order is live at Alpaca;
- waiting entries and the next tickers to be scanned;
- every analysis, with TradingAgents' full reasoning;
- closed trades, the leaderboard and analytics by rating, exit reason and sector;
- each agent cycle with its plan and Claude's output, plus logs and settings;
- a Stop/Resume (kill switch) button.

| | |
|---|---|
| **Universe** | ~50 liquid, heavily covered US stocks across sectors (`src/trading_journey/universe.py`). Override it with `universe.yaml` (`TICKER: Sector`). |
| **Screener** | Each morning, every stock is scored: activity (relative volume, the opening gap in ATRs), trend (relative to the 50-day average, 20-day momentum; an uptrend scores as a long setup, a downtrend as a short setup) and earnings (just reported is a plus; reporting within 2 days is avoided). See it with `trading-journey screen` or on the dashboard. |
| **Scan order** | All held positions are re-analyzed every morning. Then up to 15 candidates (3 per free slot) by screener score plus track record, skipping any analyzed in the last 20h. With no free slot, only held positions are analyzed. |
| **Choosing trades** | All analyses finish before any order is planned. Calls get slots and cash in order: Buy/Sell before Overweight/Underweight, then the agents' reward:risk, then the screener score. |
| **Entries** | **Buy** or **Overweight** opens a long. **Sell** or **Underweight** opens a short, if the stock is easy to borrow at Alpaca (`ALLOW_SHORTS=false` turns shorts off). Each entry is a GTC **bracket order** at Alpaca. It's a limit order (waits up to 3 trading days) when TradingAgents asks for a better price than the market (lower for a long, higher for a short), and a market order otherwise. |
| **Stop-loss / take-profit** | Bracket legs held **at Alpaca**, so they work even when Claude is closed. Levels come from TradingAgents, sanity-checked: the stop must be 2–15% from the entry on the losing side (below for a long, above for a short), else 2×ATR is used, and reward:risk must be ≥ 1.5. |
| **Exits** | Stop-loss or take-profit fills, a **trailing stop** (moved to breakeven at +1R and kept 1R behind the best price from +2R, checked each hour), the **time horizon**, or a re-analysis that turns against the position (Sell/Underweight for a long, Buy/Overweight for a short). |
| **Sizing** | Each trade risks 1% of equity × rating (Buy/Sell 1.0, Overweight/Underweight 0.6) × the stock's track record (0.5–1.5). Caps: 15% per position, 30% per sector, a 10% cash reserve and at most 10 positions. |
| **Learning** | The per-ticker win rate, average R and TradingAgents' settled alpha raise or lower a stock's scan priority. Losers (≥5 trades, negative average R) are only re-scanned weekly. |
| **Lost stop-loss** | Each sync checks that every position's stop order is still live at Alpaca. If one disappears, the next cycle places a new GTC stop, or takes profit if the price is already past the target. |
| **Safety** | A daily −3% drawdown pauses new buys. The `STOP` kill switch halts every cycle. Live trading needs `ALPACA_PAPER_TRADE=false` **and** `LIVE_CONFIRM=I_UNDERSTAND`. |

## Setup (Windows)

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
copy .env.example .env   # ANTHROPIC_API_KEY, SEC_EDGAR_USER_AGENT, ALPACA_API_KEY/SECRET
```

Register the Alpaca MCP for this folder. It needs [uv](https://docs.astral.sh/uv/):

```powershell
claude mcp add alpaca --scope local --transport stdio -e ALPACA_API_KEY=... -e ALPACA_SECRET_KEY=... -e ALPACA_PAPER_TRADE=true -- uvx alpaca-mcp-server
```

## Running it

From this folder, start `claude` and run:

- `/trade-agent` starts the background agent and dashboard, or reports on them if they're already running.
- `/trade` runs one cycle now, inside your session.

To stop the agent completely, run `Get-Process trading-journey | Stop-Process`. Your stop-loss and take-profit orders stay active at Alpaca.

**Start on login:** `scripts\install-autostart.ps1` registers a Windows scheduled task, "trading-journey agent". The task starts the agent hidden one minute after you log in and restarts it if it crashes. Remove it with `Unregister-ScheduledTask -TaskName "trading-journey agent" -Confirm:$false`.

You can also run the Python side by hand:

```powershell
trading-journey agent                     # the autonomous agent + dashboard (what /trade-agent starts)
trading-journey dashboard                 # only the dashboard
trading-journey status                    # account, positions, pending entries, today's screen
trading-journey screen                    # run the screener now and print the ranking
trading-journey plan --no-scan            # housekeeping only: stops and exits, no analyses
trading-journey plan --ticker NVDA --dry-run --force   # one real analysis, prints the plan
trading-journey sync                      # reconcile the journal with Alpaca
trading-journey report                    # markdown performance report in reports/
```

**Kill switch:** create a file named `STOP` in `%LOCALAPPDATA%\trading-journey\`. That folder also holds the SQLite journal, `agent.log` and every plan JSON, outside OneDrive, because OneDrive sync can corrupt SQLite.

**Cost:** each TradingAgents analysis makes about 15 LLM calls on the Anthropic API key. With the defaults (Sonnet managers, Haiku analysts, low thinking effort, all 4 analysts) one analysis costs about $0.08, so a scan of `DAILY_SCANS` candidates plus the held positions costs roughly $0.08 × that count. Opus/Sonnet everywhere costs about 10× more. The dashboard's **API cost** card shows the real spend, and each decision row stores its cost.

**Out-of-credit email:** when the API key runs out of credit, the agent emails `ALERT_EMAIL` (once a day) and retries the morning scan every hour until it works. Stops, take-profits and housekeeping keep running. Set `SMTP_USER` and `SMTP_PASSWORD` in `.env` (for Gmail, an [app password](https://myaccount.google.com/apppasswords)), then check it with `trading-journey test-email`.
