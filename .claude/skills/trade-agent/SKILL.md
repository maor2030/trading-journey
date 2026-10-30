---
name: trade-agent
description: Start (or check) the autonomous trading-journey agent - a background process that every US market hour runs TradingAgents analyses and, when there are orders, a headless Claude session (/trade) that places them via the Alpaca MCP. Also serves the dashboard at http://127.0.0.1:8050. Use when the user runs /trade-agent or asks to start/run/check the trading agent.
---

# Start the autonomous trading agent

The agent is a Python process, `trading-journey agent`. It runs independently of this chat:
- **Once a day at 09:45 New York** (16:45 Israel) it runs the scan: a screener ranks the universe, then TradingAgents (Anthropic API key) analyzes the held positions and the best candidates.
- **Every other market hour** it runs housekeeping only (trailing stops, lost stop-losses, time exits), with no LLM.
- **If a plan has orders,** it runs `claude -p "/trade <plan>" --permission-mode auto` (your Claude account), which places them through the Alpaca MCP.
- **Between cycles** it syncs the journal every 5 minutes.
- **It serves the dashboard** at http://127.0.0.1:8050.

## Steps

1. **Is it already running?** Fetch `http://127.0.0.1:8050/api/state` with PowerShell, e.g. `Invoke-RestMethod http://127.0.0.1:8050/api/state`.
   - If it answers and `agent.ts` is less than 3 minutes old, it's running. Report `agent.state`, `agent.next_scan`, the account equity and the open positions, then stop here.
2. **Preflight:** run `.venv/Scripts/trading-journey.exe status`.
   - If it says **LIVE**, stop and ask the user before starting.
   - If it fails, report the error and stop.
3. **Start it detached** with PowerShell, from the project root:
   ```powershell
   $d = "$env:LOCALAPPDATA\trading-journey"; New-Item -ItemType Directory -Force $d | Out-Null
   Start-Process -FilePath ".venv\Scripts\trading-journey.exe" -ArgumentList "agent" -WorkingDirectory (Get-Location) -WindowStyle Hidden -RedirectStandardOutput "$d\runner.out.log" -RedirectStandardError "$d\runner.err.log"
   ```
4. **Confirm it started:** wait ~70 seconds, then fetch `/api/state` again. `agent.state` should be `market_closed`, `waiting`, `planning` or `scanning`.
   - If it didn't start, show the tail of `%LOCALAPPDATA%\trading-journey\runner.err.log`.
5. **Tell the user:**
   - The dashboard is at http://127.0.0.1:8050.
   - It trades during US market hours (16:30–23:00 Israel time) while the PC is awake.
   - **To pause:** the dashboard's "Stop agent" button (the kill switch).
   - **To end the process:** `Get-Process trading-journey | Stop-Process`.
   - Stop-loss and take-profit orders stay active at Alpaca either way.
