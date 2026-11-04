"""The benchmark: what the morning screener alone would have done, with no LLM.

Each morning scan, the screener's best setups (score >= MIN_SCORE, side from the
row's `bias`) open simulated trades with the agent's own rules: a 2x ATR stop,
a 2R target, the same risk-based sizing and caps, the same trailing stop and a
30-day horizon. Nothing is ordered: fills are simulated from daily bars,
conservatively (a gap through the stop fills at the open; a bar that touches
both the stop and the target counts as the stop).

`summary()` compares three things from the moment the benchmark started, with
the same starting money: the agent (real Alpaca paper account), this
screener-only strategy, and SPY buy-and-hold.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pandas as pd

from . import market_data, screener
from .agent import _dir, _iso, _ts, trail
from .config import SETTINGS, Settings
from .sizing import finalize_levels, position_size
from .store import Store

logger = logging.getLogger("trading_journey.benchmark")

MIN_SCORE = 0.6
HORIZON_DAYS = 30  # TradingAgents' usual horizon, so both strategies hold for as long
SPY = "SPY"
MAX_POINTS = 600


def _daily_bars(tickers: list[str]) -> dict[str, pd.DataFrame]:
    return screener._download(tickers)


def bar_exit(bar, d: int, stop: float, target: float) -> tuple[float, str] | None:
    """(fill price, "stop" | "target") if one daily bar reaches a level, else None. d: +1 long, -1 short.

    Conservative: a gap through a level fills at the open, and a bar that touches
    both counts as the stop.
    """
    o, h, lo = float(bar["Open"]), float(bar["High"]), float(bar["Low"])
    if d * (lo if d > 0 else h) <= d * stop:
        return (min(o, stop) if d > 0 else max(o, stop)), "stop"
    if d * (h if d > 0 else lo) >= d * target:
        return (max(o, target) if d > 0 else min(o, target)), "target"
    return None


def _bar_close_ts(day) -> str:
    """A completed daily bar's exit time: that session's 16:00 New York."""
    ts = pd.Timestamp(day).tz_localize("America/New_York") + pd.Timedelta(hours=16)
    return _iso(ts.to_pydatetime())


class Benchmark:
    def __init__(self, store: Store, settings: Settings = SETTINGS, universe: dict[str, str] | None = None,
                 equity_fn=None, quote_fn=market_data.quote, atr_fn=market_data.atr, bars_fn=_daily_bars):
        self.store = store
        self.settings = settings
        self.universe = universe or {}
        self.equity_fn = equity_fn  # the agent's account equity: starting capital and the comparison line
        self.quote_fn = quote_fn
        self.atr_fn = atr_fn
        self.bars_fn = bars_fn

    # ------------------------------------------------------------------ helpers
    def _try(self, fn, *args):
        try:
            return fn(*args)
        except Exception:
            return None

    def start(self) -> dict | None:
        row = self.store.get_kv("bench_start")
        return json.loads(row["value"]) if row else None

    def _ensure_start(self, now: datetime) -> dict:
        start = self.start()
        if start is None:
            capital = (self._try(self.equity_fn) if self.equity_fn else None) or 100_000.0
            start = {"ts": _iso(now), "capital": capital, "spy": self._try(self.quote_fn, SPY)}
            self.store.set_kv("bench_start", json.dumps(start))
            logger.info("Benchmark started with %.2f (SPY %s)", capital, start["spy"])
        return start

    def _realized(self) -> float:
        return sum(t["pnl"] or 0 for t in self.store.closed_bench_trades())

    def _marks(self) -> dict[str, float]:
        row = self.store.get_kv("bench_marks")
        return json.loads(row["value"]) if row else {}

    # ---------------------------------------------------------------- entries
    def open_from_screen(self, rows: list[dict], now: datetime) -> list[str]:
        """Simulated entries for today's best-screened setups. Returns the tickers opened."""
        start = self._ensure_start(now)
        open_trades = self.store.open_bench_trades()
        held = {t["ticker"] for t in open_trades}
        slots = self.settings.max_positions - len(open_trades)
        equity = start["capital"] + self._realized()
        cash = equity - sum(t["qty"] * t["entry_price"] for t in open_trades)
        sectors: dict[str, float] = {}
        for t in open_trades:
            sectors[t["sector"]] = sectors.get(t["sector"], 0) + t["qty"] * t["entry_price"]

        opened = []
        spy = self._try(self.quote_fn, SPY)
        for row in sorted(rows, key=lambda r: r["score"], reverse=True):
            if slots <= 0 or row["score"] < MIN_SCORE:
                break
            ticker = row["ticker"]
            side = row.get("bias") or "long"
            if ticker in held or (side == "short" and not self.settings.allow_shorts):
                continue
            price = self._try(self.quote_fn, ticker)
            if not price:
                continue
            levels = finalize_levels(price, None, None, None, self._try(self.atr_fn, ticker), side)
            if levels is None:
                continue
            sector = self.universe.get(ticker, "Other")
            qty = position_size(equity=equity, cash=cash, levels=levels, rating="Buy" if side == "long" else "Sell",
                                score_mult=1.0, sector_exposure=sectors.get(sector, 0), settings=self.settings)
            if qty < 1:
                continue
            self.store.add_bench_trade(
                ticker=ticker, sector=sector, side=side, qty=qty, screen_score=row["score"],
                entry_price=levels.entry, entry_ts=_iso(now), stop=levels.stop, initial_stop=levels.stop,
                target=levels.target, horizon_until=_iso(now + timedelta(days=HORIZON_DAYS)),
                high_water=levels.entry, spy_entry=spy,
            )
            cost = qty * levels.entry
            cash -= cost
            sectors[sector] = sectors.get(sector, 0) + cost
            slots -= 1
            held.add(ticker)
            opened.append(ticker)
            logger.info("Benchmark %s %s x%d @ %.2f stop %.2f target %.2f", side, ticker, qty, levels.entry,
                        levels.stop, levels.target)
        self._snapshot(now)
        return opened

    # ------------------------------------------------------------------ exits
    def update(self, now: datetime) -> list[str]:
        """Simulate stops, take-profits, trailing and time exits; snapshot equity. Returns the tickers closed."""
        if self.start() is None:  # starts at the first morning scan
            return []
        open_trades = [dict(t) for t in self.store.open_bench_trades()]
        closed = []
        if open_trades:
            bars = self._try(self.bars_fn, sorted({t["ticker"] for t in open_trades} | {SPY})) or {}
            today = pd.Timestamp(now).tz_convert("America/New_York").date()
            for t in open_trades:
                if self._step(t, bars.get(t["ticker"]), bars.get(SPY), today, now):
                    closed.append(t["ticker"])
        self._snapshot(now)
        return closed

    def _step(self, t: dict, bars: pd.DataFrame | None, spy_bars: pd.DataFrame | None, today, now: datetime) -> bool:
        """Walk one trade through the bars it hasn't seen yet. True if it closed."""
        d = _dir(t)
        # The entry day's bar started before the entry: only later bars count.
        entry_day = pd.Timestamp(_ts(t["entry_ts"])).tz_convert("America/New_York").date()
        checked = pd.Timestamp(t["checked_through"]).date() if t.get("checked_through") else entry_day
        updates = {}
        if bars is not None:
            for idx, bar in bars.dropna(subset=["Close"]).iterrows():
                day = pd.Timestamp(idx).date()
                if day <= checked:
                    continue
                hit = bar_exit(bar, d, t["stop"], t["target"])
                if hit:
                    price, level = hit
                    if level == "target":
                        reason = "take-profit"
                    else:
                        reason = "trailing stop" if d * (t["stop"] - t["initial_stop"]) > 0.01 else "stop-loss"
                    live = day >= today
                    self._close(t, price, _iso(now) if live else _bar_close_ts(day), reason,
                                self._spy_on(spy_bars, day) if not live else None, now)
                    return True
                if day >= today:  # today's bar is still forming: check it, but don't trail on it yet
                    break
                # The completed bar's best price trails the stop for the next bar.
                best, new_stop = trail(t, round(float(bar["High"] if d > 0 else bar["Low"]), 2))
                t.update(high_water=best, stop=new_stop, checked_through=day.isoformat())
                updates.update(high_water=best, stop=new_stop, checked_through=day.isoformat())
        if updates:
            self.store.update_bench_trade(t["id"], **updates)
        if now >= _ts(t["horizon_until"]):
            price = self._try(self.quote_fn, t["ticker"])
            if price:
                self._close(t, price, _iso(now), "time horizon", None, now)
                return True
        return False

    def _spy_on(self, spy_bars: pd.DataFrame | None, day) -> float | None:
        if spy_bars is None:
            return None
        rows = spy_bars[[pd.Timestamp(i).date() == day for i in spy_bars.index]]
        return float(rows["Close"].iloc[-1]) if len(rows) else None

    def _close(self, t: dict, price: float, exit_ts: str, reason: str, spy_exit: float | None, now: datetime) -> None:
        d = _dir(t)
        risk = abs(t["entry_price"] - t["initial_stop"])
        pnl = d * (price - t["entry_price"]) * t["qty"]
        self.store.update_bench_trade(
            t["id"], status="closed", exit_price=round(price, 2), exit_ts=exit_ts, exit_reason=reason,
            pnl=round(pnl, 2), r_multiple=round(d * (price - t["entry_price"]) / risk, 3) if risk > 0 else None,
            spy_exit=spy_exit if spy_exit is not None else self._try(self.quote_fn, SPY),
            stop=t["stop"], high_water=t["high_water"],
        )
        logger.info("Benchmark closed %s @ %.2f (%s) pnl %.2f", t["ticker"], price, reason, pnl)

    # ---------------------------------------------------------------- equity
    def _snapshot(self, now: datetime) -> None:
        start = self.start()
        if start is None:
            return
        marks = {}
        unrealized = 0.0
        for t in self.store.open_bench_trades():
            price = self._try(self.quote_fn, t["ticker"]) or t["entry_price"]
            marks[t["ticker"]] = price
            unrealized += _dir(t) * (price - t["entry_price"]) * t["qty"]
        self.store.set_kv("bench_marks", json.dumps(marks))
        equity = start["capital"] + self._realized() + unrealized
        agent_equity = self._try(self.equity_fn) if self.equity_fn else None
        self.store.bench_snapshot(round(equity, 2), agent_equity, self._try(self.quote_fn, SPY), _iso(now))

    # --------------------------------------------------------------- summary
    def summary(self) -> dict:
        """Agent vs. screener-only vs. SPY since the benchmark started (for the dashboard and the CLI)."""
        start = self.start()
        if start is None:
            return {"start": None}
        capital = start["capital"]
        snaps = [dict(r) for r in self.store.rows("SELECT * FROM bench_equity ORDER BY ts")]

        def pct(v, base):
            return (v / base - 1) * 100 if v is not None and base else None

        curve = [{"ts": s["ts"], "agent": pct(s["agent_equity"], capital), "bench": pct(s["equity"], capital),
                  "spy": pct(s["spy"], start["spy"])} for s in snaps]
        if len(curve) > MAX_POINTS:
            step = len(curve) // MAX_POINTS + 1
            curve = curve[::step] + [curve[-1]]
        last = curve[-1] if curve else {}

        marks = self._marks()
        bench_open = []
        for t in self.store.open_bench_trades():
            t = dict(t)
            price = marks.get(t["ticker"], t["entry_price"])
            d = _dir(t)
            risk = abs(t["entry_price"] - t["initial_stop"])
            bench_open.append({**t, "price": price, "pnl": d * (price - t["entry_price"]) * t["qty"],
                               "r_now": d * (price - t["entry_price"]) / risk if risk > 0 else None})
        bench_closed = [dict(t) for t in self.store.closed_bench_trades()]
        agent_closed = [dict(t) for t in self.store.closed_trades() if t["entry_ts"] >= start["ts"]]
        agent_open = [t for t in self.store.open_trades() if t["entry_ts"] >= start["ts"]]

        return {
            "start": start,
            "curve": curve,
            "agent": {**_stats(agent_closed, len(agent_open)), "return_pct": last.get("agent")},
            "bench": {**_stats(bench_closed, len(bench_open)), "return_pct": last.get("bench")},
            "spy": {"return_pct": last.get("spy")},
            "open": bench_open,
            "closed": list(reversed(bench_closed))[:200],
            "min_score": MIN_SCORE,
        }


def trade_alpha(t: dict) -> float | None:
    """The trade's return minus SPY's over the same holding period, in %."""
    if not (t.get("spy_entry") and t.get("spy_exit") and t.get("entry_price") and t.get("exit_price")):
        return None
    own = _dir(t) * (t["exit_price"] / t["entry_price"] - 1)
    return (own - (t["spy_exit"] / t["spy_entry"] - 1)) * 100


def _stats(closed: list[dict], open_count: int) -> dict:
    wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
    rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
    alphas = [a for a in (trade_alpha(t) for t in closed) if a is not None]
    return {
        "closed": len(closed), "open": open_count, "wins": wins, "losses": len(closed) - wins,
        "win_rate": wins / len(closed) if closed else None,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "pnl": sum(t["pnl"] or 0 for t in closed),
        "avg_alpha_pct": sum(alphas) / len(alphas) if alphas else None,
    }
