"""Measurement: where TradingAgents' tokens go, and whether its calls are any good.

Four parts, all from what the journal already holds plus daily prices (no LLM calls):

- cost per agent: tokens, $, seconds, tool calls and wrap-ups of each TradingAgents
  agent, from the per-agent usage every decision stores;
- decision quality: for every rating, traded or not, the stock's return 5/10/20
  trading days later against SPY, and whether the agents' own stop or target
  would have been touched first (settled once a day by `settle_outcomes`);
- level overrides and skips: how often the agents' entry/stop/target were
  replaced by the sizing rules, and why calls didn't become trades;
- execution and cost per result: slippage, time to fill, orders Claude didn't
  place, and API cost per trade and per winning trade.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from .agent import _iso, _ts
from .benchmark import SPY, _daily_bars, bar_exit
from .config import SETTINGS, Settings
from .decision import BEARISH, BULLISH
from .store import Store

logger = logging.getLogger("trading_journey.measure")

HORIZONS = (5, 10, 20)
FIRST_HIT_MAX_DAYS = 20
LOOKBACK_DAYS = 180  # the daily-bar download covers about six months
AGENT_WINDOW_DAYS = 30
RATING_ORDER = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]


def direction(rating: str) -> int:
    return 1 if rating in BULLISH else -1 if rating in BEARISH else 0


def traded(action: str | None) -> bool:
    """Whether a decision opened (or queued) a new position."""
    return (action or "").startswith(("buy", "short", "pending limit"))


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _median(values) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


class Measure:
    def __init__(self, store: Store, settings: Settings = SETTINGS, bars_fn=_daily_bars):
        self.store = store
        self.settings = settings
        self.bars_fn = bars_fn

    # ---------------------------------------------------------------- outcomes
    def settle_outcomes(self, now: datetime, force: bool = False) -> int:
        """Fill in the forward returns and first-hit of past decisions, once per New York day.

        Returns the number of decisions updated.
        """
        today = pd.Timestamp(now).tz_convert("America/New_York").date()
        done = self.store.get_kv("outcomes_date")
        if done and done["value"] == today.isoformat() and not force:
            return 0
        cutoff = _iso(now - timedelta(days=LOOKBACK_DAYS))
        pending = [dict(r) for r in self.store.rows(
            "SELECT d.id, d.ticker, d.trade_date, d.rating, d.price, d.stop, d.target, d.horizon_days"
            " FROM decisions d LEFT JOIN decision_outcomes o ON o.decision_id = d.id"
            " WHERE d.ts >= ? AND o.settled_ts IS NULL", (cutoff,))]
        updated = 0
        if pending:
            bars = self.bars_fn(sorted({r["ticker"] for r in pending} | {SPY})) or {}
            for r in pending:
                row = self._outcome(r, bars.get(r["ticker"]), bars.get(SPY), today, now)
                if row:
                    self.store.upsert_outcome(**row)
                    updated += 1
        self.store.set_kv("outcomes_date", today.isoformat())
        logger.info("Settled outcomes for %d of %d pending decisions", updated, len(pending))
        return updated

    def _outcome(self, r: dict, bars: pd.DataFrame | None, spy: pd.DataFrame | None, today, now) -> dict | None:
        if bars is None or spy is None:
            return None
        trade_day = pd.Timestamp(r["trade_date"]).date()

        def split(df):
            df = df.dropna(subset=["Close"])
            days = [pd.Timestamp(i).date() for i in df.index]
            on = df[[d == trade_day for d in days]]
            # Only completed sessions after the call count; today's bar is still forming.
            after = df[[trade_day < d < today for d in days]]
            return on, after

        on, after = split(bars)
        spy_on, spy_after = split(spy)
        ref = r["price"] or (float(on["Open"].iloc[0]) if len(on) else None)
        spy_ref = float(spy_on["Open"].iloc[0]) if len(spy_on) else None
        if not ref or not spy_ref:
            return None
        out = {"decision_id": r["id"], "ref_price": ref}
        for n in HORIZONS:
            out[f"ret_{n}"] = float(after["Close"].iloc[n - 1]) / ref - 1 if len(after) >= n else None
            out[f"spy_{n}"] = float(spy_after["Close"].iloc[n - 1]) / spy_ref - 1 if len(spy_after) >= n else None

        # Would the agents' own levels have worked? Only levels on the right side of the price count.
        d = direction(r["rating"])
        first_hit = first_hit_days = None
        levels_ok = d and r["stop"] and r["target"] and d * (ref - r["stop"]) > 0 and d * (r["target"] - ref) > 0
        if levels_ok:
            window = min(r["horizon_days"] or FIRST_HIT_MAX_DAYS, FIRST_HIT_MAX_DAYS)
            for i, (_, bar) in enumerate(after.head(window).iterrows(), start=1):
                hit = bar_exit(bar, d, r["stop"], r["target"])
                if hit:
                    first_hit, first_hit_days = hit[1], i
                    break
            else:
                if len(after) >= window:
                    first_hit = "none"
        out.update(first_hit=first_hit, first_hit_days=first_hit_days)
        complete = out[f"ret_{HORIZONS[-1]}"] is not None and out[f"spy_{HORIZONS[-1]}"] is not None
        out["settled_ts"] = _iso(now) if complete and (first_hit or not levels_ok) else None
        return out

    # ----------------------------------------------------------------- summary
    def summary(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        decisions = [dict(r) for r in self.store.rows(
            "SELECT d.id, d.ts, d.ticker, d.rating, d.action, d.entry, d.stop, d.target, d.cost, d.usage,"
            " d.levels_replaced, o.ret_5, o.ret_10, o.ret_20, o.spy_5, o.spy_10, o.spy_20, o.first_hit,"
            " o.first_hit_days FROM decisions d LEFT JOIN decision_outcomes o ON o.decision_id = d.id"
            " ORDER BY d.ts")]
        return {
            "agents": self._agents(decisions, now),
            "quality": self._quality(decisions),
            "levels": self._levels(decisions),
            "execution": self._execution(),
            "cost": self._cost(decisions),
        }

    def _agents(self, decisions: list[dict], now: datetime) -> dict:
        since = _iso(now - timedelta(days=AGENT_WINDOW_DAYS))
        totals: dict[str, dict] = {}
        analyses = 0
        for r in decisions:
            if r["ts"] < since:
                continue
            agents = (json.loads(r["usage"] or "{}") or {}).get("agents")
            if not agents:
                continue
            analyses += 1
            for name, a in agents.items():
                t = totals.setdefault(name, {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cost": 0.0,
                                             "seconds": 0.0, "tool_calls": 0, "wrap_ups": 0})
                for k in t:
                    t[k] += a.get(k, 0)
        total_cost = sum(t["cost"] for t in totals.values())
        rows = [{"agent": name, **t,
                 "share": t["cost"] / total_cost if total_cost else None,
                 "cost_per_analysis": t["cost"] / analyses,
                 "tokens_per_call": (t["input"] + t["cache_read"] + t["output"]) / t["calls"] if t["calls"] else None,
                 "seconds_per_analysis": t["seconds"] / analyses}
                for name, t in totals.items()]
        return {"analyses": analyses, "days": AGENT_WINDOW_DAYS, "total_cost": total_cost,
                "rows": sorted(rows, key=lambda r: r["cost"], reverse=True)}

    def _quality(self, decisions: list[dict]) -> dict:
        by_rating = []
        for rating in RATING_ORDER + sorted({r["rating"] for r in decisions} - set(RATING_ORDER)):
            rows = [r for r in decisions if r["rating"] == rating]
            if not rows:
                continue
            d = direction(rating)
            out = {"rating": rating, "n": len(rows)}
            for n in HORIZONS:
                settled = [r for r in rows if r[f"ret_{n}"] is not None and r[f"spy_{n}"] is not None]
                out[f"settled_{n}"] = len(settled)
                if d:
                    out[f"ret_{n}"] = _mean(d * r[f"ret_{n}"] for r in settled)
                    out[f"alpha_{n}"] = _mean(d * (r[f"ret_{n}"] - r[f"spy_{n}"]) for r in settled)
                    out[f"hit_{n}"] = _mean(float(d * r[f"ret_{n}"] > 0) for r in settled)
                else:  # Hold: how much the stock moved anyway
                    out[f"abs_move_{n}"] = _mean(abs(r[f"ret_{n}"]) for r in settled)
            by_rating.append(out)

        directional = [r for r in decisions if direction(r["rating"])]
        hits = [r["first_hit"] for r in directional if r["first_hit"]]
        first_hit = {k: hits.count(k) for k in ("target", "stop", "none")}
        split = []
        for label, rows in (("traded", [r for r in directional if traded(r["action"])]),
                            ("not traded", [r for r in directional if not traded(r["action"])])):
            # (direction, return, SPY's return) of each settled call
            settled = [(direction(r["rating"]), r["ret_10"], r["spy_10"]) for r in rows
                       if r["ret_10"] is not None and r["spy_10"] is not None]
            split.append({"group": label, "n": len(rows), "settled_10": len(settled),
                          "ret_10": _mean(d * ret for d, ret, _ in settled),
                          "alpha_10": _mean(d * (ret - spy) for d, ret, spy in settled),
                          "hit_10": _mean(float(d * ret > 0) for d, ret, _ in settled)})
        return {"by_rating": by_rating, "first_hit": first_hit, "first_hit_days": _mean(
            r["first_hit_days"] for r in directional if r["first_hit"] in ("target", "stop")), "traded_split": split}

    def _levels(self, decisions: list[dict]) -> dict:
        sized = [json.loads(r["levels_replaced"]) for r in decisions if r["levels_replaced"] is not None]
        reasons: dict[str, int] = {}
        for replaced in sized:
            for reason in replaced:
                reasons[reason] = reasons.get(reason, 0) + 1
        directional = [r for r in decisions if direction(r["rating"])]
        actions: dict[str, int] = {}
        for r in decisions:
            action = r["action"] or "unknown"
            key = next((k for k in ("pending limit buy", "pending limit short", "buy", "short") if action.startswith(k)),
                       action)
            actions[key] = actions.get(key, 0) + 1
        return {
            "sized": len(sized),
            "any_replaced": _mean(float(bool(x)) for x in sized),
            "reasons": sorted(({"reason": k, "n": v, "share": v / len(sized)} for k, v in reasons.items()),
                              key=lambda x: x["n"], reverse=True),
            "directional": len(directional),
            "missing": {k: _mean(float(r[k] is None) for r in directional) for k in ("entry", "stop", "target")},
            "actions": sorted(({"action": k, "n": v} for k, v in actions.items()), key=lambda x: x["n"], reverse=True),
        }

    def _execution(self) -> dict:
        fills = [dict(r) for r in self.store.rows(
            "SELECT e.order_type, e.limit_price, e.side, e.created_ts, t.entry_price, t.entry_ts"
            " FROM trades t JOIN entries e ON t.entry_id = e.id")]

        def d(row):
            return -1 if row["side"] == "short" else 1

        market = [f for f in fills if f["order_type"] == "market" and f["limit_price"]]
        hours = {kind: [(_ts(f["entry_ts"]) - _ts(f["created_ts"])).total_seconds() / 3600
                        for f in fills if f["order_type"] == kind] for kind in ("market", "limit")}
        stops = [dict(r) for r in self.store.rows(
            "SELECT side, stop, exit_price FROM trades WHERE status = 'closed'"
            " AND exit_reason IN ('stop-loss', 'trailing stop') AND stop > 0")]
        return {
            "fills": len(fills),
            # Positive = paid more than planned (a long) or sold for less (a short): a cost.
            "entry_slippage_pct": _mean(d(f) * (f["entry_price"] / f["limit_price"] - 1) * 100 for f in market),
            "market_fills": len(market),
            "limit_fills": len(hours["limit"]),
            "hours_to_fill": {k: _median(v) for k, v in hours.items()},
            # Positive = exited worse than the stop level (gaps, slippage).
            "stop_slippage_pct": _mean(-d(s) * (s["exit_price"] / s["stop"] - 1) * 100 for s in stops),
            "stop_exits": len(stops),
            "entries": {r["status"]: r["n"] for r in self.store.rows(
                "SELECT status, COUNT(*) AS n FROM entries GROUP BY status")},
            "runs": {r["status"]: r["n"] for r in self.store.rows(
                "SELECT status, COUNT(*) AS n FROM runs GROUP BY status")},
        }

    def _cost(self, decisions: list[dict]) -> dict:
        total, costed = self.store.api_cost("")
        trades = self.store.rows("SELECT COUNT(*) AS n FROM trades")[0]["n"]
        closed = self.store.closed_trades()
        wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
        pnl = sum(t["pnl"] or 0 for t in closed)
        return {
            "total": total, "analyses": costed, "trades": trades, "wins": wins, "realized_pnl": pnl,
            "per_analysis": total / costed if costed else None,
            "per_trade": total / trades if trades else None,
            "per_win": total / wins if wins else None,
            "pnl_per_dollar": pnl / total if total else None,
        }
