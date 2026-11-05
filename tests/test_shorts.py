import dataclasses

import pytest

from conftest import NOW, decision, run_cycle

from trading_journey.decision import recommends_short
from trading_journey.sizing import finalize_levels


def test_short_levels_mirror_long():
    lv = finalize_levels(100, None, 105, 85, atr=3.0, side="short")
    assert (lv.entry, lv.stop, lv.target, lv.risk) == (100, 105, 85, 5)
    # A stop below the entry is wrong for a short: fall back to 2x ATR above.
    lv = finalize_levels(100, None, 95, None, atr=3.0, side="short")
    assert (lv.stop, lv.target) == (106, 88)
    # A short entry below the market is treated as "now".
    assert finalize_levels(100, 97, 105, 85, atr=3.0, side="short").entry == 100


def test_sell_rating_opens_a_short_bracket(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Sell", stop=105, target=85)})
    plan = run_cycle(agent, alpaca, tickers=["NVDA"])
    [intent] = plan["intents"]
    args = intent["calls"][0]["args"]
    assert intent["kind"] == "short"
    assert (args["side"], args["type"], args["order_class"], args["qty"]) == ("sell", "market", "bracket", "150")
    assert (args["stop_loss_stop_price"], args["take_profit_limit_price"]) == ("105.00", "85.00")
    [t] = agent.store.open_trades()
    assert (t["side"], t["qty"], t["entry_price"], t["stop"], t["target"]) == ("short", 150, 100, 105, 85)
    assert alpaca.positions_["NVDA"][0] == -150


def test_short_limit_waits_above_the_market(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Underweight", entry=103, stop=108, target=90)})
    plan = run_cycle(agent, alpaca, tickers=["NVDA"])
    args = plan["intents"][0]["calls"][0]["args"]
    assert (args["side"], args["type"], args["limit_price"]) == ("sell", "limit", "103.00")


def test_short_take_profit_and_stop_pnl(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Sell", stop=105, target=85),
                        "AMD": decision("AMD", "Sell", stop=52.5, target=40)})
    run_cycle(agent, alpaca, tickers=["NVDA", "AMD"])
    alpaca.hit_leg("NVDA", "limit")
    alpaca.hit_leg("AMD", "stop")
    agent.sync(NOW)
    closed = {t["ticker"]: t for t in agent.store.closed_trades()}
    assert closed["NVDA"]["exit_reason"] == "take-profit" and closed["NVDA"]["r_multiple"] == 3
    assert closed["NVDA"]["pnl"] == 150 * 15
    assert closed["AMD"]["exit_reason"] == "stop-loss" and closed["AMD"]["r_multiple"] == -1


def test_short_trailing_stop_moves_down(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Sell", stop=105, target=70)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    alpaca.prices["NVDA"] = 94  # +1.2R: stop to breakeven
    plan = run_cycle(agent, alpaca, tickers=[])
    assert plan["intents"][0]["calls"][0]["args"]["stop_price"] == "100.00"
    alpaca.prices["NVDA"] = 88  # +2.4R: 1R above the low
    plan = run_cycle(agent, alpaca, tickers=[])
    assert plan["intents"][0]["calls"][0]["args"]["stop_price"] == "93.00"
    alpaca.prices["NVDA"] = 91  # bounce: the stop never loosens
    assert run_cycle(agent, alpaca, tickers=[])["intents"] == []

    alpaca.hit_leg("NVDA", "stop")
    agent.sync(NOW)
    t = agent.store.closed_trades()[0]
    assert t["exit_reason"] == "trailing stop" and t["pnl"] == 150 * 7


def test_bullish_reanalysis_covers_a_short(make_agent, alpaca):
    decisions = {"NVDA": decision("NVDA", "Sell", stop=105, target=85)}
    agent = make_agent(decisions)
    run_cycle(agent, alpaca, tickers=["NVDA"])
    decisions["NVDA"] = decision("NVDA", "Underweight")
    assert run_cycle(agent, alpaca, tickers=["NVDA"])["decisions"][0]["action"] == "hold"
    decisions["NVDA"] = decision("NVDA", "Buy")
    alpaca.prices["NVDA"] = 97
    run_cycle(agent, alpaca, tickers=["NVDA"])
    t = agent.store.closed_trades()[0]
    assert t["exit_reason"] == "signal" and t["pnl"] == 150 * 3 and "NVDA" not in alpaca.positions_


def test_short_missing_stop_is_a_buy_stop(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Sell", stop=105, target=85)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    alpaca.cancel_bracket_legs("NVDA")
    agent.sync(NOW)
    [intent] = run_cycle(agent, alpaca, tickers=[])["intents"]
    place = intent["calls"][-1]["args"]
    assert intent["kind"] == "protect" and (place["side"], place["stop_price"]) == ("buy", "105.00")


def test_shorts_respect_setting_and_borrow(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Sell", stop=105, target=85),
                        "AMD": decision("AMD", "Sell", stop=52.5, target=40)})
    alpaca.unshortable.add("AMD")
    plan = agent.plan(now=NOW, tickers=["NVDA", "AMD"])
    actions = {d["ticker"]: d["action"] for d in plan["decisions"]}
    assert actions["AMD"] == "skip: not easy to borrow" and actions["NVDA"].startswith("short")

    agent = make_agent({"CRM": decision("CRM", "Sell", stop=210, target=170)})
    agent.settings = dataclasses.replace(settings, allow_shorts=False)
    assert agent.plan(now=NOW, tickers=["CRM"])["decisions"][0]["action"] == "no action"


STAY_OUT = [
    # Real portfolio-manager summaries where Sell/Underweight meant "stay out".
    "Avoid COIN entirely: no new position at about $140 and no short. The portfolio has no COIN holding.",
    "Avoid entry: take no new long QCOM position and do not initiate an outright short ahead of earnings.",
    "Keep $100K in cash. Do not rest the trader's 405 short entry with a 422 stop in the market.",
    "This Sell means do not buy; it is not an instruction to short.",
    "Buy a put spread. Drop the 230 entry and 255 stop language, which only apply to a stock short.",
    "AMZN gets an Underweight: stay at 0% long and treat bounces into 243-250 as selling zones.",
    "Short-term weakness likely; stay in cash.",
    "",
]


@pytest.mark.parametrize("summary", STAY_OUT)
def test_stay_out_summaries_are_not_shorts(summary):
    assert not recommends_short(summary)


@pytest.mark.parametrize("summary", [
    "Initiate a short at $140 with a stop at $161.27 and a target of $99.30.",
    "Short 48 shares into the bounce; cover at 99.3.",
])
def test_short_summaries_are_shorts(summary):
    assert recommends_short(summary)


def test_sell_that_means_stay_out_places_nothing(make_agent, alpaca):
    agent = make_agent({"COIN": decision("COIN", "Sell", stop=105, target=85, summary=STAY_OUT[0])})
    plan = agent.plan(now=NOW, tickers=["COIN"])
    assert plan["intents"] == []
    assert plan["decisions"][0]["action"] == "no action (analysis says stay out, not short)"
