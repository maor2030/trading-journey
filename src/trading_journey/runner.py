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

from .agent import FAILED_INTENTS_KEY, TradingAgent
from .config import PROJECT_ROOT, Settings
from .store import now_iso

logger = logging.getLogger("trading_journey.runner")

CLAUDE_TIMEOUT_S = 30 * 60
RETRY_DELAY_S = 60  # before running the orders that failed once more
# `uvx alpaca-mcp-server` can take longer than Claude Code's default 30s to start.
MCP_TIMEOUT_MS = "120000"
ENTRY_KINDS = ("buy", "short")
# What the headless /trade run reports, so the runner knows what to retry (see the skill's step 4).
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "mcp_connected": {"type": "boolean"},
        "intents": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "status": {"type": "string", "enum": ["placed", "skipped", "failed"]},
                           "detail": {"type": "string"}},
            "required": ["id", "status", "detail"],
        }},
        "summary": {"type": "string"},
    },
    "required": ["mcp_connected", "intents", "summary"],
}
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
    env.setdefault("MCP_TIMEOUT", MCP_TIMEOUT_MS)
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
        if scan and not plan.get("halted") and not plan.get("out_of_credit"):
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
        status, detail, log_path = self.execute(plan, now)
        self.store.update_run(run_id, finished=now_iso(), status=status, detail=detail, log_path=log_path)
        return {"status": status}

    # ------------------------------------------------------------ order runs
    def execute(self, plan: dict, now: datetime | None = None) -> tuple[str, str, str]:
        """Run the plan's orders through headless Claude, check what got done, retry the rest once.

        Status: executed (everything done), partial, failed, mcp_unavailable (Claude couldn't
        reach Alpaca), or claude_error / claude_timeout. What still failed is saved, and the
        next plan re-issues the changes to open positions that still make sense.
        """
        intents = plan["intents"]
        status, detail, log_path, report = self.execute_with_claude(plan["path"], plan["plan_id"])
        self._sync(now)
        failed = self.failed_intents(intents, report)
        if failed and status != "claude_timeout":
            logger.warning("%d of %d orders not done (%s); retrying in %ds", len(failed), len(intents), status,
                           RETRY_DELAY_S)
            time.sleep(RETRY_DELAY_S)
            retry_path = os.path.splitext(plan["path"])[0] + "-retry.json"
            with open(retry_path, "w", encoding="utf-8") as f:
                json.dump({**plan, "intents": failed, "retry_of": plan["plan_id"]}, f, indent=2)
            status, detail, log_path, report = self.execute_with_claude(retry_path, plan["plan_id"] + "-retry")
            self._sync(now)
            failed = self.failed_intents(failed, report)

        self.store.set_kv(FAILED_INTENTS_KEY, json.dumps(failed))
        summary = (report or {}).get("summary") or detail
        if not failed:
            return "executed", summary[-2000:], log_path
        lines = "\n".join(f"- {i['kind']} {i['ticker']} ({i['id']}): {i.get('error', 'not done')}" for i in failed)
        if status in ("claude_error", "claude_timeout"):
            final = status
        elif report and not report.get("mcp_connected", True):
            final = "mcp_unavailable"
        else:
            final = "failed" if len(failed) == len(intents) else "partial"
        logger.error("Order run %s: %d of %d not done", final, len(failed), len(intents))
        return final, f"Not done after a retry:\n{lines}\n\n{summary}"[-2000:], log_path

    def failed_intents(self, intents: list[dict], report: dict | None) -> list[dict]:
        """The intents not done: per Claude's report, and for entries per Alpaca itself."""
        results = {r["id"]: r for r in (report or {}).get("intents", [])}
        connected = bool(report) and report.get("mcp_connected", True)
        failed = []
        for intent in intents:
            r = results.get(intent["id"])
            if intent["kind"] in ENTRY_KINDS:  # Alpaca has the last word on whether an entry exists
                if self._entry_at_alpaca(intent) or (connected and r and r["status"] == "skipped"):
                    continue
            elif connected and r and r["status"] in ("placed", "skipped"):
                continue
            error = (r or {}).get("detail") or ("Alpaca MCP not connected" if report and not connected
                                                else "no result reported")
            if intent["kind"] in ENTRY_KINDS and r and r["status"] == "placed":
                error = "reported placed, but Alpaca has no such order"
            failed.append({**intent, "error": error})
        return failed

    def _entry_at_alpaca(self, intent: dict) -> bool:
        cid = next((c["args"].get("client_order_id") for c in intent["calls"] if c["tool"] == "place_stock_order"), None)
        try:
            return bool(cid) and self.agent.broker.order_by_client_id(cid) is not None
        except Exception:
            return False

    def _sync(self, now: datetime | None) -> None:
        try:
            self.agent.sync(now)
        except Exception:
            logger.exception("Sync after execution failed")

    def execute_with_claude(self, plan_path: str, plan_id: str) -> tuple[str, str, str, dict | None]:
        """(status, detail, log path, Claude's structured report or None)."""
        log_path = self.runs_dir / f"{plan_id}.log"
        cmd = [self.claude_cmd, "-p", f"/trade {plan_path}", "--permission-mode", "auto",
               "--output-format", "json", "--json-schema", json.dumps(RESULT_SCHEMA)]
        logger.info("Executing plan %s with Claude", plan_id)
        try:
            proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=CLAUDE_TIMEOUT_S, env=claude_env())
        except subprocess.TimeoutExpired:
            log_path.write_text("Claude run timed out", encoding="utf-8")
            return "claude_timeout", "Claude run exceeded 30 minutes", str(log_path), None
        except OSError as exc:
            return "claude_error", f"could not start claude: {exc}", "", None
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            out = {}
        report = out.get("structured_output")
        text = (report or {}).get("summary") or out.get("result") or proc.stdout
        log_path.write_text(f"$ {' '.join(cmd[:5])} ...\n\n--- summary ---\n{text}\n\n--- report ---\n"
                            f"{json.dumps(report, indent=2)}\n\n--- stderr ---\n{proc.stderr}", encoding="utf-8")
        if proc.returncode != 0 or out.get("is_error"):
            return "claude_error", (proc.stderr or text)[-2000:], str(log_path), report
        return "executed", text[-2000:], str(log_path), report

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
