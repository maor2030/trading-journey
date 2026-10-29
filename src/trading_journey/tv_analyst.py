"""TradingView analyst: a Claude agent that uses the TradingView MCP tools."""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.prebuilt import create_react_agent

from .config import SETTINGS, Settings
from .tv_mcp import tradingview_session

SYSTEM_PROMPT = """You are the TradingView technical analyst on a trading desk.
Use the TradingView tools available to you to gather data for the requested
symbol: price action, key technical indicators (trend, momentum, volatility,
volume), multi-timeframe context, and notable support/resistance levels.
Only state numbers you actually obtained from the tools.
Finish with a concise markdown report ending in a line:
TECHNICAL BIAS: BULLISH | BEARISH | NEUTRAL"""


async def tradingview_report(ticker: str, trade_date: str, settings: Settings = SETTINGS) -> str:
    """Return a markdown technical report for `ticker` built from TradingView MCP data."""
    async with tradingview_session(settings) as session:
        tools = await load_mcp_tools(session)
        llm = ChatAnthropic(model=settings.quick_model, max_tokens=4096)
        agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)
        result = await agent.ainvoke(
            {"messages": [("user", f"Analyze {ticker} as of {trade_date}.")]}
        )
        return result["messages"][-1].content
