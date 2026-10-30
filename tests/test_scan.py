import dataclasses
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from conftest import NOW, decision, run_cycle

from trading_journey.runner import Runner
from trading_journey.scoring import conviction
from trading_journey.screener import ScreenRow, score_bars


def _bars(closes, today_open=None, today_volume=None, today=date(2026, 10, 27)):
    """Daily bars ending yesterday, plus today's partial bar when today_open is given."""
    days = pd.bdate_range(end=pd.Timestamp(today) - pd.Timedelta(days=1), periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes],
                       "Close": closes, "Volume": [1_000_000] * len(closes)}, index=days)
    if today_open is not None:
        df.loc[pd.Timestamp(today)] = [today_open, today_open * 1.01, today_open * 0.99, today_open, today_volume]
    return df


def test_screener_prefers_uptrend_in_play_over_quiet_downtrend():
    up = list(np.linspace(80, 100, 80))
    down = list(np.linspace(120, 100, 80))
    hot = score_bars("HOT", _bars(up, today_open=104, today_volume=600_000), date(2026, 10, 27), minutes_open=30)
    cold = score_bars("COLD", _bars(down, today_open=100, today_volume=50_000), date(2026, 10, 27), minutes_open=30)
    assert hot.score > cold.score
    assert hot.gap_pct == 4.0 and hot.rvol > 2
    assert any("gap" in r for r in hot.reasons) and any("RVOL" in r for r in hot.reasons)


def test_screener_penalizes_earnings_tomorrow():
    bars = _bars(list(np.linspace(80, 100, 80)))
    plain = score_bars("X", bars, date(2026, 10, 27), None)
    risky = score_bars("X", bars, date(2026, 10, 27), None, earnings=date(2026, 10, 28))
    assert risky.score < plain.score and "avoid" in risky.reasons[-1]


def test_scan_picks_follow_the_screen_and_cap_by_free_slots(make_agent, alpaca, settings):
    analyzed = []
    agent = make_agent()
    agent.analyze_fn = lambda t, d, portfolio=None: analyzed.append(t) or decision(t, "Hold")
    agent.screen_fn = lambda universe, now: [ScreenRow("CRM", 0.9), ScreenRow("AMD", 0.5), ScreenRow("NVDA", 0.1)]
    agent.settings = dataclasses.replace(settings, max_positions=1, scan_workers=1)
    plan = agent.plan(now=NOW)
    assert analyzed == ["CRM", "AMD", "NVDA"]  # 1 free slot x 3 candidates, best screened first
    assert [r["ticker"] for r in plan["screen"]] == ["CRM", "AMD", "NVDA"]
    assert [r["ticker"] for r in agent.todays_screen(NOW)] == ["CRM", "AMD", "NVDA"]


def test_strongest_call_gets_the_last_slot(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Overweight", stop=95, target=115),
                        "AMD": decision("AMD", "Buy", stop=47.5, target=60)})
    agent.settings = dataclasses.replace(settings, max_positions=1)
    plan = agent.plan(now=NOW, tickers=["NVDA", "AMD"])
    assert [i["ticker"] for i in plan["intents"]] == ["AMD"]
    assert {d["ticker"]: d["action"] for d in plan["decisions"]}["NVDA"] == "skip: no free position slot"


def test_conviction_order():
    assert conviction("Buy", None, None, None) > conviction("Overweight", 100, 95, 130)
    assert conviction("Buy", 100, 95, 120) > conviction("Buy", 100, 95, 108)


def test_housekeeping_plan_runs_no_analyses(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=130)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    agent.analyze_fn = lambda *a, **k: (_ for _ in ()).throw(AssertionError("analyzed"))
    alpaca.prices["NVDA"] = 112
    plan = agent.plan(now=NOW + timedelta(days=2), scan=False)
    assert [i["kind"] for i in plan["intents"]] == ["raise_stop"] and plan["decisions"] == []


def test_scan_runs_once_a_day_after_scan_time(make_agent, settings):
    agent = make_agent()
    runner = Runner(agent, settings)
    close = "2026-10-27T16:00:00-04:00"
    ny = lambda h, m: pd.Timestamp(f"2026-10-27 {h:02d}:{m:02d}", tz="America/New_York").to_pydatetime()
    assert not runner.scan_due(ny(9, 35), close)  # before 09:45
    assert runner.scan_due(ny(9, 45), close)
    assert runner.scan_due(ny(13, 0), close)  # PC woke late: still scans
    assert not runner.scan_due(ny(14, 45), close)  # too close to the close

    calls = []
    agent.plan = lambda now, scan: calls.append(scan) or {"plan_id": "p", "intents": []}
    runner.run_cycle(ny(9, 45), scan=True)
    assert calls == [True] and not runner.scan_due(ny(11, 0), close)
    assert runner.scan_due(datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc), "2026-10-28T16:00:00-04:00")
