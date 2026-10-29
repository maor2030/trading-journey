"""Read-only Alpaca REST client, used to keep the journal in sync with the broker.

Orders are never placed from Python: Claude places them through the Alpaca MCP
using the instructions in a plan.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import SETTINGS, Settings

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float = 0.0


@dataclass
class Position:
    ticker: str
    qty: int
    avg_price: float
    price: float


class AlpacaClient:
    def __init__(self, settings: Settings = SETTINGS, http: httpx.Client | None = None):
        if not settings.alpaca_paper and settings.live_confirm != "I_UNDERSTAND":
            raise SystemExit("ALPACA_PAPER_TRADE=false requires LIVE_CONFIRM=I_UNDERSTAND in .env")
        if not (settings.alpaca_key and settings.alpaca_secret):
            raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY missing from .env")
        self.paper = settings.alpaca_paper
        self.http = http or httpx.Client(
            base_url=PAPER_URL if settings.alpaca_paper else LIVE_URL,
            headers={"APCA-API-KEY-ID": settings.alpaca_key, "APCA-API-SECRET-KEY": settings.alpaca_secret},
            timeout=20,
        )

    def _get(self, path: str, **params):
        response = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        response.raise_for_status()
        return response.json()

    def account(self) -> Account:
        a = self._get("/v2/account")
        return Account(float(a["equity"]), float(a["cash"]), float(a.get("buying_power") or 0))

    def positions(self) -> list[Position]:
        return [
            Position(p["symbol"], int(float(p["qty"])), float(p["avg_entry_price"]), float(p["current_price"]))
            for p in self._get("/v2/positions")
        ]

    def order(self, order_id: str) -> dict:
        return self._get(f"/v2/orders/{order_id}", nested="true")

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        try:
            order = self._get("/v2/orders:by_client_order_id", client_order_id=client_order_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return self.order(order["id"])  # re-read with legs

    def orders(self, symbol: str, status: str = "all", after: str | None = None) -> list[dict]:
        return self._get("/v2/orders", status=status, symbols=symbol, after=after, nested="true", limit=100)

    def clock(self) -> dict:
        return self._get("/v2/clock")
