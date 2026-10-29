"""Command line entry point (used by the /trade skill and by hand)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from .config import SETTINGS


def _setup_logging(console: bool = True) -> None:
    SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
    # Windows consoles/redirects default to a legacy codepage that garbles Hebrew paths.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    handlers: list[logging.Handler] = [RotatingFileHandler(
        SETTINGS.data_dir / "agent.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    else:  # the long-running agent: only warnings reach its stderr redirect
        warn = logging.StreamHandler(sys.stderr)
        warn.setLevel(logging.WARNING)
        handlers.append(warn)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    for noisy in ("httpx", "httpx2", "yfinance", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _agent():
    from .agent import TradingAgent
    from .alpaca import AlpacaClient
    from .store import Store

    return TradingAgent(Store(SETTINGS.db_path), AlpacaClient(SETTINGS), SETTINGS)


def _status(agent) -> None:
    from .scoring import priority, ticker_stats

    clock = agent.broker.clock()
    account = agent.broker.account()
    mode = "PAPER" if agent.broker.paper else "LIVE"
    print(f"Alpaca {mode}  equity {account.equity:,.2f}  cash {account.cash:,.2f}  "
          f"market {'OPEN' if clock['is_open'] else 'closed (next open ' + clock['next_open'] + ')'}")
    print(f"Kill switch: {'ON' if agent.halted() else 'off'} ({SETTINGS.kill_switch})\n")

    print("Open positions:")
    prices = {p.ticker: p.price for p in agent.broker.positions()}
    for t in agent.store.open_trades():
        price = prices.get(t["ticker"], t["entry_price"])
        print(f"  {t['ticker']:<6} x{t['qty']:<5} entry {t['entry_price']:.2f} now {price:.2f} "
              f"stop {t['stop']:.2f} target {t['target']:.2f} pnl {(price - t['entry_price']) * t['qty']:+,.2f}")
    print("Pending entries:")
    for e in agent.store.live_entries():
        print(f"  {e['ticker']:<6} x{e['qty']:<5} {e['order_type']} {e['limit_price']:.2f} "
              f"stop {e['stop']:.2f} target {e['target']:.2f} [{e['status']}]")

    now = datetime.now(timezone.utc)
    stats = ticker_stats(agent.store, list(agent.universe), agent.memory_log_path())
    ranked = sorted(stats.values(), key=lambda s: priority(s, now), reverse=True)
    print("\nScan priority (top 10):")
    for s in ranked[:10]:
        wr = f"{s.win_rate:.0%}" if s.win_rate is not None else "-"
        ar = f"{s.avg_r:+.2f}R" if s.avg_r is not None else "-"
        print(f"  {s.ticker:<6} prio {priority(s, now):.2f}  trades {s.trades}  win {wr}  avg {ar}  pnl {s.total_pnl:+,.2f}")


def _report(agent) -> None:
    from .scoring import ticker_stats

    SETTINGS.reports_dir.mkdir(parents=True, exist_ok=True)
    stats = sorted(ticker_stats(agent.store, list(agent.universe), agent.memory_log_path()).values(),
                   key=lambda s: s.total_pnl, reverse=True)
    lines = ["# Trading journey performance", "", f"Generated {datetime.now():%Y-%m-%d %H:%M}", "",
             "## Per-ticker", "", "| Ticker | Decisions | Trades | Win rate | Avg R | P&L | TA alpha hit |",
             "|---|---|---|---|---|---|---|"]
    for s in stats:
        lines.append(f"| {s.ticker} | {s.decisions} | {s.trades} | "
                     f"{f'{s.win_rate:.0%}' if s.win_rate is not None else '-'} | "
                     f"{f'{s.avg_r:+.2f}' if s.avg_r is not None else '-'} | {s.total_pnl:+,.2f} | "
                     f"{f'{s.alpha_hit_rate:.0%}' if s.alpha_hit_rate is not None else '-'} |")
    lines += ["", "## Closed trades", "", "| Ticker | Qty | Entry | Exit | Reason | P&L | R |", "|---|---|---|---|---|---|---|"]
    for t in agent.store.closed_trades():
        lines.append(f"| {t['ticker']} | {t['qty']} | {t['entry_price']:.2f} | {t['exit_price']:.2f} | "
                     f"{t['exit_reason']} | {t['pnl']:+,.2f} | {t['r_multiple'] if t['r_multiple'] is not None else '-'} |")
    path = SETTINGS.reports_dir / f"performance_{datetime.now():%Y%m%d}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report written to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="trading-journey")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Sync, analyze, and write the order instructions for Claude (no trading)")
    plan.add_argument("-n", type=int, default=None, help="How many tickers to analyze (default SCANS_PER_HOUR)")
    plan.add_argument("--ticker", nargs="*", help="Analyze these tickers instead of the ranked list")
    plan.add_argument("--dry-run", action="store_true", help="Don't journal the decisions or planned entries")
    plan.add_argument("--force", action="store_true", help="Plan even when the market is closed")
    sub.add_parser("sync", help="Reconcile the journal with Alpaca orders and positions")
    sub.add_parser("clock", help="Print Alpaca's market clock as JSON")
    sub.add_parser("status", help="Account, positions, pending entries, scan priority")
    sub.add_parser("report", help="Write a markdown performance report to reports/")
    agent_p = sub.add_parser("agent", help="Run the autonomous agent (hourly cycles) with the dashboard")
    agent_p.add_argument("--port", type=int, default=8050)
    agent_p.add_argument("--no-dashboard", action="store_true")
    dash = sub.add_parser("dashboard", help="Serve only the localhost dashboard")
    dash.add_argument("--port", type=int, default=8050)
    args = parser.parse_args()

    _setup_logging(console=args.command not in ("agent", "dashboard"))
    agent = _agent()
    if args.command == "plan":
        clock = agent.broker.clock()
        if not clock["is_open"] and not args.force:
            print(json.dumps({"market_open": False, "next_open": clock["next_open"], "intents": []}))
            return
        result = agent.plan(n=args.n, tickers=args.ticker, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
    elif args.command == "sync":
        print(json.dumps(agent.sync(), indent=2))
    elif args.command == "clock":
        print(json.dumps(agent.broker.clock()))
    elif args.command == "status":
        _status(agent)
    elif args.command == "report":
        _report(agent)
    elif args.command == "dashboard":
        from .dashboard import serve

        print(f"Dashboard: http://127.0.0.1:{args.port}")
        serve(agent, SETTINGS, args.port).serve_forever()
    elif args.command == "agent":
        import threading

        from .dashboard import serve
        from .runner import Runner

        if not args.no_dashboard:
            server = serve(agent, SETTINGS, args.port)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print(f"Dashboard: http://127.0.0.1:{args.port}")
        Runner(agent, SETTINGS).loop()


if __name__ == "__main__":
    main()
