"""Morning screener: which stocks in the universe are worth a TradingAgents analysis today.

TradingAgents is slow and costly, so the day's scan analyzes only the names
where something is happening and the trend gives a trade a direction:

- activity: relative volume (today's pace vs the 20-day average) and the
  opening gap measured in ATRs;
- trend: above/below the 50-day average, 20-day vs 50-day, 20-day momentum.
  An uptrend scores for a long; with shorts allowed, a downtrend scores the
  same for a short, and the row's `bias` says which;
- earnings: just reported is a catalyst; reporting within the next two days
  is a coin flip the stop-loss can't protect against, so it's penalized.

One batched yfinance download for prices, plus one calendar lookup per ticker.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

from . import market_data

logger = logging.getLogger("trading_journey.screener")

SESSION_MINUTES = 390
EARNINGS_SOON_DAYS = 2
EARNINGS_RECENT_DAYS = 3


@dataclass
class ScreenRow:
    ticker: str
    score: float
    price: float | None = None
    gap_pct: float | None = None
    rvol: float | None = None
    atr_pct: float | None = None
    mom20_pct: float | None = None
    earnings: str | None = None  # ISO date of the next/last report near today
    reasons: list[str] = field(default_factory=list)
    bias: str = "long"  # the side the trend favours

    def to_dict(self) -> dict:
        return asdict(self)


def _clip(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def score_bars(ticker: str, bars: pd.DataFrame, today: date, minutes_open: float | None,
               earnings: date | None = None, shorts: bool = True) -> ScreenRow:
    """Score one ticker from daily bars (oldest first; today's partial bar last when the market is open)."""
    bars = bars.dropna(subset=["Close"])
    if len(bars) < 55:
        return ScreenRow(ticker, 0.0, reasons=["not enough history"])
    live = bars.index[-1].date() == today
    prior = bars.iloc[:-1] if live else bars
    prev_close = float(prior["Close"].iloc[-1])
    price = float(bars["Close"].iloc[-1])

    prev = prior["Close"].shift(1)
    tr = pd.concat([prior["High"] - prior["Low"], (prior["High"] - prev).abs(), (prior["Low"] - prev).abs()],
                   axis=1).max(axis=1)
    atr_pct = float(tr.rolling(14).mean().iloc[-1]) / prev_close
    sma20 = float(prior["Close"].rolling(20).mean().iloc[-1])
    sma50 = float(prior["Close"].rolling(50).mean().iloc[-1])
    mom20 = prev_close / float(prior["Close"].iloc[-21]) - 1
    reasons = []

    # Activity: is the stock in play today?
    gap = rvol = None
    activity_parts = []
    if live:
        gap = float(bars["Open"].iloc[-1]) / prev_close - 1
        gap_atr = abs(gap) / atr_pct if atr_pct else 0
        activity_parts.append(min(gap_atr / 1.5, 1.0))
        if abs(gap) >= 0.01:
            reasons.append(f"gap {gap:+.1%} ({gap_atr:.1f} ATR)")
        avg_vol = float(prior["Volume"].tail(20).mean())
        if minutes_open and avg_vol > 0:
            pace = max(minutes_open, 15) / SESSION_MINUTES
            rvol = float(bars["Volume"].iloc[-1]) / avg_vol / pace
            activity_parts.append(min(rvol / 3, 1.0))
            if rvol >= 1.5:
                reasons.append(f"RVOL {rvol:.1f}x")
    activity = sum(activity_parts) / len(activity_parts) if activity_parts else 0.0

    # Trend: an uptrend for a long, a downtrend for a short.
    def trend_for(d: int) -> float:
        return (0.5 * (d * (prev_close - sma50) > 0) + 0.25 * (d * (sma20 - sma50) > 0)
                + 0.25 * (_clip(d * mom20 / 0.20, -1, 1) + 1) / 2)

    bias = "short" if shorts and trend_for(-1) > trend_for(1) else "long"
    trend = trend_for(-1 if bias == "short" else 1)
    reasons.append(f"{'above' if prev_close > sma50 else 'below'} 50d, 20d {mom20:+.1%}"
                   + (" (short setup)" if bias == "short" else ""))

    score = 0.5 * activity + 0.5 * trend
    if earnings:
        days = (earnings - today).days
        if 0 <= days <= EARNINGS_SOON_DAYS:
            score -= 0.3
            reasons.append(f"earnings in {days}d (avoid)" if days else "earnings today (avoid)")
        elif -EARNINGS_RECENT_DAYS <= days < 0:
            score += 0.15
            reasons.append(f"reported {-days}d ago")

    return ScreenRow(ticker, round(score, 4), round(price, 2), None if gap is None else round(gap * 100, 2),
                     None if rvol is None else round(rvol, 2), round(atr_pct * 100, 2), round(mom20 * 100, 2),
                     earnings.isoformat() if earnings else None, reasons, bias)


def _earnings_near(ticker: str, today: date) -> date | None:
    """The earnings date closest to today, if within a week either side."""
    import yfinance as yf

    try:
        dates = yf.Ticker(ticker).calendar.get("Earnings Date") or []
    except Exception:
        return None
    near = [d for d in dates if isinstance(d, date) and abs((d - today).days) <= 7]
    return min(near, key=lambda d: abs((d - today).days)) if near else None


def _download(tickers: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    data = yf.download(tickers, period="6mo", interval="1d", group_by="ticker", auto_adjust=False,
                       progress=False, threads=True)
    out = {}
    for t in tickers:
        try:
            out[t] = data[t] if len(tickers) > 1 else data
        except KeyError:
            logger.warning("No price data for %s", t)
    return out


def screen(universe: list[str], now: datetime | None = None, shorts: bool = True) -> list[ScreenRow]:
    """Every ticker in the universe, best first."""
    now = now or datetime.now(timezone.utc)
    ny = pd.Timestamp(now).tz_convert("America/New_York")
    today = ny.date()
    session_open = ny.normalize() + pd.Timedelta(hours=9, minutes=30)
    minutes_open = (ny - session_open).total_seconds() / 60 if ny >= session_open else None

    bars = _download(universe)
    with ThreadPoolExecutor(8) as pool:
        earnings = dict(zip(universe, pool.map(lambda t: _earnings_near(t, today), universe)))
    rows = []
    for t in universe:
        if t not in bars:
            rows.append(ScreenRow(t, 0.0, reasons=["no price data"]))
            continue
        try:
            rows.append(score_bars(t, bars[t], today, minutes_open, earnings.get(t), shorts))
        except Exception as exc:
            logger.warning("Screening %s failed: %s", t, exc)
            rows.append(ScreenRow(t, 0.0, reasons=[f"error: {exc}"]))
    rows.sort(key=lambda r: r.score, reverse=True)
    logger.info("Screened %d tickers for %s; top: %s", len(rows), market_data.trade_date(now),
                ", ".join(f"{r.ticker} {r.score:.2f}" for r in rows[:10]))
    return rows
