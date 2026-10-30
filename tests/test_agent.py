from datetime import timedelta

from conftest import NOW, decision, run_cycle

from trading_journey.scoring import TickerStats, priority, rank_candidates, ticker_stats


def test_market_buy_is_a_gtc_bracket_and_syncs_into_a_trade(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", entry=100, stop=95, target=115)})
    plan = run_cycle(agent, alpaca, tickers=["NVDA"])
    [intent] = plan["intents"]
    args = intent["calls"][0]["args"]
    assert intent["kind"] == "buy" and intent["calls"][0]["tool"] == "place_stock_order"
    assert args | {"client_order_id": None} == {
        "symbol": "NVDA", "side": "buy", "qty": "150", "type": "market", "time_in_force": "gtc",
        "order_class": "bracket", "take_profit_limit_price": "115.00", "stop_loss_stop_price": "95.00",
        "client_order_id": None,
    }
    [t] = agent.store.open_trades()
    assert (t["qty"], t["entry_price"], t["stop"], t["target"]) == (150, 100, 95, 115)
    assert t["stop_order_id"] and t["tp_order_id"]


def test_limit_entry_waits_then_fills(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", entry=97, stop=92, target=115)})
    plan = run_cycle(agent, alpaca, tickers=["NVDA"])
    args = plan["intents"][0]["calls"][0]["args"]
    assert args["type"] == "limit" and args["limit_price"] == "97.00"
    assert not agent.store.open_trades()
    [e] = agent.store.live_entries()
    assert e["status"] == "open" and e["order_id"]

    alpaca.fill_entry(alpaca.order(e["order_id"]), price=96.9)
    agent.sync(NOW)
    assert agent.store.open_trades()[0]["entry_price"] == 96.9
    assert not agent.store.live_entries()


def test_expired_limit_is_cancelled(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", entry=97, stop=92, target=115)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    plan = run_cycle(agent, alpaca, now=NOW + timedelta(days=4), tickers=[])
    assert [i["kind"] for i in plan["intents"]] == ["cancel_entry"]
    assert not agent.store.live_entries()


def test_unplaced_entry_is_dropped(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    agent.plan(now=NOW, tickers=["NVDA"])  # Claude never executes it
    agent.sync(NOW + timedelta(hours=1))
    assert not agent.store.live_entries()


def test_stop_loss_and_take_profit_fills_close_trades(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115),
                        "AMD": decision("AMD", "Buy", stop=47.5, target=60)})
    run_cycle(agent, alpaca, tickers=["NVDA", "AMD"])
    alpaca.hit_leg("NVDA", "stop")
    alpaca.hit_leg("AMD", "limit")
    changes = agent.sync(NOW)
    assert sorted(changes["closed"]) == ["AMD", "NVDA"]
    closed = {t["ticker"]: t for t in agent.store.closed_trades()}
    assert closed["NVDA"]["exit_reason"] == "stop-loss" and closed["NVDA"]["r_multiple"] == -1
    assert closed["AMD"]["exit_reason"] == "take-profit" and closed["AMD"]["pnl"] > 0


def test_trailing_stop_raises_the_bracket_stop(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=130)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    alpaca.prices["NVDA"] = 112  # +2.4R
    plan = run_cycle(agent, alpaca, tickers=[])
    [intent] = plan["intents"]
    assert intent["kind"] == "raise_stop" and intent["calls"][0]["args"]["stop_price"] == "107.00"
    assert agent.store.open_trades()[0]["stop"] == 107  # synced from the replaced leg

    alpaca.hit_leg("NVDA", "stop")
    agent.sync(NOW)
    t = agent.store.closed_trades()[0]
    assert t["exit_reason"] == "trailing stop" and t["pnl"] > 0


def test_time_horizon_exit(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=130, horizon=10)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    plan = run_cycle(agent, alpaca, now=NOW + timedelta(days=11), tickers=[])
    tools = [c["tool"] for c in plan["intents"][0]["calls"]]
    assert tools == ["cancel_order_by_id", "cancel_order_by_id", "close_position"]
    assert agent.store.closed_trades()[0]["exit_reason"] == "time horizon"


def test_bearish_reanalysis_exits(make_agent, alpaca):
    decisions = {"NVDA": decision("NVDA", "Buy", stop=95, target=115)}
    agent = make_agent(decisions)
    run_cycle(agent, alpaca, tickers=["NVDA"])
    decisions["NVDA"] = decision("NVDA", "Sell")
    run_cycle(agent, alpaca, tickers=["NVDA"])
    assert agent.store.closed_trades()[0]["exit_reason"] == "signal"


def test_hold_with_higher_stop_raises_it(make_agent, alpaca):
    decisions = {"NVDA": decision("NVDA", "Buy", stop=95, target=115)}
    agent = make_agent(decisions)
    run_cycle(agent, alpaca, tickers=["NVDA"])
    decisions["NVDA"] = decision("NVDA", "Hold", stop=97)
    plan = run_cycle(agent, alpaca, tickers=["NVDA"])
    assert plan["decisions"][0]["action"] == "hold, tighten stop"
    assert agent.store.open_trades()[0]["stop"] == 97


def test_budget_spans_one_plan(make_agent, alpaca, settings):
    import dataclasses

    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115),
                        "AMD": decision("AMD", "Buy", stop=47.5, target=60),
                        "CRM": decision("CRM", "Buy", stop=190, target=240)})
    agent.settings = dataclasses.replace(settings, max_positions=2)
    plan = agent.plan(now=NOW, tickers=["NVDA", "AMD", "CRM"])
    assert [i["ticker"] for i in plan["intents"]] == ["NVDA", "AMD"]
    assert plan["decisions"][2]["action"] == "skip: no free position slot"


def test_no_new_analyses_when_slots_full(make_agent, alpaca, settings):
    import dataclasses

    calls = []
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    agent.settings = dataclasses.replace(settings, max_positions=1)
    agent.analyze_fn = lambda t, d, portfolio=None: calls.append(t)
    agent.plan(now=NOW + timedelta(hours=1))
    assert calls == []  # NVDA analyzed <20h ago, no free slot for others


def test_kill_switch_and_dry_run(make_agent, alpaca, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    plan = agent.plan(now=NOW, tickers=["NVDA"], dry_run=True)
    assert plan["intents"] and not agent.store.live_entries() and not agent.store.last_analyzed()

    settings.kill_switch.write_text("stop")
    assert agent.plan(now=NOW, tickers=["NVDA"])["halted"]


def test_priority_prefers_winners(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115),
                        "AMD": decision("AMD", "Buy", stop=47.5, target=60)})
    run_cycle(agent, alpaca, tickers=["NVDA", "AMD"])
    alpaca.hit_leg("NVDA", "limit")
    alpaca.hit_leg("AMD", "stop")
    agent.sync(NOW)

    later = NOW + timedelta(days=3)
    stats = ticker_stats(agent.store, list(agent.universe))
    assert stats["NVDA"].score_mult > 1 > stats["AMD"].score_mult
    assert priority(stats["NVDA"], later) > priority(stats["AMD"], later)
    assert rank_candidates(stats, set(), 20, NOW) == ["CRM"]


def test_demoted_ticker_scanned_weekly():
    s = TickerStats("X", trades=5, avg_r=-0.5, decisions=5, last_analyzed=NOW)
    assert rank_candidates({"X": s}, set(), 20, NOW + timedelta(days=2)) == []
    assert rank_candidates({"X": s}, set(), 20, NOW + timedelta(days=8)) == ["X"]


def test_missing_stop_is_detected_and_recreated(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    assert agent.store.open_trades()[0]["protected"] == 1

    alpaca.cancel_bracket_legs("NVDA")
    agent.sync(NOW)
    assert agent.store.open_trades()[0]["protected"] == 0

    plan = run_cycle(agent, alpaca, tickers=[])
    [intent] = plan["intents"]
    assert intent["kind"] == "protect"
    place = intent["calls"][-1]["args"]
    assert (place["side"], place["type"], place["stop_price"], place["qty"]) == ("sell", "stop", "95.00", "150")
    t = agent.store.open_trades()[0]
    assert t["protected"] == 1 and t["stop"] == 95

    # The protective stop fires: the trade closes as a stop-loss.
    stop = alpaca.order_by_client_id(t["stop_client_id"])
    stop.update(status="filled", filled_avg_price="95.0", filled_at="2026-10-28T16:00:00Z")
    alpaca.positions_.pop("NVDA")
    agent.sync(NOW)
    assert agent.store.closed_trades()[0]["exit_reason"] == "stop-loss"


def test_unprotected_above_target_takes_profit(make_agent, alpaca):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    run_cycle(agent, alpaca, tickers=["NVDA"])
    alpaca.cancel_bracket_legs("NVDA")
    alpaca.prices["NVDA"] = 116
    plan = run_cycle(agent, alpaca, tickers=[])
    assert plan["intents"][0]["kind"] == "exit"
    assert agent.store.closed_trades()[0]["exit_reason"] == "take-profit"
