import dataclasses
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from trading_journey.agent import TradingAgent
from trading_journey.alpaca import Account, Position
from trading_journey.config import SETTINGS
from trading_journey.decision import Decision
from trading_journey.store import Store

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)  # a Tuesday, NYSE open


class FakeAlpaca:
    """Simulates the Alpaca orders/positions that Claude's MCP calls would create."""

    paper = True

    def __init__(self, prices):
        self.prices = dict(prices)
        self.cash = 100_000.0
        self.positions_ = {}  # ticker -> [qty, avg]
        self.orders_ = {}  # id -> order dict
        self._ids = itertools.count(1)

    # --- read API used by the agent ---
    def account(self):
        value = sum(q * self.prices[t] for t, (q, _) in self.positions_.items())
        return Account(self.cash + value, self.cash)

    def positions(self):
        return [Position(t, q, a, self.prices[t]) for t, (q, a) in self.positions_.items()]

    def order(self, order_id):
        return self.orders_[order_id]

    def order_by_client_id(self, cid):
        return next((o for o in self.orders_.values() if o.get("client_order_id") == cid), None)

    def orders(self, symbol, status="all", after=None):
        return [o for o in self.orders_.values() if o["symbol"] == symbol]

    def clock(self):
        return {"is_open": True, "next_open": ""}

    # --- what Claude does through the MCP ---
    def _new(self, **kw):
        oid = f"o{next(self._ids)}"
        self.orders_[oid] = {"id": oid, "status": "new", "legs": [], **kw}
        return self.orders_[oid]

    def execute(self, intent):
        for call in intent["calls"]:
            args = call["args"]
            if call["tool"] == "place_stock_order":
                parent = self._new(symbol=args["symbol"], side="buy", type=args["type"], qty=args["qty"],
                                   limit_price=args.get("limit_price"), client_order_id=args["client_order_id"])
                parent["legs"] = [
                    self._new(symbol=args["symbol"], side="sell", type="stop", stop_price=args["stop_loss_stop_price"]),
                    self._new(symbol=args["symbol"], side="sell", type="limit", limit_price=args["take_profit_limit_price"]),
                ]
                if args["type"] == "market":
                    self.fill_entry(parent)
            elif call["tool"] == "cancel_order_by_id":
                self.orders_[args["order_id"]]["status"] = "canceled"
            elif call["tool"] == "replace_order_by_id":
                old = self.orders_[args["order_id"]]
                new = self._new(**{k: v for k, v in old.items() if k not in ("id", "status", "legs")})
                new.update({k: v for k, v in args.items() if k != "order_id"})
                old.update(status="replaced", replaced_by=new["id"])
            elif call["tool"] == "close_position":
                t = args["symbol_or_asset_id"]
                qty, _ = self.positions_.pop(t)
                self.cash += qty * self.prices[t]
                self._new(symbol=t, side="sell", type="market", status="filled",
                          filled_avg_price=str(self.prices[t]), filled_at="2026-10-28T15:00:00Z")

    def fill_entry(self, parent, price=None):
        t = parent["symbol"]
        price = price or self.prices[t]
        qty = int(parent["qty"])
        parent.update(status="filled", filled_avg_price=str(price), filled_qty=str(qty), filled_at="2026-10-27T15:01:00Z")
        self.cash -= qty * price
        self.positions_[t] = [qty, price]

    def hit_leg(self, ticker, leg_type):
        """Price touched a bracket leg: fill it, cancel the other, flatten the position."""
        parent = next(o for o in self.orders_.values() if o["symbol"] == ticker and o["side"] == "buy")
        for leg in parent["legs"]:
            while leg.get("replaced_by"):
                leg = self.orders_[leg["replaced_by"]]
            if leg["type"] == leg_type:
                price = float(leg["stop_price"] if leg_type == "stop" else leg["limit_price"])
                leg.update(status="filled", filled_avg_price=str(price), filled_at="2026-10-28T16:00:00Z")
            else:
                leg["status"] = "canceled"
        qty, _ = self.positions_.pop(ticker)
        self.cash += qty * price


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(SETTINGS, data_dir=tmp_path, reports_dir=tmp_path / "reports")


@pytest.fixture
def alpaca():
    return FakeAlpaca({"NVDA": 100.0, "AMD": 50.0, "CRM": 200.0})


@pytest.fixture
def make_agent(settings, alpaca):
    def _make(decisions=None):
        decisions = decisions if decisions is not None else {}

        def analyze(ticker, trade_date, portfolio=None):
            return decisions[ticker]

        agent = TradingAgent(
            Store(settings.db_path), alpaca, settings,
            universe={"NVDA": "Semiconductors", "AMD": "Semiconductors", "CRM": "Software"},
            analyze_fn=analyze, quote_fn=lambda t: alpaca.prices[t], atr_fn=lambda t: 3.0,
            expiry_fn=lambda now, days: now + timedelta(days=days),
        )
        agent._portfolio_context = lambda: None
        return agent

    return _make


def decision(ticker, rating, entry=None, stop=None, target=None, horizon=30):
    return Decision(ticker, "2026-10-27", rating, entry, stop, target, horizon, "")


def run_cycle(agent, alpaca, now=NOW, **kw):
    """One /trade cycle: plan, Claude executes every intent, sync."""
    plan = agent.plan(now=now, **kw)
    for intent in plan["intents"]:
        alpaca.execute(intent)
    agent.sync(now)
    return plan
