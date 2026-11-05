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


def test_claude_runs_on_the_account_not_the_api_key(monkeypatch):
    from trading_journey.runner import claude_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ALPACA_API_KEY", "pk-test")
    env = claude_env()
    assert "ANTHROPIC_API_KEY" not in env and env["ALPACA_API_KEY"] == "pk-test"


def test_state_shows_scan_costs(make_agent, alpaca, settings):
    d = decision("NVDA", "Buy", stop=95, target=115)
    d.usage = {"cost": 1.25, "models": {}}
    agent = make_agent({"NVDA": d})
    plan = run_cycle(agent, alpaca, tickers=["NVDA"])
    state = build_state(agent, settings)
    assert state["summary"]["last_scan_cost"] == 1.25
    assert next(p for p in state["plans"] if p["id"] == plan["plan_id"])["cost"] == 1.25


def test_only_this_pc_and_the_tailnet_get_an_answer():
    from trading_journey.dashboard import client_allowed

    assert client_allowed("127.0.0.1", "off") and client_allowed("::1", "off")
    assert not client_allowed("100.101.102.103", "off")
    assert client_allowed("100.101.102.103", "tailscale")
    assert not client_allowed("192.168.1.20", "tailscale") and not client_allowed("8.8.8.8", "tailscale")


def test_remote_dashboard_shows_the_other_pcs_data_read_only(make_agent, alpaca, settings):
    from trading_journey.dashboard import serve_remote

    agent = _populated(make_agent, alpaca)
    home = serve(agent, settings, port=0)
    threading.Thread(target=home.serve_forever, daemon=True).start()
    friend = serve_remote(f"http://127.0.0.1:{home.server_address[1]}/", port=0)
    threading.Thread(target=friend.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{friend.server_address[1]}"
        assert "<title>Trading Journey</title>" in urllib.request.urlopen(base + "/").read().decode()
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert [p["ticker"] for p in state["positions"]] == ["NVDA"]
        plan_id = state["plans"][0]["id"]
        assert json.loads(urllib.request.urlopen(f"{base}/api/plan?id={plan_id}").read())["intents"]
        try:
            urllib.request.urlopen(base + "/api/plan?id=nope")
            raise AssertionError("missing plan returned 200")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404

        req = urllib.request.Request(base + "/api/kill", data=b'{"on": true}', method="POST",
                                     headers={"Origin": base})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("POST through the remote dashboard accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        assert not settings.kill_switch.exists()
    finally:
        friend.shutdown()
        home.shutdown()
