import dataclasses
from datetime import timedelta

import pandas as pd
import pytest
from conftest import NOW, decision, run_cycle

from trading_journey.benchmark import trade_alpha
from trading_journey.dashboard import build_state
from trading_journey.screener import ScreenRow

DAY = timedelta(days=1)


def daily(**days):
    """daily(d1028=(open, high, low, close), ...) -> bars indexed by date."""
    idx = pd.to_datetime([f"2026-{k[1:3]}-{k[3:]}" for k in days])
    return pd.DataFrame(list(days.values()), index=idx, columns=["Open", "High", "Low", "Close"])


def row(ticker, score, bias="long"):
    return {"ticker": ticker, "score": score, "bias": bias}


@pytest.fixture
def bench(make_agent):
    return make_agent().benchmark


def test_opens_the_best_screened_setups_with_the_agents_rules(bench):
    opened = bench.open_from_screen([row("CRM", 0.5), row("NVDA", 0.9), row("AMD", 0.8, "short")], NOW)
    assert opened == ["NVDA", "AMD"]  # CRM is below MIN_SCORE
    trades = {t["ticker"]: t for t in bench.store.open_bench_trades()}
    nvda, amd = trades["NVDA"], trades["AMD"]
    # 2x ATR stop, 2R target; 1% risk capped at 15% of equity.
    assert (nvda["side"], nvda["entry_price"], nvda["stop"], nvda["target"], nvda["qty"]) == ("long", 100, 94, 112, 150)
    assert (amd["side"], amd["entry_price"], amd["stop"], amd["target"], amd["qty"]) == ("short", 50, 56, 38, 166)
    assert nvda["spy_entry"] == 500
    assert bench.start()["capital"] == 100_000


def test_respects_slots_shorts_setting_and_held_names(bench, settings):
    bench.settings = dataclasses.replace(settings, max_positions=1)
    assert bench.open_from_screen([row("NVDA", 0.9), row("AMD", 0.8)], NOW) == ["NVDA"]
    bench.settings = dataclasses.replace(settings, allow_shorts=False)
    assert bench.open_from_screen([row("NVDA", 0.9), row("AMD", 0.8, "short")], NOW) == []  # NVDA already held


def _closed(bench):
    [t] = bench.store.closed_bench_trades()
    return t


@pytest.mark.parametrize("bar, price, reason, r", [
    ((99, 101, 93, 95), 94, "stop-loss", -1),
    ((90, 91, 88, 89), 90, "stop-loss", -10 / 6),  # gap through the stop fills at the open
    ((105, 113, 104, 110), 112, "take-profit", 2),
    ((115, 116, 114, 115), 115, "take-profit", 2.5),  # gap past the target
    ((100, 113, 93, 100), 94, "stop-loss", -1),  # touched both: assume the stop
])
def test_long_exits_from_daily_bars(bench, bars, bar, price, reason, r):
    bench.open_from_screen([row("NVDA", 0.9)], NOW)
    bars["NVDA"] = daily(d1027=(100, 120, 80, 100), d1028=bar)  # the entry day's bar never counts
    assert bench.update(NOW + 2 * DAY) == ["NVDA"]
    t = _closed(bench)
    assert (t["exit_price"], t["exit_reason"], t["r_multiple"]) == (price, reason, round(r, 3))
    assert t["exit_ts"] == "2026-10-28T20:00:00+00:00"  # that session's close


def test_short_stop_and_target(bench, bars):
    bench.open_from_screen([row("AMD", 0.9, "short")], NOW)
    bars["AMD"] = daily(d1028=(51, 57, 50, 55))
    bench.update(NOW + 2 * DAY)
    t = _closed(bench)
    assert (t["exit_price"], t["exit_reason"], t["pnl"]) == (56, "stop-loss", -6 * 166)


def test_trailing_stop_from_completed_bars(bench, bars):
    bench.open_from_screen([row("NVDA", 0.9)], NOW)
    bars["NVDA"] = daily(d1028=(101, 107, 99, 106))  # +1.17R: breakeven
    bench.update(NOW + 2 * DAY)
    [t] = bench.store.open_bench_trades()
    assert (t["stop"], t["high_water"], t["checked_through"]) == (100, 107, "2026-10-28")
    bars["NVDA"] = daily(d1028=(101, 107, 99, 106), d1029=(101, 102, 99.5, 100))
    bench.update(NOW + 3 * DAY)
    t = _closed(bench)
    assert (t["exit_price"], t["exit_reason"], t["r_multiple"]) == (100, "trailing stop", 0)


def test_todays_bar_is_checked_but_not_trailed(bench, bars):
    bench.open_from_screen([row("NVDA", 0.9)], NOW)
    bars["NVDA"] = daily(d1028=(101, 107, 99, 106))
    assert bench.update(NOW + DAY) == []  # 10-28 is still trading
    [t] = bench.store.open_bench_trades()
    assert (t["stop"], t["checked_through"]) == (94, None)
    bars["NVDA"] = daily(d1028=(101, 107, 93, 106))
    assert bench.update(NOW + DAY) == ["NVDA"]
    assert _closed(bench)["exit_ts"] == "2026-10-28T15:00:00+00:00"


def test_time_horizon_closes_at_the_quote(bench, alpaca):
    bench.open_from_screen([row("NVDA", 0.9)], NOW)
    alpaca.prices["NVDA"] = 103
    bench.update(NOW + 31 * DAY)
    t = _closed(bench)
    assert (t["exit_price"], t["exit_reason"]) == (103, "time horizon")


def test_summary_compares_agent_benchmark_and_spy(bench, bars, alpaca):
    bench.open_from_screen([row("NVDA", 0.9)], NOW)
    bars["NVDA"] = daily(d1028=(105, 113, 104, 110))
    bars["SPY"] = daily(d1028=(500, 512, 499, 510))
    alpaca.prices["SPY"] = 505
    bench.update(NOW + 2 * DAY)
    t = _closed(bench)
    assert t["spy_exit"] == 510  # SPY's close on the exit day
    assert trade_alpha(dict(t)) == pytest.approx(12 - 2)

    s = bench.summary()
    assert s["bench"]["closed"] == 1 and s["bench"]["wins"] == 1 and s["bench"]["avg_r"] == 2
    assert s["bench"]["return_pct"] == pytest.approx(150 * 12 / 100_000 * 100)
    assert s["spy"]["return_pct"] == pytest.approx(1.0)
    assert s["agent"]["return_pct"] == 0 and s["agent"]["closed"] == 0
    assert [p["bench"] for p in s["curve"]] == [0, pytest.approx(1.8)]


def test_summary_before_the_first_scan(bench):
    assert bench.summary() == {"start": None}
    assert bench.update(NOW) == []


# ---------------------------------------------------------------- wiring
def _screen(*rows):
    return lambda universe, now, shorts=True: [ScreenRow(t, s, bias=b) for t, s, b in rows]


def test_morning_scan_opens_benchmark_trades(make_agent):
    agent = make_agent({})
    agent.screen_fn = _screen(("NVDA", 0.9, "long"), ("CRM", 0.7, "long"))
    agent.next_picks = lambda *a, **k: []
    agent.plan(now=NOW)
    assert sorted(t["ticker"] for t in agent.store.open_bench_trades()) == ["CRM", "NVDA"]


def test_dry_run_leaves_the_benchmark_alone(make_agent):
    agent = make_agent({})
    agent.screen_fn = _screen(("NVDA", 0.9, "long"))
    agent.next_picks = lambda *a, **k: []
    agent.plan(now=NOW, dry_run=True)
    assert agent.store.open_bench_trades() == [] and agent.benchmark.start() is None


def test_a_failing_benchmark_never_blocks_trading(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})

    def boom(*args):
        raise RuntimeError("yfinance down")

    agent.benchmark.open_from_screen = agent.benchmark.update = boom
    plan = run_cycle(agent, alpaca, tickers=["NVDA"])
    assert [i["kind"] for i in plan["intents"]] == ["buy"]


def test_each_real_fill_and_close_updates_the_benchmark(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    calls = []
    agent.benchmark.update = lambda now: calls.append(now)
    plan = agent.plan(now=NOW, tickers=["NVDA"])
    calls.clear()
    agent.sync(NOW)  # nothing placed yet
    assert calls == []
    for intent in plan["intents"]:
        alpaca.execute(intent)
    calls.clear()
    agent.sync(NOW)  # filled
    assert calls == [NOW]
    alpaca.hit_leg("NVDA", "limit")
    agent.sync(NOW)  # closed
    assert calls == [NOW, NOW]
    [t] = agent.store.closed_trades()
    assert (t["spy_entry"], t["spy_exit"]) == (500, 500)


def test_dashboard_state_includes_the_benchmark(make_agent, settings):
    agent = make_agent({})
    assert build_state(agent, settings)["benchmark"] == {"start": None}
    agent.benchmark.open_from_screen([row("NVDA", 0.9)], NOW)
    b = build_state(agent, settings)["benchmark"]
    assert b["bench"]["open"] == 1 and b["open"][0]["ticker"] == "NVDA"
    assert build_state(make_agent({}, benchmark=False), settings)["benchmark"] is None
