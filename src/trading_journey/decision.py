"""Run TradingAgents on one ticker and turn its output into a structured decision."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from .config import SETTINGS, Settings, tradingagents_config

BULLISH = {"Buy", "Overweight"}
BEARISH = {"Underweight", "Sell"}

_NUMBER = r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


@dataclass
class Decision:
    ticker: str
    trade_date: str
    rating: str
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    horizon_days: int = 30
    summary: str = ""
    details: dict[str, str] = field(default_factory=dict)  # section title -> markdown


def _field(text: str, label: str) -> float | None:
    match = re.search(rf"\*\*{label}\*\*:\s*{_NUMBER}", text or "", re.IGNORECASE)
    return float(match.group(1).replace(",", "")) if match else None


def parse_horizon_days(text: str | None, default: int = 30) -> int:
    """'3-6 months' -> 90. Uses the lower bound, clamped to 5-180 days."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|–)?\s*\d*\s*(day|week|month|year)", text or "", re.IGNORECASE)
    if not match:
        return default
    days = float(match.group(1)) * _UNIT_DAYS[match.group(2).lower()]
    return int(min(max(days, 5), 180))


def _debate(state: dict, key: str, field_name: str) -> str | None:
    debate = state.get(key)
    return debate.get(field_name) if isinstance(debate, dict) else None


# What a reader needs to judge a call, in reading order.
REPORT_SECTIONS = [
    ("Portfolio manager", lambda s: s.get("final_trade_decision")),
    ("Trader", lambda s: s.get("trader_investment_plan")),
    ("Research manager", lambda s: s.get("investment_plan") or _debate(s, "investment_debate_state", "judge_decision")),
    ("Bull case", lambda s: _debate(s, "investment_debate_state", "bull_history")),
    ("Bear case", lambda s: _debate(s, "investment_debate_state", "bear_history")),
    ("Risk debate", lambda s: _debate(s, "risk_debate_state", "history")),
    ("Market / technicals", lambda s: s.get("market_report")),
    ("News", lambda s: s.get("news_report")),
    ("Sentiment", lambda s: s.get("sentiment_report")),
    ("Fundamentals", lambda s: s.get("fundamentals_report")),
]


def report_details(final_state: dict) -> dict[str, str]:
    out = {}
    for title, get in REPORT_SECTIONS:
        try:
            text = get(final_state)
        except Exception:
            text = None
        if isinstance(text, str) and text.strip():
            out[title] = text.strip()
    return out


def parse_decision(ticker: str, trade_date: str, rating: str, final_state: dict) -> Decision:
    trader = final_state.get("trader_investment_plan", "") or ""
    pm = final_state.get("final_trade_decision", "") or ""
    horizon = re.search(r"\*\*Time Horizon\*\*:\s*(.+)", pm)
    summary = re.search(r"\*\*Executive Summary\*\*:\s*(.+)", pm)
    return Decision(
        ticker=ticker,
        trade_date=trade_date,
        rating=rating,
        entry=_field(trader, "Entry Price"),
        stop=_field(trader, "Stop Loss"),
        target=_field(pm, "Price Target"),
        horizon_days=parse_horizon_days(horizon.group(1) if horizon else None),
        summary=summary.group(1).strip() if summary else "",
        details=report_details(final_state),
    )


_local = threading.local()  # one graph per thread: the scan runs analyses in parallel


def prefer_json_schema_structured_output() -> None:
    """Make TradingAgents' Claude agents use json_schema structured output.

    Its default (forced tool calling) fails when thinking is enabled, so the
    Trader and managers silently fell back to free text and their entry/stop/
    target had to be scraped from prose.
    """
    from tradingagents.llm_clients.anthropic_client import NormalizedChatAnthropic

    if getattr(NormalizedChatAnthropic, "_tj_json_schema", False):
        return
    original = NormalizedChatAnthropic.with_structured_output

    def with_structured_output(self, schema, *, method="json_schema", **kwargs):
        return original(self, schema, method=method, **kwargs)

    NormalizedChatAnthropic.with_structured_output = with_structured_output
    NormalizedChatAnthropic._tj_json_schema = True


def _get_graph(settings: Settings):
    if getattr(_local, "graph", None) is None:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        prefer_json_schema_structured_output()
        _local.graph = TradingAgentsGraph(debug=False, config=tradingagents_config(settings))
    return _local.graph


def analyze(ticker: str, trade_date: str, portfolio=None, settings: Settings = SETTINGS) -> Decision:
    """Full TradingAgents run. `portfolio` is a tradingagents.portfolio.PortfolioContext."""
    final_state, signal = _get_graph(settings).propagate(ticker, trade_date, portfolio=portfolio)
    return parse_decision(ticker, trade_date, str(signal), final_state)
