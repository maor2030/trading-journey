"""Execute through the TradingView MCP, if it exposes trading tools.

TradingView's official MCP is in beta and its tool set isn't documented, so
tools are discovered at startup by name and their argument names are mapped
from each tool's input schema. If no order/position/account tools exist,
BrokerUnavailable is raised and the agent falls back to the local paper broker.
Run `trading-journey tools` to see what the server exposes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from .. import market_data
from ..config import Settings
from ..tv_mcp import call_tool, list_tools_full
from .base import Account, BrokerPosition, BrokerUnavailable

logger = logging.getLogger(__name__)

_ROLES = {
    "order": re.compile(r"(place|create|submit|send).*order|order.*(place|create|submit)", re.I),
    "positions": re.compile(r"positions?", re.I),
    "account": re.compile(r"account|balance|portfolio", re.I),
}
_ALIASES = {
    "symbol": ("symbol", "ticker", "instrument"),
    "side": ("side", "direction", "action"),
    "qty": ("qty", "quantity", "size", "amount", "units"),
    "type": ("type", "order_type", "orderType"),
}


def _pick(tools, role):
    matches = [t for t in tools if _ROLES[role].search(t.name)]
    return min(matches, key=lambda t: len(t.name)) if matches else None


def _map_args(tool, values: dict) -> dict:
    props = (tool.inputSchema or {}).get("properties", {})
    args = {}
    for key, value in values.items():
        name = next((a for a in _ALIASES[key] if a in props), None)
        if name:
            args[name] = value
    return args


def _json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class TradingViewBroker:
    name = "tradingview"

    def __init__(self, settings: Settings):
        self.settings = settings
        tools = asyncio.run(list_tools_full(settings))
        self.tools = {role: _pick(tools, role) for role in _ROLES}
        missing = [role for role, tool in self.tools.items() if tool is None]
        if missing:
            raise BrokerUnavailable(f"no TradingView MCP tools for: {', '.join(missing)}")
        logger.info("TradingView broker tools: %s", {r: t.name for r, t in self.tools.items()})

    def _call(self, role: str, args: dict | None = None):
        text = asyncio.run(call_tool(self.tools[role].name, args or {}, self.settings))
        return _json(text) or text

    def quote(self, ticker: str) -> float:
        return market_data.quote(ticker)

    def account(self) -> Account:
        data = self._call("account")
        if not isinstance(data, dict):
            raise BrokerUnavailable(f"unreadable account response: {str(data)[:200]}")
        cash = float(data.get("cash") or data.get("balance") or 0)
        equity = float(data.get("equity") or data.get("netLiquidation") or cash)
        return Account(equity=equity, cash=cash)

    def positions(self) -> list[BrokerPosition]:
        data = self._call("positions")
        items = data.get("positions", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        out = []
        for p in items:
            symbol = str(p.get("symbol") or p.get("ticker") or "").split(":")[-1]
            qty = int(float(p.get("qty") or p.get("quantity") or 0))
            if symbol and qty:
                out.append(BrokerPosition(symbol, qty, float(p.get("avg_price") or p.get("avgPrice") or 0)))
        return out

    def _order(self, ticker: str, qty: int, side: str) -> float:
        args = _map_args(self.tools["order"], {"symbol": ticker, "side": side, "qty": qty, "type": "market"})
        logger.info("TradingView %s %s x%d -> %s", side, ticker, qty, self._call("order", args))
        return self.quote(ticker)

    def buy(self, ticker: str, qty: int) -> float:
        return self._order(ticker, qty, "buy")

    def sell(self, ticker: str, qty: int) -> float:
        return self._order(ticker, qty, "sell")
