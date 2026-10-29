"""SQLite journal: decisions, entries (orders), trades, equity snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, ticker TEXT, ts TEXT, trade_date TEXT, rating TEXT,
    entry REAL, stop REAL, target REAL, horizon_days INTEGER, summary TEXT, action TEXT, plan_id TEXT,
    details TEXT
);
-- One row per planned buy. status: planned -> open -> filled | canceled | expired | rejected | not_placed
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY, plan_id TEXT, client_order_id TEXT UNIQUE, order_id TEXT, ticker TEXT,
    sector TEXT, rating TEXT, qty INTEGER, order_type TEXT, limit_price REAL, stop REAL, target REAL,
    horizon_days INTEGER, created_ts TEXT, expires_ts TEXT, status TEXT DEFAULT 'planned'
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, entry_id INTEGER, ticker TEXT, sector TEXT, rating TEXT,
    status TEXT DEFAULT 'open', qty INTEGER, entry_price REAL, entry_ts TEXT,
    order_id TEXT, stop_order_id TEXT, tp_order_id TEXT,
    stop REAL, initial_stop REAL, target REAL, horizon_until TEXT, high_water REAL,
    planned_exit TEXT, exit_price REAL, exit_ts TEXT, exit_reason TEXT, pnl REAL, r_multiple REAL,
    protected INTEGER, stop_client_id TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (ts TEXT, equity REAL, cash REAL);
-- One row per agent cycle: plan, then (if there are orders) a headless Claude run.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, started TEXT, finished TEXT, plan_id TEXT, status TEXT,
    decisions INTEGER, intents INTEGER, detail TEXT, log_path TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, ts TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Shared by the CLI and the dashboard server.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a journal was created."""
        added = {"decisions": {"details": "TEXT"}, "trades": {"protected": "INTEGER", "stop_client_id": "TEXT"}}
        for table, columns in added.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, params).fetchall()

    def _insert(self, table: str, **kw) -> int:
        cols = ", ".join(kw)
        return self.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(kw))})", tuple(kw.values())
        ).lastrowid

    def _update(self, table: str, row_id: int, **kw) -> None:
        sets = ", ".join(f"{k} = ?" for k in kw)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*kw.values(), row_id))

    # --- decisions -------------------------------------------------------
    def record_decision(self, d, action: str, ts: str | None = None, plan_id: str | None = None) -> None:
        self._insert(
            "decisions", ticker=d.ticker, ts=ts or now_iso(), trade_date=d.trade_date, rating=d.rating,
            entry=d.entry, stop=d.stop, target=d.target, horizon_days=d.horizon_days, summary=d.summary,
            action=action, plan_id=plan_id, details=json.dumps(getattr(d, "details", None) or {}),
        )

    def last_analyzed(self) -> dict[str, str]:
        return {r["ticker"]: r["ts"] for r in self.rows("SELECT ticker, MAX(ts) AS ts FROM decisions GROUP BY ticker")}

    def decision_counts(self) -> dict[str, int]:
        return {r["ticker"]: r["n"] for r in self.rows("SELECT ticker, COUNT(*) AS n FROM decisions GROUP BY ticker")}

    def recent_decisions(self, limit: int = 50) -> list[sqlite3.Row]:
        """Without the (large) report text; see decision()."""
        return self.rows(
            "SELECT id, ticker, ts, trade_date, rating, entry, stop, target, horizon_days, summary, action, plan_id,"
            " length(details) > 2 AS has_details FROM decisions ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    def decision(self, decision_id: int) -> sqlite3.Row | None:
        rows = self.rows("SELECT * FROM decisions WHERE id = ?", (decision_id,))
        return rows[0] if rows else None

    # --- entries ---------------------------------------------------------
    def add_entry(self, **kw) -> int:
        return self._insert("entries", **kw)

    def update_entry(self, entry_id: int, **kw) -> None:
        self._update("entries", entry_id, **kw)

    def live_entries(self) -> list[sqlite3.Row]:
        """Entries that may still become positions."""
        return self.rows("SELECT * FROM entries WHERE status IN ('planned', 'open')")

    # --- trades ----------------------------------------------------------
    def open_trades(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM trades WHERE status = 'open'")

    def add_trade(self, **kw) -> int:
        return self._insert("trades", **kw)

    def update_trade(self, trade_id: int, **kw) -> None:
        self._update("trades", trade_id, **kw)

    def closed_trades(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_ts")

    # --- equity ----------------------------------------------------------
    def snapshot(self, equity: float, cash: float, ts: str | None = None) -> None:
        self.execute("INSERT INTO equity_snapshots VALUES (?,?,?)", (ts or now_iso(), equity, cash))

    def first_equity_since(self, since_iso: str) -> float | None:
        row = self.rows("SELECT equity FROM equity_snapshots WHERE ts >= ? ORDER BY ts LIMIT 1", (since_iso,))
        return row[0]["equity"] if row else None

    # --- agent runs / heartbeat -----------------------------------------
    def add_run(self, **kw) -> int:
        return self._insert("runs", **kw)

    def update_run(self, run_id: int, **kw) -> None:
        self._update("runs", run_id, **kw)

    def recent_runs(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    def set_kv(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, ts = excluded.ts",
                     (key, value, now_iso()))

    def get_kv(self, key: str) -> sqlite3.Row | None:
        rows = self.rows("SELECT * FROM kv WHERE key = ?", (key,))
        return rows[0] if rows else None
