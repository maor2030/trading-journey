"""Stop/target sanity rules and conviction-weighted position sizing."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import SETTINGS, Settings

RATING_MULT = {"Buy": 1.0, "Overweight": 0.6}

MIN_STOP_PCT, MAX_STOP_PCT = 0.02, 0.15
MIN_REWARD_RISK = 1.5


@dataclass
class Levels:
    entry: float
    stop: float
    target: float

    @property
    def risk(self) -> float:
        return self.entry - self.stop


def finalize_levels(
    price: float,
    entry: float | None,
    stop: float | None,
    target: float | None,
    atr: float | None,
) -> Levels | None:
    """Validate the agents' levels, falling back to ATR-based ones. None = skip the trade."""
    # An entry above the market (or absurdly far below) is treated as "buy now".
    if entry is None or entry > price or entry < price * 0.9:
        entry = price

    if stop is None or not (entry * (1 - MAX_STOP_PCT) <= stop <= entry * (1 - MIN_STOP_PCT)):
        if atr is None:
            return None
        stop = entry - 2 * atr
        stop = min(max(stop, entry * (1 - MAX_STOP_PCT)), entry * (1 - MIN_STOP_PCT))

    risk = entry - stop
    if target is None or (target - entry) < MIN_REWARD_RISK * risk:
        target = entry + 2 * risk
    return Levels(round(entry, 2), round(stop, 2), round(target, 2))


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
