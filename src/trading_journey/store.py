"""SQLite journal: decisions, pending entries, trades, equity snapshots."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, ticker TEXT, ts TEXT, trade_date TEXT, rating TEXT,
    entry REAL, stop REAL, target REAL, horizon_days INTEGER, summary TEXT, action TEXT
);
CREATE TABLE IF NOT EXISTS pending_entries (
    id INTEGER PRIMARY KEY, ticker TEXT, created_ts TEXT, expires_ts TEXT, limit_price REAL,
    stop REAL, target REAL, horizon_days INTEGER, qty INTEGER, rating TEXT, sector TEXT,
    status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, ticker TEXT, sector TEXT, rating TEXT, status TEXT DEFAULT 'open',
    qty INTEGER, entry_price REAL, entry_ts TEXT, stop REAL, initial_stop REAL, target REAL,
    horizon_until TEXT, high_water REAL, exit_price REAL, exit_ts TEXT, exit_reason TEXT,
    pnl REAL, r_multiple REAL
);
CREATE TABLE IF NOT EXISTS equity_snapshots (ts TEXT, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS paper_account (id INTEGER PRIMARY KEY CHECK (id = 1), cash REAL);
CREATE TABLE IF NOT EXISTS paper_positions (ticker TEXT PRIMARY KEY, qty INTEGER, avg_price REAL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Shared by the scan thread and the monitor loop.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, params).fetchall()

    # --- decisions -------------------------------------------------------
    def record_decision(self, d, action: str, ts: str | None = None) -> None:
        self.execute(
            "INSERT INTO decisions (ticker, ts, trade_date, rating, entry, stop, target, horizon_days, summary, action)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d.ticker, ts or now_iso(), d.trade_date, d.rating, d.entry, d.stop, d.target, d.horizon_days, d.summary, action),
        )

    def last_analyzed(self) -> dict[str, str]:
        return {r["ticker"]: r["ts"] for r in self.rows("SELECT ticker, MAX(ts) AS ts FROM decisions GROUP BY ticker")}

    def decision_counts(self) -> dict[str, int]:
        return {r["ticker"]: r["n"] for r in self.rows("SELECT ticker, COUNT(*) AS n FROM decisions GROUP BY ticker")}

    # --- pending entries -------------------------------------------------
    def open_pending(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM pending_entries WHERE status = 'open'")

    def add_pending(self, **kw) -> None:
        cols = ", ".join(kw)
        self.execute(f"INSERT INTO pending_entries ({cols}) VALUES ({', '.join('?' * len(kw))})", tuple(kw.values()))

    def close_pending(self, pending_id: int, status: str) -> None:
        self.execute("UPDATE pending_entries SET status = ? WHERE id = ?", (status, pending_id))

    # --- trades ----------------------------------------------------------
    def open_trades(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM trades WHERE status = 'open'")

    def open_trade(self, **kw) -> None:
        cols = ", ".join(kw)
        self.execute(f"INSERT INTO trades ({cols}) VALUES ({', '.join('?' * len(kw))})", tuple(kw.values()))

    def update_trade(self, trade_id: int, **kw) -> None:
        sets = ", ".join(f"{k} = ?" for k in kw)
        self.execute(f"UPDATE trades SET {sets} WHERE id = ?", (*kw.values(), trade_id))

    def closed_trades(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_ts")

    # --- equity ----------------------------------------------------------
    def snapshot(self, equity: float, cash: float) -> None:
        self.execute("INSERT INTO equity_snapshots VALUES (?,?,?)", (now_iso(), equity, cash))

    def first_equity_since(self, since_iso: str) -> float | None:
        row = self.rows("SELECT equity FROM equity_snapshots WHERE ts >= ? ORDER BY ts LIMIT 1", (since_iso,))
        return row[0]["equity"] if row else None
