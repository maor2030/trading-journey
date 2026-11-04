import json
from datetime import timedelta

import pandas as pd
import pytest
from conftest import NOW, decision, run_cycle

from trading_journey.dashboard import build_state
from trading_journey.sizing import finalize_levels

SESSIONS = pd.bdate_range("2026-10-27", periods=30)  # NOW's date is the first session


def after(k):
    """A moment after the k-th session following the trade date has closed."""
    return pd.Timestamp(SESSIONS[k + 1]).tz_localize("UTC").to_pydatetime() + timedelta(hours=1)


def rising(start, step=1.0):
    """Daily bars that rise by `step` each session: open = previous close, high/low +-0.5."""
    closes = [start + step * i for i in range(len(SESSIONS))]
    opens = [start] + closes[:-1]
    return pd.DataFrame({"Open": opens, "High": [c + 0.5 for c in closes], "Low": [o - 0.5 for o in opens],
                         "Close": closes}, index=SESSIONS)


@pytest.fixture
def market(bars):
    bars["NVDA"] = rising(100)
    bars["SPY"] = rising(500, 0.5)
    return bars


def _record(agent, ticker, rating, stop=None, target=None, price=100.0, action="no action", usage=None):
    d = decision(ticker, rating, stop=stop, target=target)
    d.usage = usage or {}
    agent.store.record_decision(d, action, ts="2026-10-27T14:00:00+00:00", price=price)
    return agent.store.rows("SELECT MAX(id) AS id FROM decisions")[0]["id"]


def _outcome(agent, decision_id):
    rows = agent.store.rows("SELECT * FROM decision_outcomes WHERE decision_id = ?", (decision_id,))
    return dict(rows[0]) if rows else None


# ------------------------------------------------------------- level overrides
@pytest.mark.parametrize("side, args, reasons", [
    ("long", (100, None, 95, 115), ()),  # every level used as is
    ("long", (100, 105, 95, 115), ("entry_wrong_side",)),
    ("long", (100, 85, 95, 115), ("entry_far",)),
    ("long", (100, None, None, 115), ("stop_missing",)),
    ("long", (100, None, 99.5, None), ("stop_out_of_range", "target_missing")),
    ("long", (100, None, 95, 105), ("target_low_rr",)),
    ("short", (100, 95, 105, 85), ("entry_wrong_side",)),
    ("short", (100, None, 120, 85), ("stop_out_of_range",)),
    ("short", (100, None, 105, 96), ("target_low_rr",)),
])
def test_finalize_levels_says_what_it_replaced(side, args, reasons):
    assert finalize_levels(*args, atr=3.0, side=side).replaced == reasons


def test_plan_records_price_and_final_levels(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", target=115), "AMD": decision("AMD", "Hold")})
    run_cycle(agent, alpaca, tickers=["NVDA", "AMD"])
    rows = {r["ticker"]: dict(r) for r in agent.store.rows("SELECT * FROM decisions")}
    nvda, amd = rows["NVDA"], rows["AMD"]
    assert (nvda["price"], nvda["final_entry"], nvda["final_stop"], nvda["final_target"]) == (100, 100, 94, 115)
    assert json.loads(nvda["levels_replaced"]) == ["stop_missing"]
    assert (amd["price"], amd["final_stop"], amd["levels_replaced"]) == (50, None, None)


def test_dry_run_plan_shows_usage(make_agent, alpaca):
    d = decision("NVDA", "Hold")
    d.usage = {"cost": 0.08, "agents": {"Portfolio Manager": {"cost": 0.05}}}
    plan = make_agent({"NVDA": d}).plan(now=NOW, tickers=["NVDA"], dry_run=True)
    assert plan["decisions"][0]["usage"]["agents"]["Portfolio Manager"]["cost"] == 0.05


# ----------------------------------------------------------- decision quality
def test_forward_returns_fill_in_as_sessions_pass(make_agent, market):
    agent = make_agent()
    did = _record(agent, "NVDA", "Buy", stop=90, target=150)
    m = agent.measure

    assert m.settle_outcomes(after(6)) == 1
    o = _outcome(agent, did)
    assert o["ret_5"] == pytest.approx(105 / 100 - 1)
    assert o["spy_5"] == pytest.approx(502.5 / 500 - 1)  # from SPY's open on the trade date
    assert (o["ret_10"], o["first_hit"], o["settled_ts"]) == (None, None, None)
    assert m.settle_outcomes(after(6)) == 0  # once per day

    m.settle_outcomes(after(21))
    o = _outcome(agent, did)
    assert o["ret_20"] == pytest.approx(0.20) and o["first_hit"] == "none" and o["settled_ts"]
    assert m.settle_outcomes(after(22), force=True) == 0  # settled for good


def test_todays_partial_bar_does_not_count(make_agent, market):
    agent = make_agent()
    did = _record(agent, "NVDA", "Buy")
    # During the 6th session after the trade date only 5 sessions are complete.
    agent.measure.settle_outcomes(pd.Timestamp(SESSIONS[6]).tz_localize("UTC").to_pydatetime() + timedelta(hours=16))
    assert _outcome(agent, did)["ret_5"] == pytest.approx(0.05)


def test_reference_price_falls_back_to_the_open(make_agent, market):
    agent = make_agent()
    did = _record(agent, "NVDA", "Buy", price=None)
    market["NVDA"].loc[SESSIONS[0], "Open"] = 98
    agent.measure.settle_outcomes(after(6))
    assert _outcome(agent, did)["ref_price"] == 98


@pytest.mark.parametrize("rating, stop, target, hit, days", [
    ("Buy", 95, 103, "target", 3),  # session 3 closes at 103, its high reaches 103.5
    ("Sell", 104, 90, "stop", 4),  # a short in a rising stock: the stop above is touched
])
def test_first_hit_of_the_agents_own_levels(make_agent, market, rating, stop, target, hit, days):
    agent = make_agent()
    did = _record(agent, "NVDA", rating, stop=stop, target=target)
    agent.measure.settle_outcomes(after(21))
    o = _outcome(agent, did)
    assert (o["first_hit"], o["first_hit_days"]) == (hit, days)


def test_a_bar_touching_both_levels_counts_as_the_stop(make_agent, market):
    market["NVDA"].loc[SESSIONS[1], ["High", "Low"]] = [151, 94]
    agent = make_agent()
    did = _record(agent, "NVDA", "Buy", stop=95, target=150)
    agent.measure.settle_outcomes(after(21))
    o = _outcome(agent, did)
    assert (o["first_hit"], o["first_hit_days"]) == ("stop", 1)


def test_levels_on_the_wrong_side_are_not_tested(make_agent, market):
    agent = make_agent()
    did = _record(agent, "NVDA", "Buy", stop=105, target=150)  # a long stop above the price
    agent.measure.settle_outcomes(after(21))
    o = _outcome(agent, did)
    assert o["first_hit"] is None and o["settled_ts"]


def test_quality_summary_signs_returns_by_the_call(make_agent, market):
    agent = make_agent()
    _record(agent, "NVDA", "Buy", action="buy 150 @ market stop 94 target 112")
    _record(agent, "NVDA", "Sell")
    _record(agent, "NVDA", "Hold")
    agent.measure.settle_outcomes(after(11))
    q = agent.measure.summary(after(11))["quality"]
    rows = {r["rating"]: r for r in q["by_rating"]}
    assert rows["Buy"]["ret_10"] == pytest.approx(0.10) and rows["Buy"]["hit_10"] == 1
    assert rows["Buy"]["alpha_10"] == pytest.approx(0.10 - 5 / 500)
    assert rows["Sell"]["ret_10"] == pytest.approx(-0.10) and rows["Sell"]["hit_10"] == 0
    assert rows["Hold"]["abs_move_10"] == pytest.approx(0.10) and "ret_10" not in rows["Hold"]
    assert rows["Buy"]["settled_20"] == 0
    split = {g["group"]: g for g in q["traded_split"]}
    assert (split["traded"]["n"], split["not traded"]["n"]) == (1, 1)
    assert split["traded"]["hit_10"] == 1 and split["not traded"]["hit_10"] == 0


# --------------------------------------------------- the other three sections
def test_cost_per_agent(make_agent):
    agent = make_agent()
    usage = {"cost": 0.10, "agents": {
        "Market Analyst": {"calls": 4, "input": 8000, "output": 800, "cache_read": 2000, "cost": 0.02,
                           "seconds": 30.0, "tool_calls": 3, "wrap_ups": 0},
        "Portfolio Manager": {"calls": 1, "input": 6000, "output": 1000, "cache_read": 0, "cost": 0.08,
                              "seconds": 20.0, "tool_calls": 0, "wrap_ups": 0}}}
    _record(agent, "NVDA", "Buy", usage=usage)
    _record(agent, "AMD", "Hold", usage=usage)
    _record(agent, "CRM", "Hold")  # recorded before the per-agent split: not counted
    a = agent.measure.summary(NOW + timedelta(days=1))["agents"]
    assert a["analyses"] == 2 and a["total_cost"] == pytest.approx(0.20)
    pm, market = a["rows"]
    assert pm["agent"] == "Portfolio Manager" and pm["share"] == pytest.approx(0.8)
    assert market["tokens_per_call"] == 2700 and market["seconds_per_analysis"] == 30
    assert agent.measure.summary(NOW + timedelta(days=40))["agents"]["analyses"] == 0  # outside the window


def test_levels_and_actions(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", target=115),
                        "AMD": decision("AMD", "Buy", stop=47, target=60), "CRM": decision("CRM", "Hold")})
    run_cycle(agent, alpaca, tickers=["NVDA", "AMD", "CRM"])
    lv = agent.measure.summary(NOW)["levels"]
    assert lv["sized"] == 2 and lv["any_replaced"] == 0.5
    assert lv["reasons"] == [{"reason": "stop_missing", "n": 1, "share": 0.5}]
    assert lv["missing"]["stop"] == 0.5 and lv["missing"]["entry"] == 1
    assert {r["action"]: r["n"] for r in lv["actions"]} == {"buy": 2, "no action": 1}


def test_execution_and_cost_per_result(make_agent, alpaca):
    d = decision("NVDA", "Buy", stop=95, target=115)
    d.usage = {"cost": 0.50}
    agent = make_agent({"NVDA": d})
    plan = agent.plan(now=NOW, tickers=["NVDA"])
    alpaca.prices["NVDA"] = 101  # the market moved before Claude placed the order
    for intent in plan["intents"]:
        alpaca.execute(intent)
    agent.sync(NOW)
    alpaca.hit_leg("NVDA", "limit")
    agent.sync(NOW)
    s = agent.measure.summary(NOW)
    e, c = s["execution"], s["cost"]
    assert e["fills"] == 1 and e["entry_slippage_pct"] == pytest.approx(1.0)
    assert e["stop_exits"] == 0 and e["entries"] == {"filled": 1}
    assert (c["total"], c["trades"], c["wins"], c["per_trade"], c["per_win"]) == (0.5, 1, 1, 0.5, 0.5)
    assert c["pnl_per_dollar"] == pytest.approx(c["realized_pnl"] / 0.5)


def test_dashboard_state_includes_measurement(make_agent, settings):
    agent = make_agent()
    state = build_state(agent, settings)
    assert set(state["measure"]) == {"agents", "quality", "levels", "execution", "cost"}
    json.dumps(state, default=str)


def test_cli_prints_measurement_and_benchmark(make_agent, alpaca, market, capsys):
    from trading_journey import cli

    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    agent.benchmark.open_from_screen([{"ticker": "AMD", "score": 0.9, "bias": "long"}], NOW)
    agent.measure.settle_outcomes(after(11))
    cli._measure(agent)
    cli._benchmark(agent)
    out = capsys.readouterr().out
    assert "Cost per agent" in out and "Decision quality" in out and "Screener only" in out
