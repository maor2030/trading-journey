import dataclasses

from conftest import NOW, decision, run_cycle

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
