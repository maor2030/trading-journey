# trading-journey

An autonomous, **long-only** paper-trading agent, run from a Claude Code session.

- **TradingAgents decides.** [TradingAgents](https://github.com/TauricResearch/TradingAgents), a multi-agent LLM framework, analyzes each stock with your Anthropic API key.
- **Claude executes.** Your Claude Code session places the orders through the [Alpaca MCP](https://alpaca.markets/mcp-server) on an Alpaca paper account.

> For research and education only. This is not financial advice. Run it on paper and measure it before you consider real money.

## How a cycle works (`/trade`)

```
trading-journey plan   →  syncs with Alpaca, runs up to 5 TradingAgents analyses,
                          writes exact order instructions (JSON), never trades
Claude (/trade skill)  →  executes those instructions via the Alpaca MCP
trading-journey sync   →  journals fills, exits, P&L from Alpaca's real orders
```

`/trade-agent` repeats this every hour with `/loop` for as long as the session is open.

| | |
|---|---|
| **Universe** | ~50 liquid, heavily covered US stocks across sectors (`src/trading_journey/universe.py`). Override it with `universe.yaml` (`TICKER: Sector`). |
| **Scan order** | Held positions due for review come first. Then the highest-priority stocks not analyzed in the last 20h. When no position slot is free, new stocks aren't analyzed, which saves API cost. |
| **Entries** | Only on **Buy** or **Overweight**. Each entry is a GTC **bracket order** at Alpaca. It's a limit order (waits up to 3 trading days) when TradingAgents' entry price is below the market, and a market order otherwise. |
| **Stop-loss / take-profit** | Bracket legs held **at Alpaca**, so they work even when Claude is closed. Levels come from TradingAgents, sanity-checked: the stop must be 2–15% below entry, else 2×ATR is used, and reward:risk must be ≥ 1.5. |
| **Exits** | Stop-loss or take-profit fills, a **trailing stop** (raised to breakeven at +1R and 1R under the high from +2R, checked each cycle), the **time horizon**, or a **Sell/Underweight** re-analysis. |
| **Sizing** | Each trade risks 1% of equity × rating (Buy 1.0, Overweight 0.6) × the stock's track record (0.5–1.5). Caps: 15% per position, 30% per sector, a 10% cash reserve and at most 10 positions. |
| **Learning** | The per-ticker win rate, average R and TradingAgents' settled alpha raise or lower a stock's scan priority. Losers (≥5 trades, negative average R) are only re-scanned weekly. |
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

- `/trade-agent` starts hourly autonomous trading.
- `/trade` runs one cycle now.

You can also run the Python side by hand:

```powershell
trading-journey status                    # account, positions, pending entries, scan priority
trading-journey plan --ticker NVDA --dry-run --force   # one real analysis, prints the plan
trading-journey sync                      # reconcile the journal with Alpaca
trading-journey report                    # markdown performance report in reports/
```

**Kill switch:** create a file named `STOP` in `%LOCALAPPDATA%\trading-journey\`. That folder also holds the SQLite journal, `agent.log` and every plan JSON, outside OneDrive, because OneDrive sync can corrupt SQLite.

**Cost:** each TradingAgents analysis makes dozens of LLM calls. At 5 per hour that's about 35 a day on the Anthropic API key. Lower `SCANS_PER_HOUR` or choose cheaper models (`TA_DEEP_MODEL`, `TA_QUICK_MODEL`) to reduce it.
