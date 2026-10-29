---
name: trade-agent
description: Start the autonomous trading-journey agent - runs the /trade cycle every hour (TradingAgents decides, Claude executes via the Alpaca MCP) for as long as this Claude session stays open. Use when the user runs /trade-agent or asks to start/run the trading agent.
---

# Start the autonomous trading agent

1. **Preflight.**
   - Confirm the Alpaca MCP tools (`mcp__alpaca__*`) are available. If not, stop and tell the user to restart Claude Code from this folder.
   - Run `.venv/Scripts/trading-journey.exe status` with the Bash tool.
   - Show the user the account line: paper or live, equity, market open or closed.
   - If it says **LIVE**, stop and ask the user to confirm before continuing.
2. **Start the hourly loop.** Invoke the `loop` skill with the args `60m /trade`.
   - Each cycle runs the `trade` skill.
   - Outside US market hours (16:30–23:00 Israel time) a cycle just reports the next open and ends. Those cycles are cheap and make no LLM analysis calls.
3. **Tell the user:**
   - The agent runs only while this Claude Code session stays open and the PC is awake.
   - **To stop it:** stop the loop, or create a `STOP` file in `%LOCALAPPDATA%\trading-journey\`. The `STOP` file also blocks any `/trade` cycle.
   - Stop-loss and take-profit orders live at Alpaca. Open positions stay protected even when this session is closed.
