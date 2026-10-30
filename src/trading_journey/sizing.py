"""Stop/target sanity rules and conviction-weighted position sizing, for longs and shorts."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import SETTINGS, Settings

RATING_MULT = {"Buy": 1.0, "Overweight": 0.6, "Sell": 1.0, "Underweight": 0.6}

MIN_STOP_PCT, MAX_STOP_PCT = 0.02, 0.15
MIN_REWARD_RISK = 1.5


@dataclass
class Levels:
    entry: float
    stop: float
    target: float
    side: str = "long"

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)


def finalize_levels(
    price: float,
    entry: float | None,
    stop: float | None,
    target: float | None,
    atr: float | None,
    side: str = "long",
) -> Levels | None:
    """Validate the agents' levels, falling back to ATR-based ones. None = skip the trade.

    A short mirrors a long: entry at or above the market, stop above, target below.
    """
    d = 1 if side == "long" else -1
    # An entry on the wrong side of the market (or absurdly far from it) is treated as "now".
    if entry is None or d * (entry - price) > 0 or abs(entry - price) > price * 0.1:
        entry = price

    lo, hi = sorted((entry * (1 - d * MAX_STOP_PCT), entry * (1 - d * MIN_STOP_PCT)))
    if stop is None or not (lo <= stop <= hi):
        if atr is None:
            return None
        stop = min(max(entry - d * 2 * atr, lo), hi)

    risk = abs(entry - stop)
    if target is None or d * (target - entry) < MIN_REWARD_RISK * risk:
        target = entry + d * 2 * risk
    return Levels(round(entry, 2), round(stop, 2), round(target, 2), side)


def position_size(
    *,
    equity: float,
    cash: float,
    levels: Levels,
    rating: str,
    score_mult: float,
    sector_exposure: float,
    settings: Settings = SETTINGS,
) -> int:
    """Shares to buy: risk-based, scaled by conviction, then capped."""
    mult = RATING_MULT.get(rating, 0.0)
    if mult == 0 or levels.risk <= 0:
        return 0
    risk_dollars = equity * settings.risk_per_trade * mult * score_mult
    shares = math.floor(risk_dollars / levels.risk)

    caps = [
        equity * settings.max_position_pct,
        equity * settings.max_sector_pct - sector_exposure,
        cash - equity * settings.cash_reserve_pct,
    ]
    max_dollars = min(caps)
    shares = min(shares, math.floor(max(max_dollars, 0) / levels.entry))
    return max(shares, 0)
