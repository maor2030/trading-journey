# trading-journey

This is an autonomous, **long-only** paper-trading agent.
- [TradingAgents](https://github.com/TauricResearch/TradingAgents), a multi-agent LLM framework running on Claude, makes every decision.
- Execution goes through the **official TradingView MCP** (`https://mcp.tradingview.com/mcp`) when it offers trading tools, or a local paper ledger otherwise.

> For research and education only. This is not financial advice. Run it on paper and measure it before you consider real money.

## What it does

| | |
|---|---|
| **Universe** | ~50 liquid, heavily covered US stocks across sectors (`src/trading_journey/universe.py`). Override it with `universe.yaml` (`TICKER: Sector`). |
| **Hourly scan** | During NYSE hours, it runs **5 TradingAgents analyses per hour**. Held positions due for review go first, then the highest-priority stocks not analyzed in the last 20h. |
| **Entries** | Only on **Buy** or **Overweight**. If the trader's entry price is below the market, it places a **pending limit entry** that waits up to 3 trading days. |
| **Stop-loss / take-profit** | The trader's stop and the portfolio manager's price target, sanity-checked: the stop must be 2–15% below entry, else 2×ATR is used, and reward:risk must be ≥ 1.5. |
| **Exits** | Stop-loss, take-profit, a **trailing stop** (breakeven at +1R, trailing 1R under the high from +2R), the **time horizon**, or a **Sell/Underweight** re-analysis. |
| **Sizing** | Each trade risks 1% of equity × rating (Buy 1.0, Overweight 0.6) × the stock's track record (0.5–1.5). Caps: 15% per position, 30% per sector, a 10% cash reserve and at most 10 positions. |
| **Learning** | Every decision and trade is journaled. The per-ticker win rate, average R and TradingAgents' settled alpha raise or lower a stock's scan priority. Losers (≥5 trades, negative average R) are only re-scanned weekly. |
| **Risk monitor** | Every 5 minutes, with no LLM involved: fills pending entries, trails stops, and enforces exits. A daily −3% drawdown pauses new buys. |

Stops, targets and pending entries are managed by the agent, not the broker. Because of that, they behave the same on every broker adapter.

## Setup (Windows)

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
copy .env.example .env   # fill in ANTHROPIC_API_KEY and SEC_EDGAR_USER_AGENT
```

## Usage

```powershell
trading-journey scan --ticker NVDA --dry-run   # one real analysis, prints the plan, places nothing
trading-journey run                            # start the agent (leave it running; idles outside market hours)
trading-journey status                         # account, positions, pending entries, scan priority
trading-journey report                         # markdown performance report in reports/
trading-journey monitor                        # one risk-monitor pass
trading-journey tools                          # TradingView OAuth login + list MCP tools
```

**Kill switch:** create a file named `STOP` in `%LOCALAPPDATA%\trading-journey\` and the agent halts all scans and orders. Delete the file to resume.

The database (`trading_journey.sqlite3`) and `agent.log` are also stored there, outside OneDrive, because OneDrive sync can corrupt SQLite.

## Executing through TradingView

1. Run `trading-journey tools` and sign in. You need a TradingView account on the Essential plan or higher.
2. If the listed tools include order, position and account tools, set `BROKER=tradingview` in `.env`. Make sure the TradingView account is connected to **Paper Trading**.
3. If the MCP doesn't expose those tools, the agent logs a warning and uses the local paper broker.

Adding a real broker (Alpaca, IBKR) later means writing one adapter in `src/trading_journey/brokers/` with `account / positions / quote / buy / sell`.

## Cost

Each TradingAgents analysis makes dozens of LLM calls. At 5 per hour that comes to about 35 a day. Reduce `SCANS_PER_HOUR` or use cheaper models (`TA_DEEP_MODEL`, `TA_QUICK_MODEL`) to lower the cost.

## Claude Code

`.mcp.json` registers `mcp-tradingview` for this folder. Run `/mcp` in Claude Code to authenticate it.
