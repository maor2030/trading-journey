import json
from datetime import timedelta

import pytest

from trading_journey import runner as runner_mod
from trading_journey.agent import FAILED_INTENTS_KEY
from trading_journey.runner import Runner, claude_env

from conftest import NOW, decision, run_cycle


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(runner_mod, "RETRY_DELAY_S", 0)


def fake_claude(alpaca, outcomes):
    """Stands in for `claude -p /trade`: each run takes the next outcome ("ok", "no_mcp", "lie")."""
    runs = []

    def run(path, plan_id):
        plan = json.loads(open(path, encoding="utf-8").read())
        outcome = outcomes.pop(0)
        runs.append({"plan_id": plan_id, "intents": [i["id"] for i in plan["intents"]]})
        if outcome == "no_mcp":
            return "executed", "MCP timed out", "", {"mcp_connected": False, "summary": "MCP timed out", "intents": [
                {"id": i["id"], "status": "failed", "detail": "Alpaca MCP not connected"} for i in plan["intents"]]}
        if outcome == "ok":
            for intent in plan["intents"]:
                alpaca.execute(intent)
        # "lie": reports success without placing anything
        return "executed", "done", "", {"mcp_connected": True, "summary": "done", "intents": [
            {"id": i["id"], "status": "placed", "detail": ""} for i in plan["intents"]]}

    return run, runs


def _runner(agent, settings, alpaca, outcomes, tickers=("NVDA",)):
    runner = Runner(agent, settings, claude_cmd="claude")
    runner.execute_with_claude, runs = fake_claude(alpaca, outcomes)
    agent.plan = (lambda orig: lambda **kw: orig(tickers=list(tickers), **kw) if kw.get("scan", True) else orig(**kw))(agent.plan)
    return runner, runs


def test_orders_placed_first_time(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    runner, runs = _runner(agent, settings, alpaca, ["ok"])
    assert runner.run_cycle(NOW)["status"] == "executed"
    assert len(runs) == 1 and agent.store.open_trades()
    assert json.loads(agent.store.get_kv(FAILED_INTENTS_KEY)["value"]) == []


def test_mcp_timeout_is_retried_with_only_the_failed_orders(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    runner, runs = _runner(agent, settings, alpaca, ["no_mcp", "ok"])
    assert runner.run_cycle(NOW)["status"] == "executed"
    assert runs[1]["plan_id"].endswith("-retry") and runs[1]["intents"] == runs[0]["intents"]
    assert agent.store.open_trades()


def test_a_run_that_places_nothing_is_not_recorded_as_executed(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    runner, runs = _runner(agent, settings, alpaca, ["no_mcp", "no_mcp"])
    assert runner.run_cycle(NOW)["status"] == "mcp_unavailable"
    [run] = agent.store.recent_runs()
    assert run["status"] == "mcp_unavailable" and "buy NVDA" in run["detail"]


def test_a_buy_reported_as_placed_is_checked_at_alpaca(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    runner, runs = _runner(agent, settings, alpaca, ["lie", "lie"])
    assert runner.run_cycle(NOW)["status"] == "failed" and len(runs) == 2


def test_a_failed_order_run_saves_what_to_retry(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    alpaca.prices["NVDA"] = 112  # +2.4R: the trailing stop moves up
    runner, runs = _runner(agent, settings, alpaca, ["no_mcp", "no_mcp"])
    assert runner.run_cycle(NOW + timedelta(days=1), scan=False)["status"] == "mcp_unavailable"
    [failed] = json.loads(agent.store.get_kv(FAILED_INTENTS_KEY)["value"])
    assert failed["kind"] == "raise_stop" and failed["error"] == "Alpaca MCP not connected"


def test_a_failed_stop_tightening_is_carried_into_the_next_plan(make_agent, alpaca, settings):
    """The analysts' tighter stop: housekeeping wouldn't recreate it, so the next plan re-issues it."""
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    alpaca.prices["NVDA"] = 103  # under +1R: no trailing move of its own
    agent.store.set_kv(FAILED_INTENTS_KEY, json.dumps([
        {"kind": "raise_stop", "ticker": "NVDA", "id": "old-NVDA-stop", "reason": "agents tightened the stop",
         "calls": [{"tool": "replace_order_by_id", "args": {"order_id": "an-old-id", "stop_price": "99.00"}}]}]))
    plan = agent.plan(now=NOW + timedelta(hours=1), scan=False)
    [carried] = plan["intents"]
    assert carried["kind"] == "raise_stop" and carried["reason"].startswith("retry")
    args = carried["calls"][0]["args"]
    assert args["stop_price"] == "99.00" and args["order_id"] == agent.store.open_trades()[0]["stop_order_id"]


def test_carry_over_drops_what_no_longer_applies(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    stale = [
        {"kind": "raise_stop", "ticker": "NVDA", "id": "x-stop", "reason": "",
         "calls": [{"tool": "replace_order_by_id", "args": {"order_id": "gone", "stop_price": "90.00"}}]},  # looser
        {"kind": "buy", "ticker": "AMD", "id": "x-buy", "reason": "", "calls": []},  # entries aren't carried
        {"kind": "exit", "ticker": "CRM", "id": "x-exit", "reason": "", "calls": []},  # no CRM position
    ]
    agent.store.set_kv(FAILED_INTENTS_KEY, json.dumps(stale))
    assert agent.plan(now=NOW + timedelta(hours=1), scan=False)["intents"] == []


def test_claude_env_gives_mcp_servers_time_to_start(monkeypatch):
    monkeypatch.delenv("MCP_TIMEOUT", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    env = claude_env()
    assert env["MCP_TIMEOUT"] == runner_mod.MCP_TIMEOUT_MS and "ANTHROPIC_API_KEY" not in env
