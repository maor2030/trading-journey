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
    from .benchmark import Benchmark
    from .measure import Measure
    from .store import Store
    from .universe import load_universe

    store, broker, universe = Store(SETTINGS.db_path), AlpacaClient(SETTINGS), load_universe()
    benchmark = Benchmark(store, SETTINGS, universe, equity_fn=lambda: broker.account().equity)         if SETTINGS.benchmark else None
    return TradingAgent(store, broker, SETTINGS, universe=universe, benchmark=benchmark,
                        measure=Measure(store, SETTINGS))


def _measure(agent) -> None:
    def f(v, fmt):
        return "-" if v is None else format(v, fmt)

    s = agent.measure.summary()
    a = s["agents"]
    print(f"Cost per agent (last {a['days']} days, {a['analyses']} analyses, ${a['total_cost']:.2f}):")
    for r in a["rows"]:
        print(f"  {r['agent']:<22}{f(r['share'], '.0%'):>5}  ${r['cost_per_analysis']:.4f}/analysis  "
              f"{r['calls'] / max(a['analyses'], 1):4.1f} calls  {f(r['tokens_per_call'], ',.0f'):>7} tok/call  "
              f"{r['seconds_per_analysis']:5.1f}s  tools {r['tool_calls']}  wrap-ups {r['wrap_ups']}")
    print()
    print("Decision quality (direction-signed: + means the call was right):")
    for r in s["quality"]["by_rating"]:
        cells = []
        for n in (5, 10, 20):
            if f"ret_{n}" in r:  # a directional call
                cells.append(f"{n}d {f(r[f'ret_{n}'], '+.2%')} alpha {f(r[f'alpha_{n}'], '+.2%')} "
                             f"hit {f(r[f'hit_{n}'], '.0%')} (n={r[f'settled_{n}']})")
            else:  # Hold: how much it moved anyway
                cells.append(f"{n}d move {f(r[f'abs_move_{n}'], '.2%')} (n={r[f'settled_{n}']})")
        cells = "  ".join(cells)
        print(f"  {r['rating']:<12}{r['n']:>4}  {cells}")
    fh = s["quality"]["first_hit"]
    print(f"  Agents' own levels, first touched: target {fh['target']}, stop {fh['stop']}, neither {fh['none']}")
    for g in s["quality"]["traded_split"]:
        print(f"  {g['group']:<11} n={g['n']:<4} 10d {f(g['ret_10'], '+.2%')} alpha {f(g['alpha_10'], '+.2%')} "
              f"hit {f(g['hit_10'], '.0%')} (settled {g['settled_10']})")
    lv = s["levels"]
    print()
    print(f"Levels: {lv['sized']} sized entries, {f(lv['any_replaced'], '.0%')} had a level replaced")
    for r in lv["reasons"]:
        print(f"  {r['reason']:<20}{r['n']:>4}  {r['share']:.0%}")
    print("  Missing from the agents' output: " + ", ".join(
        f"{k} {f(v, '.0%')}" for k, v in lv["missing"].items()) + f" of {lv['directional']} directional calls")
    print("  Actions: " + ", ".join(f"{r['action']} x{r['n']}" for r in lv["actions"]))
    e = s["execution"]
    print()
    print(f"Execution: {e['fills']} fills; entry slippage {f(e['entry_slippage_pct'], '+.3f')}% "
          f"({e['market_fills']} market); stop slippage {f(e['stop_slippage_pct'], '+.3f')}% ({e['stop_exits']} exits); "
          f"median hours to fill: market {f(e['hours_to_fill']['market'], '.2f')}, "
          f"limit {f(e['hours_to_fill']['limit'], '.1f')}")
    print(f"  Entries: {e['entries']}  Runs: {e['runs']}")
    c = s["cost"]
    print()
    print(f"Cost: ${c['total']:.2f} over {c['analyses']} analyses (${f(c['per_analysis'], '.3f')} each); "
          f"${f(c['per_trade'], '.2f')} per trade, ${f(c['per_win'], '.2f')} per win; "
          f"realized P&L per API $: {f(c['pnl_per_dollar'], '+,.1f')}")


def _pct(v, signed: bool = True) -> str:
    return "-" if v is None else (f"{v:+.2f}%" if signed else f"{v:.0%}")


def _benchmark(agent) -> None:
    if agent.benchmark is None:
        print("The benchmark is off (BENCHMARK=false).")
        return
    s = agent.benchmark.summary()
    if not s["start"]:
        print("The benchmark starts at the next morning scan.")
        return
    print(f"Since {s['start']['ts']} (start {s['start']['capital']:,.2f}):\n")
    print(f"  {'':<16}{'return':>9}{'closed':>8}{'open':>6}{'W/L':>8}{'win':>6}{'avg R':>8}{'P&L':>12}{'alpha':>9}")
    for name, key in (("Agent", "agent"), ("Screener only", "bench")):
        r = s[key]
        print(f"  {name:<16}{_pct(r['return_pct']):>9}{r['closed']:>8}{r['open']:>6}{str(r['wins']) + '/' + str(r['losses']):>8}"
              f"{_pct(r['win_rate'], False):>6}{'-' if r['avg_r'] is None else format(r['avg_r'], '+.2f'):>8}"
              f"{r['pnl']:>+12,.2f}{_pct(r['avg_alpha_pct']):>9}")
    print(f"  {'SPY':<16}{_pct(s['spy']['return_pct']):>9}")


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
        d = -1 if t["side"] == "short" else 1
        print(f"  {t['ticker']:<6} {t['side'] or 'long':<5} x{t['qty']:<5} entry {t['entry_price']:.2f} now {price:.2f} "
              f"stop {t['stop']:.2f} target {t['target']:.2f} pnl {d * (price - t['entry_price']) * t['qty']:+,.2f}")
    print("Pending entries:")
    for e in agent.store.live_entries():
        print(f"  {e['ticker']:<6} {e['side'] or 'long':<5} x{e['qty']:<5} {e['order_type']} {e['limit_price']:.2f} "
              f"stop {e['stop']:.2f} target {e['target']:.2f} [{e['status']}]")

    now = datetime.now(timezone.utc)
    screen = agent.todays_screen(now)
    if screen:
        print("\nToday's screen (top 10):")
        for r in screen[:10]:
            print(f"  {r['ticker']:<6} {r['score']:5.2f} {r.get('bias', 'long'):<5}  {'; '.join(r['reasons'])}")
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
    plan.add_argument("-n", type=int, default=None, help="How many new candidates to analyze (default DAILY_SCANS)")
    plan.add_argument("--ticker", nargs="*", help="Analyze these tickers instead of the ranked list")
    plan.add_argument("--no-scan", action="store_true", help="Housekeeping only: no screen, no analyses")
    plan.add_argument("--dry-run", action="store_true", help="Don't journal the decisions or planned entries")
    plan.add_argument("--force", action="store_true", help="Plan even when the market is closed")
    sub.add_parser("sync", help="Reconcile the journal with Alpaca orders and positions")
    sub.add_parser("clock", help="Print Alpaca's market clock as JSON")
    sub.add_parser("status", help="Account, positions, pending entries, today's screen")
    screen_p = sub.add_parser("screen", help="Run the morning screener now and print the ranking")
    screen_p.add_argument("--top", type=int, default=20)
    sub.add_parser("report", help="Write a markdown performance report to reports/")
    sub.add_parser("benchmark", help="Agent vs. the screener-only benchmark vs. SPY")
    sub.add_parser("measure", help="Cost per agent, decision quality, level overrides, execution")
    agent_p = sub.add_parser("agent", help="Run the autonomous agent (daily scan, hourly housekeeping) with the dashboard")
    agent_p.add_argument("--port", type=int, default=8050)
    agent_p.add_argument("--no-dashboard", action="store_true")
    sub.add_parser("test-email", help="Send a test alert email (checks the SMTP settings)")
    dash = sub.add_parser("dashboard", help="Serve only the localhost dashboard")
    dash.add_argument("--port", type=int, default=8050)
    dash.add_argument("--remote", metavar="URL",
                      help="Show another PC's dashboard data (e.g. http://my-pc:8050 over Tailscale) with this "
                           "checkout's page, for working on web/index.html. Needs no .env.")
    args = parser.parse_args()

    _setup_logging(console=args.command not in ("agent", "dashboard"))
    if args.command == "dashboard" and args.remote:
        from .dashboard import serve_remote

        print(f"Dashboard: http://127.0.0.1:{args.port} (data from {args.remote})")
        serve_remote(args.remote, args.port).serve_forever()
        return
    agent = _agent()
    if args.command == "plan":
        clock = agent.broker.clock()
        if not clock["is_open"] and not args.force:
            print(json.dumps({"market_open": False, "next_open": clock["next_open"], "intents": []}))
            return
        result = agent.plan(n=args.n, tickers=args.ticker, dry_run=args.dry_run, scan=not args.no_scan)
        print(json.dumps(result, indent=2))
    elif args.command == "sync":
        print(json.dumps(agent.sync(), indent=2))
    elif args.command == "clock":
        print(json.dumps(agent.broker.clock()))
    elif args.command == "status":
        _status(agent)
    elif args.command == "screen":
        for r in agent.screen_fn(list(agent.universe), datetime.now(timezone.utc), SETTINGS.allow_shorts)[:args.top]:
            print(f"  {r.ticker:<6} {r.score:5.2f} {r.bias:<5}  {'; '.join(r.reasons)}")
    elif args.command == "report":
        _report(agent)
    elif args.command == "benchmark":
        _benchmark(agent)
    elif args.command == "measure":
        _measure(agent)
    elif args.command == "test-email":
        from .notify import send_email

        ok = send_email(SETTINGS, "Test alert", "Email alerts from the trading agent work.")
        print("Sent to " + SETTINGS.alert_email if ok else "Not sent; see the log above")
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
