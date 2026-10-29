from datetime import timedelta

from conftest import NOW, decision

from trading_journey.scoring import TickerStats, priority, rank_candidates, ticker_stats


def test_buy_now_opens_trade_with_bracket(make_agent):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", entry=100, stop=95, target=115)})
    [(ticker, action)] = agent.scan(tickers=["NVDA"], now=NOW)
    assert action.startswith("Buy: buy 150")
    [t] = agent.store.open_trades()
    assert (t["qty"], t["entry_price"], t["stop"], t["target"]) == (150, 100, 95, 115)
    assert agent.broker.account().cash == 100_000 - 15_000


def test_lower_entry_waits_as_pending_then_fills(make_agent, market):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", entry=97, stop=92, target=115)})
    agent.scan(tickers=["NVDA"], now=NOW)
    assert not agent.store.open_trades()
    [p] = agent.store.open_pending()
    assert p["limit_price"] == 97

    agent.monitor(NOW)  # price still 100: no fill
    assert agent.store.open_pending()
    market.prices["NVDA"] = 96.5
    agent.monitor(NOW)
    assert not agent.store.open_pending()
    assert agent.store.open_trades()[0]["entry_price"] == 96.5


def test_pending_expires(make_agent):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", entry=97, stop=92, target=115)})
    agent.scan(tickers=["NVDA"], now=NOW)
    agent.monitor(NOW + timedelta(days=4))
    assert not agent.store.open_pending()
    assert not agent.store.open_trades()


def test_stop_loss_take_profit_and_time_exits(make_agent, market):
    agent = make_agent({
        "NVDA": decision("NVDA", "Buy", stop=95, target=115),
        "AMD": decision("AMD", "Buy", stop=47.5, target=60),
        "CRM": decision("CRM", "Buy", stop=190, target=240, horizon=10),
    })
    agent.scan(tickers=["NVDA", "AMD", "CRM"], now=NOW)
    assert len(agent.store.open_trades()) == 3

    market.prices.update(NVDA=94.0, AMD=61.0)
    agent.monitor(NOW)
    reasons = {t["ticker"]: t["exit_reason"] for t in agent.store.closed_trades()}
    assert reasons == {"NVDA": "stop-loss", "AMD": "take-profit"}

    agent.monitor(NOW + timedelta(days=11))
    assert {t["ticker"]: t["exit_reason"] for t in agent.store.closed_trades()}["CRM"] == "time horizon"


def test_trailing_stop(make_agent, market):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=130)})
    agent.scan(tickers=["NVDA"], now=NOW)
    market.prices["NVDA"] = 106  # +1.2R -> breakeven
    agent.monitor(NOW)
    assert agent.store.open_trades()[0]["stop"] == 100
    market.prices["NVDA"] = 112  # +2.4R -> trail 1R under the high
    agent.monitor(NOW)
    assert agent.store.open_trades()[0]["stop"] == 107
    market.prices["NVDA"] = 106.5
    agent.monitor(NOW)
    [t] = agent.store.closed_trades()
    assert t["exit_reason"] == "trailing stop" and t["pnl"] > 0


def test_bearish_reanalysis_exits_and_hold_keeps(make_agent):
    decisions = {"NVDA": decision("NVDA", "Buy", stop=95, target=115)}
    agent = make_agent(decisions)
    agent.scan(tickers=["NVDA"], now=NOW)
    decisions["NVDA"] = decision("NVDA", "Hold", stop=97)
    [(_, action)] = agent.scan(tickers=["NVDA"], now=NOW)
    assert "hold" in action and agent.store.open_trades()[0]["stop"] == 97
    decisions["NVDA"] = decision("NVDA", "Underweight")
    agent.scan(tickers=["NVDA"], now=NOW)
    assert agent.store.closed_trades()[0]["exit_reason"] == "signal"


def test_max_positions_and_kill_switch(make_agent, settings):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    agent.settings = __import__("dataclasses").replace(settings, max_positions=0)
    [(_, action)] = agent.scan(tickers=["NVDA"], now=NOW)
    assert "no free position slot" in action

    settings.kill_switch.write_text("stop")
    assert agent.scan(tickers=["NVDA"], now=NOW) == []


def test_dry_run_places_nothing(make_agent):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115)})
    [(_, action)] = agent.scan(tickers=["NVDA"], dry_run=True, now=NOW)
    assert "dry-run: would buy" in action
    assert not agent.store.open_trades() and not agent.store.last_analyzed()


def test_priority_prefers_winners_and_skips_recent(make_agent, market):
    agent = make_agent({"NVDA": decision("NVDA", "Buy", stop=95, target=115),
                        "AMD": decision("AMD", "Buy", stop=47.5, target=60)})
    agent.scan(tickers=["NVDA", "AMD"], now=NOW)
    market.prices.update(NVDA=116.0, AMD=47.0)
    agent.monitor(NOW)

    later = NOW + timedelta(days=3)
    stats = ticker_stats(agent.store, list(agent.universe))
    assert stats["NVDA"].avg_r > 0 > stats["AMD"].avg_r
    assert stats["NVDA"].score_mult > 1 > stats["AMD"].score_mult
    assert priority(stats["NVDA"], later) > priority(stats["AMD"], later)
    # Analyzed within the re-analysis window -> not eligible yet
    assert rank_candidates(stats, set(), 20, NOW) == ["CRM"]


def test_demoted_ticker_scanned_weekly():
    s = TickerStats("X", trades=5, avg_r=-0.5, decisions=5, last_analyzed=NOW)
    assert rank_candidates({"X": s}, set(), 20, NOW + timedelta(days=2)) == []
    assert rank_candidates({"X": s}, set(), 20, NOW + timedelta(days=8)) == ["X"]
