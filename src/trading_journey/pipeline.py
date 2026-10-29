"""End-to-end run: TradingView analyst + TradingAgents + final reconciliation."""

from __future__ import annotations

import asyncio
from datetime import datetime

from langchain_anthropic import ChatAnthropic

from .config import SETTINGS, Settings, tradingagents_config
from .tv_analyst import tradingview_report

RECONCILE_PROMPT = """You are the head of the trading desk. Combine the two inputs
below into one final decision for {ticker} on {date}.

## TradingAgents decision (fundamentals, news, sentiment, debate)
{ta_decision}

## TradingView technical report
{tv_report}

Explain agreements and conflicts briefly, then end with exactly one line:
FINAL DECISION: BUY | HOLD | SELL"""


def run_tradingagents(ticker: str, trade_date: str, settings: Settings = SETTINGS) -> str:
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph(debug=False, config=tradingagents_config(settings))
    _, decision = graph.propagate(ticker, trade_date)
    return str(decision)


def run(ticker: str, trade_date: str, settings: Settings = SETTINGS) -> str:
    tv_report = asyncio.run(tradingview_report(ticker, trade_date, settings))
    ta_decision = run_tradingagents(ticker, trade_date, settings)

    llm = ChatAnthropic(model=settings.deep_model, max_tokens=4096)
    final = llm.invoke(
        RECONCILE_PROMPT.format(
            ticker=ticker, date=trade_date, ta_decision=ta_decision, tv_report=tv_report
        )
    ).content

    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = settings.reports_dir / f"{ticker}_{trade_date}_{stamp}.md"
    path.write_text(
        f"# {ticker} — {trade_date}\n\n## TradingView report\n\n{tv_report}\n\n"
        f"## TradingAgents decision\n\n{ta_decision}\n\n## Final\n\n{final}\n",
        encoding="utf-8",
    )
    print(f"Report saved to {path}")
    return final
