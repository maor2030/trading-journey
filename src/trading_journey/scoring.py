"""Per-ticker performance stats, the scan order, and the order in which buys get capital."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .sizing import MIN_REWARD_RISK, RATING_MULT
from .store import Store

DIRECTION = {"Buy": 1, "Overweight": 1, "Underweight": -1, "Sell": -1}


@dataclass
class TickerStats:
    ticker: str
    trades: int = 0
    wins: int = 0
    avg_r: float | None = None
    total_pnl: float = 0.0
    alpha_hit_rate: float | None = None
    decisions: int = 0
    last_analyzed: datetime | None = None

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None

    @property
    def quality(self) -> float | None:
        """0..1 from realized results and settled TradingAgents calls; None without history."""
        parts = []
        if self.avg_r is not None:
            parts.append((0.5, min(max((self.avg_r + 1) / 3, 0.0), 1.0)))  # -1R -> 0, +2R -> 1
        if self.win_rate is not None:
            parts.append((0.2, self.win_rate))
        if self.alpha_hit_rate is not None:
            parts.append((0.2, self.alpha_hit_rate))
        if not parts:
            return None
        return sum(w * v for w, v in parts) / sum(w for w, _ in parts)

    @property
    def score_mult(self) -> float:
        """Sizing multiplier 0.5..1.5; neutral 1.0 without history."""
        q = self.quality
        return 1.0 if q is None else 0.5 + q

    @property
    def demoted(self) -> bool:
        return self.trades >= 5 and (self.avg_r or 0) < 0


def _alpha_hits(memory_log_path: str | None) -> dict[str, float]:
    """Share of settled directional TradingAgents calls that beat the benchmark, per ticker."""
    if not memory_log_path:
        return {}
    try:
        from tradingagents.memory import TradingMemoryLog

        entries = TradingMemoryLog({"memory_log_path": memory_log_path}).load_entries()
    except Exception:
        return {}
    hits: dict[str, list[int]] = {}
    for e in entries:
        direction = DIRECTION.get(e.get("rating"), 0)
        text = (e.get("alpha") or "").strip().rstrip("%")
        if e.get("pending") or not direction or not text:
            continue
        try:
            alpha = float(text)
        except ValueError:
            continue
        hits.setdefault(e["ticker"].upper(), []).append(int(alpha * direction > 0))
    return {t: sum(v) / len(v) for t, v in hits.items()}


def ticker_stats(store: Store, universe: list[str], memory_log_path: str | None = None) -> dict[str, TickerStats]:
    stats = {t: TickerStats(t) for t in universe}
    by_ticker: dict[str, list] = {}
    for row in store.closed_trades():
        by_ticker.setdefault(row["ticker"], []).append(row)
    for ticker, rows in by_ticker.items():
        s = stats.setdefault(ticker, TickerStats(ticker))
        s.trades = len(rows)
        s.wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
        s.total_pnl = sum(r["pnl"] or 0 for r in rows)
        rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
        s.avg_r = sum(rs) / len(rs) if rs else None
    for ticker, n in store.decision_counts().items():
        stats.setdefault(ticker, TickerStats(ticker)).decisions = n
    for ticker, ts in store.last_analyzed().items():
        stats.setdefault(ticker, TickerStats(ticker)).last_analyzed = datetime.fromisoformat(ts)
    for ticker, rate in _alpha_hits(memory_log_path).items():
        if ticker in stats:
            stats[ticker].alpha_hit_rate = rate
    return stats


def priority(s: TickerStats, now: datetime) -> float:
    quality = s.quality if s.quality is not None else 0.5
    hours = max((now - s.last_analyzed).total_seconds() / 3600, 0) if s.last_analyzed else 72
    staleness = min(hours / 48, 1.0) * 0.3
    exploration = 0.3 if s.decisions < 3 else 0.0
    return 0.9 * quality + staleness + exploration


SCREEN_WEIGHT = 1.5  # today's screen outweighs the track record (priority spans ~0..1.5)


def rank_candidates(
    stats: dict[str, TickerStats],
    exclude: set[str],
    reanalyze_hours: float,
    now: datetime | None = None,
    screen: dict[str, float] | None = None,
) -> list[str]:
    """Tickers eligible for a fresh scan, best first: today's screener score plus track record."""
    now = now or datetime.now(timezone.utc)
    eligible = []
    for t, s in stats.items():
        if t in exclude:
            continue
        if s.last_analyzed and now - s.last_analyzed < timedelta(hours=reanalyze_hours):
            continue
        if s.demoted and s.last_analyzed and now - s.last_analyzed < timedelta(days=7):
            continue
        eligible.append(t)
    # Tiny daily jitter breaks ties, so a fresh start doesn't scan one sector first.
    jitter = random.Random(now.date().toordinal())
    screen = screen or {}
    keyed = {t: priority(stats[t], now) + SCREEN_WEIGHT * screen.get(t, 0.0) + jitter.random() * 1e-3
             for t in sorted(eligible)}
    return sorted(eligible, key=keyed.__getitem__, reverse=True)


def conviction(rating: str, entry: float | None, stop: float | None, target: float | None,
               screen_score: float = 0.0) -> float:
    """Order in which one scan's bullish calls get the free slots and cash.

    The rating dominates (Buy before Overweight), then the agents' own
    reward:risk, then how strongly the stock screened today.
    """
    rr = MIN_REWARD_RISK
    if entry and stop and target and entry > stop:
        rr = (target - entry) / (entry - stop)
    return RATING_MULT.get(rating, 0.0) + 0.1 * min(max(rr, 0.0), 4.0) + 0.2 * screen_score
