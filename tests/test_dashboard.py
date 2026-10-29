import json
import threading
import urllib.request

from conftest import NOW, decision, run_cycle

from trading_journey.dashboard import build_state, serve
from trading_journey.runner import Runner


def _populated(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115),
                        "AMD": decision("AMD", "Overweight", entry=48, stop=45, target=60),
                        "CRM": decision("CRM", "Buy", stop=190, target=240)})
    run_cycle(agent, alpaca, tickers=["NVDA", "AMD", "CRM"])
    alpaca.hit_leg("CRM", "limit")
    agent.sync(NOW)
    return agent


def test_build_state_with_data(make_agent, alpaca, settings):
    agent = _populated(make_agent, alpaca)
    state = build_state(agent, settings)
    json.dumps(state, default=str)  # serializable
    assert [p["ticker"] for p in state["positions"]] == ["NVDA"]
    assert [e["ticker"] for e in state["entries"]] == ["AMD"]
    assert state["closed_trades"][0]["exit_reason"] == "take-profit"
    assert state["summary"]["closed"] == 1 and state["summary"]["win_rate"] == 1
    assert state["sectors"]["Semiconductors"] > 0
    assert len(state["leaderboard"]) == 3 and state["plans"]


def test_runner_cycle_records_run_and_calls_claude(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    runner = Runner(agent, settings, claude_cmd="claude")
    executed = []

    def fake_claude(path, plan_id):
        plan = json.loads(open(path, encoding="utf-8").read())
        for intent in plan["intents"]:
            alpaca.execute(intent)
        executed.append(plan_id)
        return "executed", "ok", ""

    runner.execute_with_claude = fake_claude
    agent.plan = (lambda orig: lambda **kw: orig(tickers=["NVDA"], **kw))(agent.plan)
    assert runner.run_cycle(NOW)["status"] == "executed"
    [run] = agent.store.recent_runs()
    assert run["status"] == "executed" and run["intents"] == 1 and executed
    assert agent.store.open_trades()


def test_http_server_endpoints(make_agent, alpaca, settings):
    agent = _populated(make_agent, alpaca)
    server = serve(agent, settings, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "<title>Trading Journey</title>" in html
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        plan_id = state["plans"][0]["id"]
        plan = json.loads(urllib.request.urlopen(f"{base}/api/plan?id={plan_id}").read())
        assert plan["intents"]

        # State-changing calls need a same-origin request.
        req = urllib.request.Request(base + "/api/kill", data=b'{"on": true}', method="POST")
        try:
            urllib.request.urlopen(req)
            raise AssertionError("cross-origin POST accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        req.add_header("Origin", base)
        assert json.loads(urllib.request.urlopen(req).read())["kill_switch"] is True
        assert settings.kill_switch.exists()
    finally:
        server.shutdown()


def test_decision_details_endpoint_and_next_picks(make_agent, alpaca, settings):
    d = decision("NVDA", "Buy", stop=95, target=115)
    d.details = {"Portfolio manager": "**Rating**: Buy"}
    agent = make_agent({"NVDA": d})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    state = build_state(agent, settings)
    row = state["decisions"][0]
    assert row["has_details"] and "details" not in row
    # The held position (last analyzed on the test date, >20h ago) is due for review first.
    assert [(p["ticker"], p["held"]) for p in state["next_picks"]][0] == ("NVDA", True)
    assert {p["ticker"] for p in state["next_picks"]} == {"NVDA", "AMD", "CRM"}

    server = serve(agent, settings, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/decision?id={row['id']}"
        data = json.loads(urllib.request.urlopen(url).read())
        assert data["details"] == {"Portfolio manager": "**Rating**: Buy"}
    finally:
        server.shutdown()
