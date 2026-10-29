"""Prices, ATR and US market hours."""

from __future__ import annotations

import warnings
from datetime import datetime, timezone
from functools import lru_cache

import pandas as pd

# yfinance triggers pandas deprecation noise on every call.
warnings.filterwarnings("ignore", module="yfinance")


def quote(ticker: str) -> float:
    import yfinance as yf

    price = yf.Ticker(ticker).fast_info["last_price"]
    if not price or price <= 0:
        raise ValueError(f"no price for {ticker}")
    return float(price)


def atr(ticker: str, period: int = 14) -> float | None:
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="3mo", interval="1d")
    if len(hist) < period + 1:
        return None
    prev_close = hist["Close"].shift(1)
    tr = pd.concat(
        [hist["High"] - hist["Low"], (hist["High"] - prev_close).abs(), (hist["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


@lru_cache(maxsize=1)
def _nyse():
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS")


def trade_date(now: datetime | None = None) -> str:
    """Today's date in New York, the date TradingAgents analyzes."""
    now = now or datetime.now(timezone.utc)
    return pd.Timestamp(now).tz_convert("America/New_York").strftime("%Y-%m-%d")


def add_trading_days(start: datetime, days: int) -> datetime:
    sessions = _nyse().sessions_window(pd.Timestamp(trade_date(start)), days + 1)
    close = _nyse().session_close(sessions[-1])
    return close.to_pydatetime()
