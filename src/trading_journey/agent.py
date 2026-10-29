"""The autonomous agent: hourly TradingAgents scans + a frequent no-LLM risk monitor.

Long only. Entries come only from Buy/Overweight ratings; exits come from a
Sell/Underweight re-analysis, the stop-loss (with trailing), the take-profit,
or the time horizon. Stops, targets and pending limit entries are managed here
rather than at the broker, so every broker adapter behaves the same.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import decision as decision_mod
from . import market_data
from .brokers import Broker
from .config import SETTINGS, Settings, tradingagents_config
from .decision import BEARISH, BULLISH, Decision
from .scoring import TickerStats, rank_candidates, ticker_stats
from .sizing import finalize_levels, position_size
from .store import Store, now_iso
from .universe import load_universe

logger = logging.getLogger("trading_journey.agent")


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


class TradingAgent:
    def __init__(
        self,
        store: Store,
        broker: Broker,
        settings: Settings = SETTINGS,
        universe: dict[str, str] | None = None,
        analyze_fn=None,
        atr_fn=market_data.atr,
        expiry_fn=market_data.add_trading_days,
    ):
        self.store = store
        self.broker = broker
        self.settings = settings
        self.universe = universe if universe is not None else load_universe()
        self.analyze_fn = analyze_fn or (lambda t, d, portfolio: decision_mod.analyze(t, d, portfolio, settings))
        self.atr_fn = atr_fn
        self.expiry_fn = expiry_fn
        # Order-placing sections of scan and monitor never interleave.
        self._trade_lock = threading.RLock()

    # ------------------------------------------------------------------ helpers
    def halted(self) -> bool:
        return self.settings.kill_switch.exists()

    def _memory_log_path(self) -> str | None:
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

    def _open_trade_for(self, ticker: str):
        return next((t for t in self.store.open_trades() if t["ticker"] == ticker), None)

    def _pending_for(self, ticker: str):
        return next((p for p in self.store.open_pending() if p["ticker"] == ticker), None)

    def _buys_paused(self, now: datetime) -> bool:
        ny_midnight = pd.Timestamp(market_data.trade_date(now)).tz_localize("America/New_York").tz_convert("UTC")
        start = self.store.first_equity_since(ny_midnight.isoformat())
        if not start:
            return False
        return (self.broker.account().equity - start) / start <= -self.settings.daily_loss_limit

    # --------------------------------------------------------------------- scan
    def scan(self, n: int | None = None, tickers: list[str] | None = None,
             dry_run: bool = False, now: datetime | None = None) -> list[tuple[str, str]]:
        """Analyze up to n tickers (held positions due for review first). Returns (ticker, action)."""
        if self.halted():
            logger.warning("Kill switch present (%s); scan skipped", self.settings.kill_switch)
            return []
        now = now or datetime.now(timezone.utc)
        n = n or self.settings.scans_per_hour
        stats = ticker_stats(self.store, list(self.universe), self._memory_log_path())

        if tickers:
            picks = [t.upper() for t in tickers]
        else:
            held = [t["ticker"] for t in self.store.open_trades()]
            window = timedelta(hours=self.settings.reanalyze_hours)
            last = self.store.last_analyzed()
            due_held = [t for t in held if t not in last or now - _ts(last[t]) >= window]
            picks = (due_held + rank_candidates(stats, set(held), self.settings.reanalyze_hours, now))[:n]

        results = []
        for ticker in picks:
            try:
                d = self.analyze_fn(ticker, market_data.trade_date(now), portfolio=self._portfolio_context())
            except Exception as exc:  # one failed analysis must not end the scan
                logger.exception("Analysis of %s failed: %s", ticker, exc)
                results.append((ticker, f"error: {exc}"))
                continue
            with self._trade_lock:
                action = self.handle(d, stats.get(ticker) or TickerStats(ticker), dry_run, now)
            if not dry_run:
                self.store.record_decision(d, action, now.isoformat(timespec="seconds"))
            logger.info("%s -> %s: %s", ticker, d.rating, action)
            results.append((ticker, f"{d.rating}: {action}"))
        return results

    def handle(self, d: Decision, stats: TickerStats, dry_run: bool, now: datetime) -> str:
        trade = self._open_trade_for(d.ticker)
        pending = self._pending_for(d.ticker)

        if trade:
            if d.rating in BEARISH:
                if dry_run:
                    return "dry-run: would exit"
                self._close(trade, "signal")
                return "exit (signal)"
            return self._refresh_levels(trade, d, dry_run)

        if d.rating not in BULLISH:
            if pending and not dry_run:
                self.store.close_pending(pending["id"], "cancelled")
                return "cancelled pending entry"
            return "no action"

        if self._buys_paused(now):
            return "skip: daily loss limit reached"
        open_count = len(self.store.open_trades()) + len(self.store.open_pending()) - (1 if pending else 0)
        if open_count >= self.settings.max_positions:
            return "skip: no free position slot"

        price = self.broker.quote(d.ticker)
        levels = finalize_levels(price, d.entry, d.stop, d.target, self.atr_fn(d.ticker))
        if levels is None:
            return "skip: no valid stop-loss"

        account = self.broker.account()
        other_pending = [p for p in self.store.open_pending() if not pending or p["id"] != pending["id"]]
        reserved = sum(p["qty"] * p["limit_price"] for p in other_pending)
        sector = self.universe.get(d.ticker, "Other")
        sector_exposure = sum(t["qty"] * t["entry_price"] for t in self.store.open_trades() if t["sector"] == sector)
        sector_exposure += sum(p["qty"] * p["limit_price"] for p in other_pending if p["sector"] == sector)
        qty = position_size(
            equity=account.equity, cash=account.cash - reserved, levels=levels, rating=d.rating,
            score_mult=stats.score_mult, sector_exposure=sector_exposure, settings=self.settings,
        )
        if qty < 1:
            return "skip: position size is 0 (caps/cash)"

        wait = levels.entry < price * 0.995
        plan = f"{qty} @ {'limit ' if wait else ''}{levels.entry} stop {levels.stop} target {levels.target}"
        if dry_run:
            return f"dry-run: would {'place pending' if wait else 'buy'} {plan}"

        if pending:
            self.store.close_pending(pending["id"], "replaced")
        if wait:
            self.store.add_pending(
                ticker=d.ticker, created_ts=now_iso(),
                expires_ts=self.expiry_fn(now, self.settings.limit_expiry_days).isoformat(),
                limit_price=levels.entry, stop=levels.stop, target=levels.target,
                horizon_days=d.horizon_days, qty=qty, rating=d.rating, sector=sector,
            )
            return f"pending limit {plan}"
        self._open(d.ticker, qty, levels.stop, levels.target, d.horizon_days, d.rating, sector, now)
        return f"buy {plan}"

    def _refresh_levels(self, trade, d: Decision, dry_run: bool) -> str:
        price = self.broker.quote(trade["ticker"])
        updates = {}
        # Stops only move up; targets only if still above the market.
        if d.stop and trade["stop"] < d.stop < price:
            updates["stop"] = round(d.stop, 2)
        if d.target and d.target > price and d.target != trade["target"]:
            updates["target"] = round(d.target, 2)
        if updates and not dry_run:
            self.store.update_trade(trade["id"], **updates)
        return f"hold{' (updated ' + ', '.join(updates) + ')' if updates else ''}"

    # ---------------------------------------------------------------- execution
    def _open(self, ticker, qty, stop, target, horizon_days, rating, sector, now) -> None:
        fill = self.broker.buy(ticker, qty)
        if stop >= fill:  # gapped through the stop while the order filled
            stop = round(fill * 0.97, 2)
        self.store.open_trade(
            ticker=ticker, sector=sector, rating=rating, qty=qty, entry_price=fill, entry_ts=now_iso(),
            stop=stop, initial_stop=stop, target=target, high_water=fill,
            horizon_until=(now + timedelta(days=horizon_days)).isoformat(),
        )
        logger.info("BUY %s x%d @ %.2f (stop %.2f, target %.2f)", ticker, qty, fill, stop, target)

    def _close(self, trade, reason: str) -> None:
        price = self.broker.sell(trade["ticker"], trade["qty"])
        risk = trade["entry_price"] - trade["initial_stop"]
        pnl = (price - trade["entry_price"]) * trade["qty"]
        self.store.update_trade(
            trade["id"], status="closed", exit_price=price, exit_ts=now_iso(), exit_reason=reason,
            pnl=round(pnl, 2), r_multiple=round((price - trade["entry_price"]) / risk, 3) if risk > 0 else None,
        )
        logger.info("EXIT %s x%d @ %.2f (%s) pnl %.2f", trade["ticker"], trade["qty"], price, reason, pnl)

    # ------------------------------------------------------------------ monitor
    def monitor(self, now: datetime | None = None) -> None:
        """Fill/expire pending entries, trail stops, and enforce stop/target/time exits."""
        if self.halted():
            return
        now = now or datetime.now(timezone.utc)
        with self._trade_lock:
            for p in self.store.open_pending():
                try:
                    self._check_pending(p, now)
                except Exception:
                    logger.exception("Pending check for %s failed", p["ticker"])
            for t in self.store.open_trades():
                try:
                    self._check_trade(t, now)
                except Exception:
                    logger.exception("Position check for %s failed", t["ticker"])
            account = self.broker.account()
            self.store.snapshot(account.equity, account.cash)

    def _check_pending(self, p, now: datetime) -> None:
        if now >= _ts(p["expires_ts"]):
            self.store.close_pending(p["id"], "expired")
            logger.info("Pending entry for %s expired", p["ticker"])
            return
        price = self.broker.quote(p["ticker"])
        if price > p["limit_price"]:
            return
        if price <= p["stop"]:  # fell through the stop before we got in: the setup is invalid
            self.store.close_pending(p["id"], "invalidated")
            return
        if self.broker.account().cash < p["qty"] * price:
            return
        self._open(p["ticker"], p["qty"], p["stop"], p["target"], p["horizon_days"], p["rating"], p["sector"], now)
        self.store.close_pending(p["id"], "filled")

    def _check_trade(self, t, now: datetime) -> None:
        price = self.broker.quote(t["ticker"])
        high = max(t["high_water"] or t["entry_price"], price)
        risk = t["entry_price"] - t["initial_stop"]
        stop = t["stop"]
        if risk > 0:
            gain = high - t["entry_price"]
            if gain >= 2 * risk:
                stop = max(stop, round(high - risk, 2))
            elif gain >= risk:
                stop = max(stop, t["entry_price"])
        if high != t["high_water"] or stop != t["stop"]:
            self.store.update_trade(t["id"], high_water=high, stop=stop)

        if price <= stop:
            self._close(t, "trailing stop" if stop > t["initial_stop"] else "stop-loss")
        elif price >= t["target"]:
            self._close(t, "take-profit")
        elif now >= _ts(t["horizon_until"]):
            self._close(t, "time horizon")

    # --------------------------------------------------------------------- loop
    def run_forever(self) -> None:
        logger.info("Agent started: broker=%s mode=%s universe=%d tickers",
                    self.broker.name, self.settings.trading_mode, len(self.universe))
        last_scan_hour = None
        last_monitor = 0.0
        scan_thread: threading.Thread | None = None
        while True:
            try:
                now = datetime.now(timezone.utc)
                if self.halted():
                    logger.warning("Kill switch present; idle")
                elif market_data.market_open(now):
                    if time.monotonic() - last_monitor >= self.settings.monitor_minutes * 60:
                        self.monitor(now)
                        last_monitor = time.monotonic()
                    hour = now.strftime("%Y%m%d%H")
                    if hour != last_scan_hour and not (scan_thread and scan_thread.is_alive()):
                        last_scan_hour = hour
                        # Scans take many minutes; the monitor keeps running meanwhile.
                        scan_thread = threading.Thread(target=self.scan, daemon=True)
                        scan_thread.start()
            except Exception:
                logger.exception("Agent loop error")
            time.sleep(30)
