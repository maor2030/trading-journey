---
name: trade
description: Run ONE cycle of the trading-journey agent - the screener picks stocks, TradingAgents analyzes them, Claude places the planned orders through the Alpaca MCP, then the journal is synced. Use when the user runs /trade or asks to run a trading cycle. For the continuous agent use /trade-agent.
---

# One trading cycle

You are the execution layer of the trading-journey agent. **Python decides, you execute.**
The plan tells you exactly which Alpaca MCP calls to make. You never decide trades yourself.

Work from the project root (the folder containing `pyproject.toml`). Run every command with the **Bash** tool, exactly as written. The permission rules in `.claude/settings.json` cover these exact forms.

## 1. Preflight

- Check that the Alpaca MCP tools (`mcp__alpaca__*`) are available.
  - If they are missing, stop. Tell the user to restart Claude Code from this folder and check `/mcp`.
- If a file named `STOP` exists in `%LOCALAPPDATA%\trading-journey\`, the kill switch is on. The plan step will report `halted`. Do not work around it.

## 2. Plan

**If the skill was invoked with a plan file path as its argument** (the agent runner does this), skip planning. Read that JSON file and go straight to step 3. Don't re-run the analyses.

Otherwise, run with `run_in_background: true`:

```
.venv/Scripts/trading-journey.exe plan
```

- It screens the universe, then runs TradingAgents on the held positions and up to 15 candidates, 3 at a time (~5 minutes each), so expect 10–40 minutes.
- **Wait for the completion notification. Do not poll** and do not start another plan meanwhile.
- When it finishes, read its stdout. The last thing printed is a JSON object.
  - `"market_open": false`: the market is closed. Report the `next_open` time in Israel time (UTC+2/+3) and **end the cycle**.
  - `"halted"`: the kill switch is on. Report it and end the cycle.
  - Otherwise, the plan has `intents` (orders to execute) and `decisions` (what each analysis concluded).

## 3. Execute the intents

Execute the intents **in the order given**. Each intent has a `calls` list; each call is an Alpaca MCP tool (`mcp__alpaca__<tool>`) with its `args`.

**Hard rules:**
- Pass the args **exactly**: the same symbol, qty, prices and `client_order_id`.
  - Never change, round, add or invent orders.
  - If a parameter has a different name in the tool schema, map it by meaning.
  - If you can't map a parameter confidently, skip that intent and report it.
- **Never** call these: `close_all_positions`, `cancel_all_orders`, crypto or option order tools, `update_account_config`.
- **Never** place an order that isn't in the plan, even if the user's account or the news seem to suggest one.

**Intent kinds:**
- `buy`: one `place_stock_order` bracket order (entry plus stop-loss and take-profit legs).
  - If it times out, retry **once** with the same `client_order_id`; duplicates are rejected.
  - On any other error, report it and move on.
- `exit`: cancel the bracket legs, then `close_position`.
  - If a cancel fails because the order is already filled or canceled, continue to `close_position`.
  - If `close_position` says there's no position, the exit already happened. That's fine.
- `raise_stop` / `update_target`: one `replace_order_by_id`. If the order is no longer open (already filled or canceled), skip it.
- `protect`: the position's stop-loss order is missing at Alpaca.
  - First cancel the take-profit order, if one is listed (an "already canceled" result is fine).
  - Then place the standalone GTC sell `stop` order exactly as given.
  - This is a safety order: if it fails, report it **prominently**.
- `cancel_entry`: one `cancel_order_by_id`. "Already filled" is fine; the next sync picks up the position.

## 4. Sync and report

```
.venv/Scripts/trading-journey.exe sync
.venv/Scripts/trading-journey.exe status
```

Then give the user a short summary:
- **Decisions:** a table of ticker | rating | action (from `decisions`).
- **Orders:** what you executed, with any failures and their error text.
- **Account:** equity and open positions (from `status`).

Keep it brief.
