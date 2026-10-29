# trading-journey

Connects [TradingAgents](https://github.com/TauricResearch/TradingAgents), a multi-agent LLM trading framework, to the **official TradingView MCP server** (`https://mcp.tradingview.com/mcp`). Claude (Anthropic) is the LLM.

## How it works

1. **TradingView analyst**: a Claude agent connects to the TradingView MCP over streamable HTTP with OAuth, then uses its tools to write a technical report.
2. **TradingAgents**: the regular TradingAgents graph (fundamentals, news, sentiment, bull/bear debate, trader, risk) produces its own decision.
3. **Reconciliation**: Claude merges both into a final `BUY | HOLD | SELL`, and the result is saved to `reports/`.

## Setup (Windows)

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
copy .env.example .env   # then fill in ANTHROPIC_API_KEY and SEC_EDGAR_USER_AGENT
```

TradingView MCP requires a TradingView account on the **Essential plan or higher**. Free trials are not supported.

## Usage

```powershell
trading-journey tools                      # first run opens the TradingView OAuth login, then lists tools
trading-journey analyze NVDA --date 2026-10-28
```

OAuth tokens are cached in `.tokens/`, which is gitignored.

## Claude Code

`.mcp.json` registers the server for this project. You can also register it yourself:

```powershell
claude mcp add --transport http mcp-tradingview https://mcp.tradingview.com/mcp
```

Then run `/mcp` inside Claude Code and authenticate `mcp-tradingview`.

## Disclaimer

For research and education only. This is not financial advice.
