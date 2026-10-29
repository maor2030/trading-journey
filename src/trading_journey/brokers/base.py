from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class BrokerUnavailable(RuntimeError):
    """The broker can't be used for execution (e.g. no order tools exposed)."""


@dataclass
class Account:
    equity: float
    cash: float


@dataclass
class BrokerPosition:
    ticker: str
    qty: int
    avg_price: float


class Broker(Protocol):
    name: str

    def account(self) -> Account: ...

    def positions(self) -> list[BrokerPosition]: ...

    def quote(self, ticker: str) -> float: ...

    def buy(self, ticker: str, qty: int) -> float:
        """Market buy; returns the fill price."""
        ...

    def sell(self, ticker: str, qty: int) -> float:
        """Market sell; returns the fill price."""
        ...
