"""Simulated broker: a cash + positions ledger in SQLite, filled at the live quote."""

from __future__ import annotations

from collections.abc import Callable

from .. import market_data
from ..store import Store
from .base import Account, BrokerPosition


class LocalPaperBroker:
    name = "local_paper"

    def __init__(self, store: Store, starting_cash: float, quote_fn: Callable[[str], float] = market_data.quote):
        self.store = store
        self._quote = quote_fn
        if not store.rows("SELECT cash FROM paper_account WHERE id = 1"):
            store.execute("INSERT INTO paper_account (id, cash) VALUES (1, ?)", (starting_cash,))

    def _cash(self) -> float:
        return self.store.rows("SELECT cash FROM paper_account WHERE id = 1")[0]["cash"]

    def quote(self, ticker: str) -> float:
        return self._quote(ticker)

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(r["ticker"], r["qty"], r["avg_price"])
            for r in self.store.rows("SELECT * FROM paper_positions WHERE qty > 0")
        ]

    def account(self) -> Account:
        cash = self._cash()
        value = sum(p.qty * self.quote(p.ticker) for p in self.positions())
        return Account(equity=cash + value, cash=cash)

    def buy(self, ticker: str, qty: int) -> float:
        price = self.quote(ticker)
        cost = price * qty
        if cost > self._cash():
            raise ValueError(f"insufficient paper cash for {qty} {ticker}")
        row = self.store.rows("SELECT qty, avg_price FROM paper_positions WHERE ticker = ?", (ticker,))
        old_qty, old_avg = (row[0]["qty"], row[0]["avg_price"]) if row else (0, 0.0)
        new_qty = old_qty + qty
        avg = (old_qty * old_avg + cost) / new_qty
        self.store.execute(
            "INSERT INTO paper_positions (ticker, qty, avg_price) VALUES (?,?,?)"
            " ON CONFLICT(ticker) DO UPDATE SET qty = excluded.qty, avg_price = excluded.avg_price",
            (ticker, new_qty, avg),
        )
        self.store.execute("UPDATE paper_account SET cash = cash - ? WHERE id = 1", (cost,))
        return price

    def sell(self, ticker: str, qty: int) -> float:
        row = self.store.rows("SELECT qty FROM paper_positions WHERE ticker = ?", (ticker,))
        held = row[0]["qty"] if row else 0
        qty = min(qty, held)
        if qty <= 0:
            raise ValueError(f"no paper position in {ticker}")
        price = self.quote(ticker)
        self.store.execute("UPDATE paper_positions SET qty = qty - ? WHERE ticker = ?", (qty, ticker))
        self.store.execute("UPDATE paper_account SET cash = cash + ? WHERE id = 1", (price * qty,))
        return price
