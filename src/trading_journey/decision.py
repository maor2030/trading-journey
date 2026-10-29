"""Run TradingAgents on one ticker and turn its output into a structured decision."""

from __future__ import annotations

import re
from dataclasses import dataclass

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
    )


_graph = None


def _get_graph(settings: Settings):
    global _graph
    if _graph is None:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        _graph = TradingAgentsGraph(debug=False, config=tradingagents_config(settings))
    return _graph


def analyze(ticker: str, trade_date: str, portfolio=None, settings: Settings = SETTINGS) -> Decision:
    """Full TradingAgents run. `portfolio` is a tradingagents.portfolio.PortfolioContext."""
    final_state, signal = _get_graph(settings).propagate(ticker, trade_date, portfolio=portfolio)
    return parse_decision(ticker, trade_date, str(signal), final_state)
