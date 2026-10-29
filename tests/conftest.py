import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from trading_journey.agent import TradingAgent
from trading_journey.brokers.local_paper import LocalPaperBroker
from trading_journey.config import SETTINGS
from trading_journey.decision import Decision
from trading_journey.store import Store

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)  # a Tuesday, NYSE open


class FakeMarket:
    def __init__(self, prices):
        self.prices = dict(prices)

    def __call__(self, ticker):
        return self.prices[ticker]


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(SETTINGS, data_dir=tmp_path, reports_dir=tmp_path / "reports")


@pytest.fixture
def market():
    return FakeMarket({"NVDA": 100.0, "AMD": 50.0, "CRM": 200.0})


@pytest.fixture
def make_agent(settings, market):
    def _make(decisions=None):
        store = Store(settings.db_path)
        broker = LocalPaperBroker(store, 100_000, quote_fn=market)
        decisions = decisions or {}

        def analyze(ticker, trade_date, portfolio=None):
            return decisions[ticker]

        agent = TradingAgent(
            store, broker, settings,
            universe={"NVDA": "Semiconductors", "AMD": "Semiconductors", "CRM": "Software"},
            analyze_fn=analyze, atr_fn=lambda t: 3.0,
            expiry_fn=lambda now, days: now + timedelta(days=days),
        )
        agent._portfolio_context = lambda: None
        return agent

    return _make


def decision(ticker, rating, entry=None, stop=None, target=None, horizon=30):
    return Decision(ticker, "2026-10-27", rating, entry, stop, target, horizon, "")
