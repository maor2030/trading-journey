"""Planning and syncing for the trading agent.

`plan()` decides; it never trades. It returns intents, each with the exact Alpaca
MCP calls that Claude (the /trade skill) executes. `sync()` then reconciles the
journal with what actually happened at Alpaca. Every buy carries a
client_order_id, so sync finds its order even if Claude never reports back.

Long only. Entries come only from Buy/Overweight ratings. Stop-loss and
take-profit sit at Alpaca as bracket legs. Exits come from those legs, the
trailing-stop raises, the time horizon, or a Sell/Underweight re-analysis.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import decision as decision_mod
from . import market_data
from .alpaca import AlpacaClient
from .config import SETTINGS, Settings, tradingagents_config
from .decision import BEARISH, BULLISH, Decision
from .scoring import TickerStats, rank_candidates, ticker_stats
from .sizing import finalize_levels, position_size
from .store import Store
from .universe import load_universe

logger = logging.getLogger("trading_journey.agent")

DONE_ORDER_STATUSES = {"canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}
UNPLACED_GRACE = timedelta(minutes=45)


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _price(value) -> str:
    return f"{value:.2f}"


@dataclass
class Intent:
    id: str
    kind: str  # buy | exit | cancel_entry | raise_stop | update_target
    ticker: str
    reason: str
    calls: list[dict] = field(default_factory=list)


@dataclass
class Budget:
    """What's left for new buys while one plan is being built."""
    cash: float
    slots: int
    sector_exposure: dict[str, float]


class TradingAgent:
    def __init__(
        self,
        store: Store,
        broker: AlpacaClient,
        settings: Settings = SETTINGS,
        universe: dict[str, str] | None = None,
        analyze_fn=None,
        quote_fn=market_data.quote,
        atr_fn=market_data.atr,
        expiry_fn=market_data.add_trading_days,
    ):
        self.store = store
        self.broker = broker
        self.settings = settings
        self.universe = universe if universe is not None else load_universe()
        self.analyze_fn = analyze_fn or (lambda t, d, portfolio: decision_mod.analyze(t, d, portfolio, settings))
        self.quote_fn = quote_fn
        self.atr_fn = atr_fn
        self.expiry_fn = expiry_fn

    # ------------------------------------------------------------------ helpers
    def halted(self) -> bool:
        return self.settings.kill_switch.exists()

    def memory_log_path(self) -> str | None:
        try:
            return tradingagents_config(self.settings).get("memory_log_path")
        except Exception:
            return None

    def _portfolio_context(self):
        from tradingagents.portfolio import PortfolioContext, Position

        account = self.broker.account()
        return PortfolioContext(
            cash=account.cash,
            currency="USD",
            positions=[Position(ticker=p.ticker, quantity=p.qty, average_price=p.avg_price) for p in self.broker.positions()],
        )

    def _buys_paused(self, equity: float, now: datetime) -> bool:
        ny_midnight = pd.Timestamp(market_data.trade_date(now)).tz_localize("America/New_York").tz_convert("UTC")
        start = self.store.first_equity_since(ny_midnight.isoformat())
        return bool(start) and (equity - start) / start <= -self.settings.daily_loss_limit

    def _trail(self, trade, price: float) -> tuple[float, float]:
        """(new high-water, stop after trailing). Breakeven at +1R, high minus 1R from +2R."""
        high = max(trade["high_water"] or trade["entry_price"], price)
        risk = trade["entry_price"] - trade["initial_stop"]
        stop = trade["stop"]
        if risk > 0:
            gain = high - trade["entry_price"]
            if gain >= 2 * risk:
                stop = max(stop, round(high - risk, 2))
            elif gain >= risk:
                stop = max(stop, round(trade["entry_price"], 2))
        return high, stop

    # --------------------------------------------------------------------- sync
    def sync(self, now: datetime | None = None) -> dict:
        """Reconcile entries and trades with Alpaca; snapshot equity."""
        now = now or datetime.now(timezone.utc)
        changes = {"filled": [], "closed": [], "entries_closed": []}
        held = {p.ticker: p for p in self.broker.positions()}

        for e in self.store.live_entries():
            order = self.broker.order_by_client_id(e["client_order_id"])
            if order is None:
                if e["status"] == "planned" and now - _ts(e["created_ts"]) > UNPLACED_GRACE:
                    self.store.update_entry(e["id"], status="not_placed")
                    changes["entries_closed"].append((e["ticker"], "not_placed"))
                continue
            status = order["status"]
            if status == "filled":
                self._open_trade_from(e, order, now)
                self.store.update_entry(e["id"], status="filled", order_id=order["id"])
                changes["filled"].append(e["ticker"])
            elif status in DONE_ORDER_STATUSES:
                self.store.update_entry(e["id"], status=status, order_id=order["id"])
                changes["entries_closed"].append((e["ticker"], status))
            elif e["status"] == "planned":
                self.store.update_entry(e["id"], status="open", order_id=order["id"])

        for t in self.store.open_trades():
            if t["ticker"] in held:
                self._refresh_legs(t, held[t["ticker"]].price)
            else:
                self._close_trade(t, now)
                changes["closed"].append(t["ticker"])

        account = self.broker.account()
        self.store.snapshot(account.equity, account.cash, _iso(now))
        return changes

    def _legs(self, order: dict) -> tuple[dict | None, dict | None]:
        """Current (stop, take-profit) legs of a bracket, following replacements."""
        stop_leg = tp_leg = None
        for leg in order.get("legs") or []:
            for _ in range(10):
                if not leg.get("replaced_by"):
                    break
                leg = self.broker.order(leg["replaced_by"])
            if leg.get("type") in ("stop", "stop_limit"):
                stop_leg = leg
            elif leg.get("type") == "limit":
                tp_leg = leg
        return stop_leg, tp_leg

    def _open_trade_from(self, e, order: dict, now: datetime) -> None:
        stop_leg, tp_leg = self._legs(order)
        fill = float(order["filled_avg_price"])
        stop = float(stop_leg["stop_price"]) if stop_leg else e["stop"]
        filled_at = _ts(order["filled_at"]) if order.get("filled_at") else now
        self.store.add_trade(
            entry_id=e["id"], ticker=e["ticker"], sector=e["sector"], rating=e["rating"],
            qty=int(float(order["filled_qty"])), entry_price=fill, entry_ts=_iso(filled_at),
            order_id=order["id"], stop_order_id=stop_leg["id"] if stop_leg else None,
            tp_order_id=tp_leg["id"] if tp_leg else None, stop=stop, initial_stop=stop,
            target=float(tp_leg["limit_price"]) if tp_leg else e["target"],
            horizon_until=_iso(filled_at + timedelta(days=e["horizon_days"])), high_water=fill,
        )
        logger.info("Filled %s x%s @ %.2f", e["ticker"], order["filled_qty"], fill)

    def _refresh_legs(self, t, price: float) -> None:
        updates = {"high_water": max(t["high_water"] or t["entry_price"], price)}
        if t["order_id"]:
            stop_leg, tp_leg = self._legs(self.broker.order(t["order_id"]))
            if stop_leg:
                updates.update(stop=float(stop_leg["stop_price"]), stop_order_id=stop_leg["id"])
            if tp_leg:
                updates.update(target=float(tp_leg["limit_price"]), tp_order_id=tp_leg["id"])
        self.store.update_trade(t["id"], **updates)

    def _close_trade(self, t, now: datetime) -> None:
        exit_price, reason, exit_ts = None, None, _iso(now)
        if t["order_id"]:
            stop_leg, tp_leg = self._legs(self.broker.order(t["order_id"]))
            for leg, name in ((stop_leg, "stop"), (tp_leg, "take-profit")):
                if leg and leg.get("status") == "filled":
                    exit_price = float(leg["filled_avg_price"])
                    exit_ts = leg.get("filled_at") or exit_ts
                    reason = name if name == "take-profit" else (
                        "trailing stop" if (t["stop"] or 0) > t["initial_stop"] else "stop-loss")
        if exit_price is None:  # closed by close_position (signal / time horizon) or by hand
            sells = [o for o in self.broker.orders(t["ticker"], status="closed", after=t["entry_ts"])
                     if o.get("side") == "sell" and o.get("status") == "filled"]
            if sells:
                last = max(sells, key=lambda o: o.get("filled_at") or "")
                exit_price, exit_ts = float(last["filled_avg_price"]), last.get("filled_at") or exit_ts
            else:
                exit_price = self.quote_fn(t["ticker"])
            reason = t["planned_exit"] or "manual"
        risk = t["entry_price"] - t["initial_stop"]
        pnl = (exit_price - t["entry_price"]) * t["qty"]
        self.store.update_trade(
            t["id"], status="closed", exit_price=exit_price, exit_ts=exit_ts, exit_reason=reason,
            pnl=round(pnl, 2), r_multiple=round((exit_price - t["entry_price"]) / risk, 3) if risk > 0 else None,
        )
        logger.info("Closed %s @ %.2f (%s) pnl %.2f", t["ticker"], exit_price, reason, pnl)

    # --------------------------------------------------------------------- plan
    def plan(self, n: int | None = None, tickers: list[str] | None = None,
             dry_run: bool = False, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        plan_id = now.strftime("%Y%m%d%H%M") + "-" + uuid.uuid4().hex[:4]
        result = {"plan_id": plan_id, "created": _iso(now), "dry_run": dry_run, "intents": [], "decisions": []}
        if self.halted():
            result["halted"] = f"kill switch present: {self.settings.kill_switch}"
            return result

        result["sync"] = self.sync(now)
        account = self.broker.account()
        result["account"] = asdict(account)
        intents: list[Intent] = []
        trades = {t["ticker"]: t for t in self.store.open_trades()}
        entries = {e["ticker"]: e for e in self.store.live_entries()}
        prices = {p.ticker: p.price for p in self.broker.positions()}

        # 1. Housekeeping, no LLM: time exits, trailing stops, stale limit entries.
        for t in trades.values():
            price = prices.get(t["ticker"]) or self.quote_fn(t["ticker"])
            if now >= _ts(t["horizon_until"]):
                intents.append(self._exit_intent(t, "time horizon", plan_id, dry_run))
                continue
            high, stop = self._trail(t, price)
            if stop > t["stop"] + 0.01 and t["stop_order_id"]:
                intents.append(self._stop_intent(t, stop, f"trailing stop (high {high:.2f})", plan_id, dry_run))
        for e in entries.values():
            if e["expires_ts"] and now >= _ts(e["expires_ts"]) and e["order_id"]:
                intents.append(self._cancel_intent(e, "limit entry expired", plan_id, dry_run))

        # 2. Scan: held positions due for review first, then the best-ranked candidates.
        reserved = sum(e["qty"] * (e["limit_price"] or 0) for e in entries.values())
        budget = Budget(
            cash=account.cash - reserved,
            slots=self.settings.max_positions - len(trades) - len(entries),
            sector_exposure={},
        )
        for t in trades.values():
            budget.sector_exposure[t["sector"]] = budget.sector_exposure.get(t["sector"], 0) + t["qty"] * t["entry_price"]
        for e in entries.values():
            budget.sector_exposure[e["sector"]] = budget.sector_exposure.get(e["sector"], 0) + e["qty"] * (e["limit_price"] or 0)

        stats = ticker_stats(self.store, list(self.universe), self.memory_log_path())
        n = n or self.settings.scans_per_hour
        if tickers:
            picks = [t.upper() for t in tickers][:n]
        else:
            picks = self.next_picks(n, trades, entries, budget.slots, stats, now)

        exiting = {i.ticker for i in intents if i.kind == "exit"}
        paused = self._buys_paused(account.equity, now)
        for ticker in picks:
            try:
                d = self.analyze_fn(ticker, market_data.trade_date(now), portfolio=self._portfolio_context())
            except Exception as exc:  # one failed analysis must not end the plan
                logger.exception("Analysis of %s failed", ticker)
                result["decisions"].append({"ticker": ticker, "rating": "ERROR", "action": str(exc)})
                continue
            action, intent = self._decide(d, trades.get(ticker), entries.get(ticker), stats.get(ticker),
                                          budget, paused, ticker in exiting, plan_id, dry_run, now)
            if intent:
                intents.append(intent)
            if not dry_run:
                self.store.record_decision(d, action, _iso(now), plan_id)
            result["decisions"].append({"ticker": ticker, "rating": d.rating, "action": action, "summary": d.summary})
            logger.info("%s -> %s: %s", ticker, d.rating, action)

        result["intents"] = [asdict(i) for i in intents]
        self.settings.plans_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.plans_dir / f"{plan_id}{'-dry' if dry_run else ''}.json"
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["path"] = str(path)
        return result

    def next_picks(self, n: int, trades: dict, entries: dict, free_slots: int,
                   stats: dict[str, TickerStats], now: datetime) -> list[str]:
        """Held positions due for review first, then the best-ranked candidates."""
        window = timedelta(hours=self.settings.reanalyze_hours)
        last = self.store.last_analyzed()
        due = [t for t in trades if t not in last or now - _ts(last[t]) >= window]
        # No free slot: analyzing new names would only spend money.
        fresh = []
        if free_slots > 0:
            fresh = rank_candidates(stats, set(trades) | set(entries), self.settings.reanalyze_hours, now)
        return (due + fresh)[:n]

    def _decide(self, d: Decision, trade, entry, stats: TickerStats | None, budget: Budget,
                paused: bool, exiting: bool, plan_id: str, dry_run: bool, now: datetime):
        if trade:
            if exiting:
                return "exit already planned", None
            if d.rating in BEARISH:
                return "exit (signal)", self._exit_intent(trade, "signal", plan_id, dry_run)
            price = self.quote_fn(d.ticker)
            if d.stop and trade["stop"] + 0.01 < d.stop < price and trade["stop_order_id"]:
                return "hold, raise stop", self._stop_intent(trade, d.stop, "agents raised the stop", plan_id, dry_run)
            if d.target and d.target > price and abs(d.target - trade["target"]) / trade["target"] > 0.01 \
                    and trade["tp_order_id"]:
                return "hold, new target", self._target_intent(trade, d.target, plan_id)
            return "hold", None

        if entry:
            if d.rating not in BULLISH:
                if entry["order_id"]:
                    return "cancel pending entry", self._cancel_intent(entry, f"rating now {d.rating}", plan_id, dry_run)
                return "no action (entry not placed yet)", None
            return "keep pending entry", None

        if d.rating not in BULLISH:
            return "no action", None
        if paused:
            return "skip: daily loss limit reached", None
        if budget.slots <= 0:
            return "skip: no free position slot", None

        price = self.quote_fn(d.ticker)
        levels = finalize_levels(price, d.entry, d.stop, d.target, self.atr_fn(d.ticker))
        if levels is None:
            return "skip: no valid stop-loss", None
        sector = self.universe.get(d.ticker, "Other")
        qty = position_size(
            equity=self.broker.account().equity, cash=budget.cash, levels=levels, rating=d.rating,
            score_mult=(stats or TickerStats(d.ticker)).score_mult,
            sector_exposure=budget.sector_exposure.get(sector, 0), settings=self.settings,
        )
        if qty < 1:
            return "skip: position size is 0 (caps/cash)", None

        wait = levels.entry < price * 0.995
        cost = qty * (levels.entry if wait else price)
        budget.cash -= cost
        budget.slots -= 1
        budget.sector_exposure[sector] = budget.sector_exposure.get(sector, 0) + cost

        client_order_id = f"tj-{plan_id}-{d.ticker}"
        args = {
            "symbol": d.ticker, "side": "buy", "qty": str(qty),
            # gtc even for market entries: bracket legs inherit it, and "day" legs
            # would expire at the close and leave the position unprotected.
            "type": "limit" if wait else "market", "time_in_force": "gtc",
            "order_class": "bracket", "take_profit_limit_price": _price(levels.target),
            "stop_loss_stop_price": _price(levels.stop), "client_order_id": client_order_id,
        }
        if wait:
            args["limit_price"] = _price(levels.entry)
        plan_text = f"{qty} @ {'limit ' + _price(levels.entry) if wait else 'market'} stop {levels.stop} target {levels.target}"
        if not dry_run:
            self.store.add_entry(
                plan_id=plan_id, client_order_id=client_order_id, ticker=d.ticker, sector=sector,
                rating=d.rating, qty=qty, order_type=args["type"], limit_price=levels.entry if wait else price,
                stop=levels.stop, target=levels.target, horizon_days=d.horizon_days, created_ts=_iso(now),
                expires_ts=_iso(self.expiry_fn(now, self.settings.limit_expiry_days)) if wait else None,
            )
        intent = Intent(f"{plan_id}-{d.ticker}-buy", "buy", d.ticker,
                        f"{d.rating}: {d.summary[:200]}", [{"tool": "place_stock_order", "args": args}])
        return f"{'pending limit' if wait else 'buy'} {plan_text}", intent

    # ----------------------------------------------------------------- intents
    def _exit_intent(self, t, reason: str, plan_id: str, dry_run: bool) -> Intent:
        if not dry_run:
            self.store.update_trade(t["id"], planned_exit=reason)
        calls = [{"tool": "cancel_order_by_id", "args": {"order_id": oid}}
                 for oid in (t["stop_order_id"], t["tp_order_id"]) if oid]
        calls.append({"tool": "close_position", "args": {"symbol_or_asset_id": t["ticker"]}})
        return Intent(f"{plan_id}-{t['ticker']}-exit", "exit", t["ticker"], reason, calls)

    def _stop_intent(self, t, stop: float, reason: str, plan_id: str, dry_run: bool) -> Intent:
        return Intent(f"{plan_id}-{t['ticker']}-stop", "raise_stop", t["ticker"], f"{reason}: {t['stop']} -> {stop:.2f}",
                      [{"tool": "replace_order_by_id", "args": {"order_id": t["stop_order_id"], "stop_price": _price(stop)}}])

    def _target_intent(self, t, target: float, plan_id: str) -> Intent:
        return Intent(f"{plan_id}-{t['ticker']}-target", "update_target", t["ticker"], f"target {t['target']} -> {target:.2f}",
                      [{"tool": "replace_order_by_id", "args": {"order_id": t["tp_order_id"], "limit_price": _price(target)}}])

    def _cancel_intent(self, e, reason: str, plan_id: str, dry_run: bool = False) -> Intent:
        return Intent(f"{plan_id}-{e['ticker']}-cancel", "cancel_entry", e["ticker"], reason,
                      [{"tool": "cancel_order_by_id", "args": {"order_id": e["order_id"]}}])
