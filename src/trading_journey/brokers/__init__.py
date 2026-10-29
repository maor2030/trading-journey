"""Broker adapters. The agent manages stops, targets and pending limits itself,
so a broker only needs market buys/sells, positions and the account."""

from __future__ import annotations

import logging

from ..config import SETTINGS, Settings
from ..store import Store
from .base import Account, Broker, BrokerPosition, BrokerUnavailable

logger = logging.getLogger(__name__)

__all__ = ["Account", "Broker", "BrokerPosition", "BrokerUnavailable", "make_broker"]


def make_broker(store: Store, settings: Settings = SETTINGS) -> Broker:
    if settings.trading_mode == "live" and settings.live_confirm != "I_UNDERSTAND":
        raise SystemExit("TRADING_MODE=live requires LIVE_CONFIRM=I_UNDERSTAND in .env")

    if settings.broker == "tradingview":
        from .tradingview import TradingViewBroker

        try:
            return TradingViewBroker(settings)
        except BrokerUnavailable as exc:
            logger.warning("TradingView MCP can't execute orders (%s); using local paper broker", exc)

    from .local_paper import LocalPaperBroker

    return LocalPaperBroker(store, settings.starting_cash)
