"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from .config import SETTINGS


def _setup_logging() -> None:
    SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(SETTINGS.data_dir / "agent.log", encoding="utf-8")],
    )


def _agent():
    from .agent import TradingAgent
    from .brokers import make_broker
    from .store import Store

    store = Store(SETTINGS.db_path)
    return TradingAgent(store, make_broker(store, SETTINGS), SETTINGS)


def _status(agent) -> None:
    from .scoring import priority, ticker_stats

    account = agent.broker.account()
    print(f"Broker: {agent.broker.name} ({SETTINGS.trading_mode})  equity {account.equity:,.2f}  cash {account.cash:,.2f}")
    print(f"Kill switch: {'ON' if agent.halted() else 'off'} ({SETTINGS.kill_switch})\n")

    print("Open positions:")
    for t in agent.store.open_trades():
        price = agent.broker.quote(t["ticker"])
        pnl = (price - t["entry_price"]) * t["qty"]
        print(f"  {t['ticker']:<6} x{t['qty']:<5} entry {t['entry_price']:.2f} now {price:.2f} "
              f"stop {t['stop']:.2f} target {t['target']:.2f} pnl {pnl:+,.2f}")
    print("Pending entries:")
    for p in agent.store.open_pending():
        print(f"  {p['ticker']:<6} x{p['qty']:<5} limit {p['limit_price']:.2f} expires {p['expires_ts'][:16]}")

    now = datetime.now(timezone.utc)
    stats = ticker_stats(agent.store, list(agent.universe), agent._memory_log_path())
    ranked = sorted(stats.values(), key=lambda s: priority(s, now), reverse=True)
    print("\nScan priority (top 15):")
    for s in ranked[:15]:
        wr = f"{s.win_rate:.0%}" if s.win_rate is not None else "-"
        ar = f"{s.avg_r:+.2f}R" if s.avg_r is not None else "-"
        print(f"  {s.ticker:<6} prio {priority(s, now):.2f}  trades {s.trades}  win {wr}  avg {ar}  pnl {s.total_pnl:+,.2f}")


def _report(agent) -> None:
    from .scoring import ticker_stats

    SETTINGS.reports_dir.mkdir(parents=True, exist_ok=True)
    stats = sorted(ticker_stats(agent.store, list(agent.universe), agent._memory_log_path()).values(),
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
    lines += ["", "## Daily equity (last snapshot per day)", "", "| Day | Equity | Cash |", "|---|---|---|"]
    for r in agent.store.rows("SELECT substr(ts,1,10) AS day, equity, cash, MAX(ts) FROM equity_snapshots GROUP BY day"):
        lines.append(f"| {r['day']} | {r['equity']:,.2f} | {r['cash']:,.2f} |")
    path = SETTINGS.reports_dir / f"performance_{datetime.now():%Y%m%d}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report written to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="trading-journey")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Start the autonomous agent (hourly scans + risk monitor, US market hours)")
    scan = sub.add_parser("scan", help="Run one scan now")
    scan.add_argument("-n", type=int, default=None, help="How many tickers (default SCANS_PER_HOUR)")
    scan.add_argument("--ticker", nargs="*", help="Analyze these tickers instead of the ranked list")
    scan.add_argument("--dry-run", action="store_true", help="Analyze and print the plan; place no orders")
    sub.add_parser("monitor", help="Run one risk-monitor pass now")
    sub.add_parser("status", help="Account, positions, pending entries, scan priority")
    sub.add_parser("report", help="Write a markdown performance report to reports/")
    sub.add_parser("tools", help="Log in to TradingView MCP and list its tools")
    args = parser.parse_args()

    if args.command == "tools":
        from .tv_mcp import list_tools

        for tool in asyncio.run(list_tools()):
            print(f"- {tool['name']}: {tool['description'].splitlines()[0] if tool['description'] else ''}")
        return

    _setup_logging()
    agent = _agent()
    if args.command == "run":
        agent.run_forever()
    elif args.command == "scan":
        for ticker, action in agent.scan(n=args.n, tickers=args.ticker, dry_run=args.dry_run):
            print(f"{ticker}: {action}")
    elif args.command == "monitor":
        agent.monitor()
    elif args.command == "status":
        _status(agent)
    elif args.command == "report":
        _report(agent)


if __name__ == "__main__":
    main()
