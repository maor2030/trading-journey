"""Localhost dashboard: one JSON state endpoint plus a static single-page UI."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .agent import TradingAgent, _ts
from .config import Settings
from .scoring import priority, ticker_stats

logger = logging.getLogger("trading_journey.dashboard")

WEB_DIR = Path(__file__).parent / "web"
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(size - 200_000, 0))
        text = f.read().decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def _group(trades: list[dict], key) -> list[dict]:
    """Win rate / avg R / P&L of closed trades grouped by key(trade)."""
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(key(t) or "unknown", []).append(t)
    out = []
    for name, rows in groups.items():
        rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
        out.append({"name": name, "trades": len(rows),
                    "win_rate": sum(1 for r in rows if (r["pnl"] or 0) > 0) / len(rows),
                    "avg_r": sum(rs) / len(rs) if rs else None, "pnl": sum(r["pnl"] or 0 for r in rows)})
    return sorted(out, key=lambda g: g["trades"], reverse=True)


def build_state(agent: TradingAgent, settings: Settings) -> dict:
    now = datetime.now(timezone.utc)
    errors = []
    account = clock = None
    live = {}
    try:
        account = agent.broker.account()
        clock = agent.broker.clock()
        live = {p.ticker: p for p in agent.broker.positions()}
    except Exception as exc:  # the page still renders the journal
        errors.append(f"Alpaca: {exc}")

    positions = []
    for t in _rows(agent.store.open_trades()):
        p = live.get(t["ticker"])
        price = p.price if p else t["entry_price"]
        d = -1 if t.get("side") == "short" else 1
        risk = abs(t["entry_price"] - t["initial_stop"])
        span = (t["target"] - t["stop"]) or 1  # negative for a short, like price - stop
        positions.append({
            **t, "side": t.get("side") or "long", "price": price, "market_value": price * t["qty"],
            "pnl": d * (price - t["entry_price"]) * t["qty"],
            "pnl_pct": d * (price / t["entry_price"] - 1) * 100,
            "r_now": d * (price - t["entry_price"]) / risk if risk > 0 else None,
            "progress": min(max((price - t["stop"]) / span, 0), 1),
            "days_held": (now - _ts(t["entry_ts"])).total_seconds() / 86400,
            "days_left": (_ts(t["horizon_until"]) - now).total_seconds() / 86400,
            "at_broker": p is not None,
        })
    untracked = [asdict(p) for tk, p in live.items() if tk not in {x["ticker"] for x in positions}]

    stats = ticker_stats(agent.store, list(agent.universe), agent.memory_log_path())
    last = agent.store.last_analyzed()
    leaderboard = sorted(
        ({"ticker": s.ticker, "sector": agent.universe.get(s.ticker, "Other"), "priority": priority(s, now),
          "decisions": s.decisions, "trades": s.trades, "win_rate": s.win_rate, "avg_r": s.avg_r,
          "pnl": s.total_pnl, "alpha_hit_rate": s.alpha_hit_rate, "score_mult": s.score_mult,
          "demoted": s.demoted, "last_analyzed": last.get(s.ticker)} for s in stats.values()),
        key=lambda r: r["priority"], reverse=True,
    )

    open_trades = {t["ticker"]: t for t in agent.store.open_trades()}
    live_entries = {e["ticker"]: e for e in agent.store.live_entries()}
    free_slots = settings.max_positions - len(open_trades) - len(live_entries)
    screen = agent.todays_screen(now)
    screen_scores = {r["ticker"]: r["score"] for r in screen}
    next_picks = [{"ticker": t, "held": t in open_trades, "score": screen_scores.get(t)}
                  for t in agent.next_picks(settings.daily_scans, open_trades, live_entries, free_slots, stats, now,
                                            screen_scores)]

    sectors: dict[str, float] = {}
    for p in positions:
        sectors[p["sector"]] = sectors.get(p["sector"], 0) + p["market_value"]
    for e in agent.store.live_entries():
        sectors[e["sector"]] = sectors.get(e["sector"], 0) + e["qty"] * (e["limit_price"] or 0)

    equity = _rows(agent.store.rows("SELECT ts, equity, cash FROM equity_snapshots ORDER BY ts"))
    if len(equity) > 600:
        step = len(equity) // 600 + 1
        equity = equity[::step] + [equity[-1]]

    closed = _rows(agent.store.closed_trades())
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]

    ratings = {r["rating"]: r["n"] for r in agent.store.rows(
        "SELECT rating, COUNT(*) AS n FROM decisions GROUP BY rating")}
    analytics = {
        "ratings": ratings,
        "by_rating": _group(closed, lambda t: t["rating"]),
        "by_exit": _group(closed, lambda t: t["exit_reason"]),
        "by_sector": _group(closed, lambda t: t["sector"]),
        "avg_hold_days": (sum((_ts(t["exit_ts"]) - _ts(t["entry_ts"])).total_seconds() for t in closed)
                          / len(closed) / 86400) if closed else None,
        "best": max(closed, key=lambda t: t["pnl"] or 0) if closed else None,
        "worst": min(closed, key=lambda t: t["pnl"] or 0) if closed else None,
    }

    plans = []
    if settings.plans_dir.exists():
        for path in sorted(settings.plans_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            plans.append({"id": path.stem, "created": data.get("created"), "dry_run": data.get("dry_run"),
                          "halted": data.get("halted"), "intents": len(data.get("intents", [])),
                          "decisions": len(data.get("decisions", []))})

    agent_kv = agent.store.get_kv("agent")
    return {
        "now": now.isoformat(timespec="seconds"),
        "errors": errors,
        "paper": getattr(agent.broker, "paper", True),
        "account": asdict(account) if account else None,
        "clock": clock,
        "kill_switch": agent.halted(),
        "agent": json.loads(agent_kv["value"]) if agent_kv else None,
        "positions": positions,
        "untracked_positions": untracked,
        "entries": _rows(agent.store.live_entries()),
        "decisions": _rows(agent.store.recent_decisions(150)),
        "closed_trades": list(reversed(closed))[:200],
        "summary": {
            "closed": len(closed), "wins": len(wins),
            "win_rate": len(wins) / len(closed) if closed else None,
            "avg_r": sum(rs) / len(rs) if rs else None,
            "realized_pnl": sum(t["pnl"] or 0 for t in closed),
            "unrealized_pnl": sum(p["pnl"] for p in positions),
            "analyses": sum(r["decisions"] for r in leaderboard),
            "api_cost_today": agent.store.api_cost(datetime.now(timezone.utc).strftime("%Y-%m-%d"))[0],
            "api_cost_7d": agent.store.api_cost((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())[0],
        },
        "equity": equity,
        "sectors": sectors,
        "leaderboard": leaderboard,
        "next_picks": next_picks,
        "screen": screen,
        "analytics": analytics,
        "free_slots": free_slots,
        "runs": _rows(agent.store.recent_runs(30)),
        "plans": plans,
        "log": _tail(settings.data_dir / "agent.log", 200),
        "settings": {
            "scan_time": settings.scan_time, "daily_scans": settings.daily_scans,
            "scan_workers": settings.scan_workers, "max_positions": settings.max_positions,
            "risk_per_trade": settings.risk_per_trade, "max_position_pct": settings.max_position_pct,
            "max_sector_pct": settings.max_sector_pct, "cash_reserve_pct": settings.cash_reserve_pct,
            "daily_loss_limit": settings.daily_loss_limit, "limit_expiry_days": settings.limit_expiry_days,
            "deep_model": settings.deep_model, "quick_model": settings.quick_model,
            "effort": settings.effort, "analysts": list(settings.analysts),
            "universe_size": len(agent.universe),
        },
    }


def make_handler(agent: TradingAgent, settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/state":
                    self._json(build_state(agent, settings))
                elif url.path == "/api/plan":
                    plan_id = query.get("id", [""])[0]
                    path = settings.plans_dir / f"{plan_id}.json"
                    if not SAFE_ID.match(plan_id) or not path.exists():
                        return self._json({"error": "not found"}, 404)
                    self._send(200, path.read_bytes(), "application/json")
                elif url.path == "/api/decision":
                    row = agent.store.decision(int(query.get("id", ["0"])[0] or 0))
                    if not row:
                        return self._json({"error": "not found"}, 404)
                    data = dict(row)
                    data["details"] = json.loads(data.get("details") or "{}")
                    self._json(data)
                elif url.path == "/api/run-log":
                    run = agent.store.rows("SELECT log_path FROM runs WHERE id = ?", (query.get("id", ["0"])[0],))
                    log_path = Path(run[0]["log_path"]) if run and run[0]["log_path"] else None
                    if not log_path or not log_path.exists() or log_path.parent != settings.data_dir / "runs":
                        return self._json({"error": "no log"}, 404)
                    self._send(200, log_path.read_bytes(), "text/plain; charset=utf-8")
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:
                logger.exception("Dashboard GET failed")
                self._json({"error": str(exc)}, 500)

        def do_POST(self):  # noqa: N802
            # Only the page itself may change state (blocks cross-site requests).
            origin = self.headers.get("Origin", "")
            if not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
                return self._json({"error": "forbidden"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                if self.path == "/api/kill":
                    if body.get("on"):
                        settings.kill_switch.parent.mkdir(parents=True, exist_ok=True)
                        settings.kill_switch.write_text("stopped from dashboard", encoding="utf-8")
                    elif settings.kill_switch.exists():
                        settings.kill_switch.unlink()
                    self._json({"kill_switch": settings.kill_switch.exists()})
                elif self.path == "/api/sync":
                    self._json(agent.sync())
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:
                logger.exception("Dashboard POST failed")
                self._json({"error": str(exc)}, 500)

    return Handler


def serve(agent: TradingAgent, settings: Settings, port: int = 8050) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(agent, settings))
    logger.info("Dashboard on http://127.0.0.1:%d", port)
    return server
