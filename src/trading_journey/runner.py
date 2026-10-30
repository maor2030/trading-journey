"""The long-running agent process.

Once each trading day, at SCAN_TIME (New York), the scan: Python screens the
universe and runs TradingAgents on the held positions and the best-screened
candidates (API key). Every other market hour, a housekeeping plan (no LLM):
trailing stops, lost stop-losses, time exits, expired limit entries. Whenever a
plan has orders, a headless Claude Code session (your Claude account) runs the
/trade skill on it and places them through the Alpaca MCP.
Between cycles the journal is synced every few minutes. The dashboard can run
in the same process.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

import pandas as pd

from .agent import TradingAgent
from .config import PROJECT_ROOT, Settings
from .store import now_iso

logger = logging.getLogger("trading_journey.runner")

CLAUDE_TIMEOUT_S = 30 * 60
SYNC_EVERY_S = 5 * 60
CLOSED_SYNC_EVERY_S = 60 * 60
TICK_S = 60


def claude_env() -> dict[str, str]:
    """Environment for the headless Claude run, minus the API credentials loaded from .env.

    The API key is for TradingAgents only. Left in place, it would take precedence
    over the Claude account login and bill order execution to the API key.
    """
    env = dict(os.environ)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(key, None)
    return env


class Runner:
    def __init__(self, agent: TradingAgent, settings: Settings, claude_cmd: str | None = None):
        self.agent = agent
        self.store = agent.store
        self.settings = settings
        self.claude_cmd = claude_cmd or shutil.which("claude") or "claude"
        self.runs_dir = settings.data_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def status(self, state: str, **extra) -> None:
        self.store.set_kv("agent", json.dumps({"state": state, "ts": now_iso(), **extra}))

    # ------------------------------------------------------------ the day's scan
    def scan_time(self, now: datetime) -> pd.Timestamp:
        ny = pd.Timestamp(now).tz_convert("America/New_York")
        hour, minute = (int(x) for x in self.settings.scan_time.split(":"))
        return ny.normalize() + pd.Timedelta(hours=hour, minutes=minute)

    def scan_due(self, now: datetime, next_close: str) -> bool:
        """Today's scan hasn't run, it's past SCAN_TIME, and there's time left to act on it."""
        done = self.store.get_kv("scan_date")
        today = pd.Timestamp(now).tz_convert("America/New_York").strftime("%Y-%m-%d")
        if done and done["value"] == today:
            return False
        close = pd.Timestamp(next_close)
        return self.scan_time(now) <= pd.Timestamp(now) <= close - pd.Timedelta(minutes=self.settings.scan_cutoff_min)

    # ---------------------------------------------------------------- one cycle
    def run_cycle(self, now: datetime | None = None, scan: bool = True) -> dict:
        now = now or datetime.now(timezone.utc)
        run_id = self.store.add_run(started=now_iso(), status="scanning" if scan else "planning")
        self.status("scanning" if scan else "planning", run_id=run_id)
        try:
            plan = self.agent.plan(now=now, scan=scan)
        except Exception as exc:
            logger.exception("Plan failed")
            self.store.update_run(run_id, finished=now_iso(), status="plan_error", detail=str(exc)[:2000])
            return {"status": "plan_error"}
        if scan and not plan.get("halted"):
            self.store.set_kv("scan_date", pd.Timestamp(now).tz_convert("America/New_York").strftime("%Y-%m-%d"))

        intents = plan.get("intents", [])
        self.store.update_run(run_id, plan_id=plan["plan_id"], decisions=len(plan.get("decisions", [])),
                              intents=len(intents))
        if plan.get("halted"):
            self.store.update_run(run_id, finished=now_iso(), status="halted", detail=plan["halted"])
            return {"status": "halted"}
        if not intents:
            self.store.update_run(run_id, finished=now_iso(), status="no_orders")
            return {"status": "no_orders"}

        self.status("executing", run_id=run_id, plan_id=plan["plan_id"])
        status, detail, log_path = self.execute_with_claude(plan["path"], plan["plan_id"])
        try:
            self.agent.sync()
        except Exception:
            logger.exception("Sync after execution failed")
        self.store.update_run(run_id, finished=now_iso(), status=status, detail=detail, log_path=log_path)
        return {"status": status}

    def execute_with_claude(self, plan_path: str, plan_id: str) -> tuple[str, str, str]:
        log_path = self.runs_dir / f"{plan_id}.log"
        cmd = [self.claude_cmd, "-p", f"/trade {plan_path}", "--permission-mode", "auto"]
        logger.info("Executing plan %s with Claude", plan_id)
        try:
            proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=CLAUDE_TIMEOUT_S, env=claude_env())
        except subprocess.TimeoutExpired:
            log_path.write_text("Claude run timed out", encoding="utf-8")
            return "claude_timeout", "Claude run exceeded 30 minutes", str(log_path)
        except OSError as exc:
            return "claude_error", f"could not start claude: {exc}", ""
        log_path.write_text(f"$ {' '.join(cmd)}\n\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
                            encoding="utf-8")
        if proc.returncode != 0:
            return "claude_error", (proc.stderr or proc.stdout)[-2000:], str(log_path)
        return "executed", proc.stdout[-2000:], str(log_path)

    def next_scan(self, clock: dict) -> str:
        """When the next scan runs: today's SCAN_TIME if still ahead, else the next open's."""
        now = datetime.now(timezone.utc)
        if clock["is_open"] and self.scan_due(now, clock["next_close"]):
            return now_iso()
        today = self.scan_time(now)
        done = self.store.get_kv("scan_date")
        if clock["is_open"] and pd.Timestamp(now) < today and not (done and done["value"] == today.strftime("%Y-%m-%d")):
            return today.isoformat()
        return self.scan_time(pd.Timestamp(clock["next_open"]).to_pydatetime()).isoformat()

    # --------------------------------------------------------------------- loop
    def loop(self) -> None:
        logger.info("Agent runner started (Claude: %s)", self.claude_cmd)
        last_slot = last_scan_slot = None
        last_sync = 0.0
        while True:
            try:
                if self.agent.halted():
                    self.status("halted", detail="kill switch present")
                else:
                    clock = self.agent.broker.clock()
                    if clock["is_open"]:
                        now = datetime.now(timezone.utc)
                        slot = pd.Timestamp(now).tz_convert("America/New_York").strftime("%Y-%m-%d %H")
                        scan = self.scan_due(now, clock["next_close"])
                        # A failed scan is retried in the next hour, not every minute.
                        if slot != last_slot or (scan and slot != last_scan_slot):
                            last_slot = slot
                            if scan:
                                last_scan_slot = slot
                            self.run_cycle(scan=scan)
                            last_sync = time.monotonic()
                        elif time.monotonic() - last_sync >= SYNC_EVERY_S:
                            self.agent.sync()
                            last_sync = time.monotonic()
                        next_hour = (pd.Timestamp.now(tz="UTC").floor("h") + pd.Timedelta(hours=1)).isoformat()
                        self.status("waiting", detail="market open", next_cycle=next_hour,
                                    next_close=clock["next_close"], next_scan=self.next_scan(clock))
                    else:
                        # After-hours fills/cancels still happen; keep the journal current.
                        if time.monotonic() - last_sync >= CLOSED_SYNC_EVERY_S:
                            self.agent.sync()
                            last_sync = time.monotonic()
                        self.status("market_closed", next_cycle=clock["next_open"], next_scan=self.next_scan(clock))
            except Exception as exc:
                logger.exception("Runner loop error")
                self.status("error", detail=str(exc)[:500])
            time.sleep(TICK_S)
