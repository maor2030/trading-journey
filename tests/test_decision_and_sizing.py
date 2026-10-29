from trading_journey.config import SETTINGS
from trading_journey.decision import parse_decision, parse_horizon_days
from trading_journey.sizing import Levels, finalize_levels, position_size

TRADER = """**Action**: Buy

**Reasoning**: Strong momentum.

**Entry Price**: $1,234.50

**Stop Loss**: 1150

**Position Sizing**: 5% of portfolio

FINAL TRANSACTION PROPOSAL: **BUY**"""

PM = """**Rating**: Overweight

**Executive Summary**: Accumulate on dips.

**Investment Thesis**: ...

**Price Target**: 1400.0

**Time Horizon**: 3-6 months"""


def test_parse_decision():
    d = parse_decision("NVDA", "2026-10-27", "Overweight",
                       {"trader_investment_plan": TRADER, "final_trade_decision": PM})
    assert (d.entry, d.stop, d.target, d.horizon_days) == (1234.5, 1150.0, 1400.0, 90)
    assert d.summary == "Accumulate on dips."


def test_parse_decision_missing_levels():
    d = parse_decision("NVDA", "2026-10-27", "Hold",
                       {"trader_investment_plan": "**Entry Price**: not provided", "final_trade_decision": ""})
    assert d.entry is None and d.stop is None and d.target is None and d.horizon_days == 30


def test_horizon_parsing():
    assert parse_horizon_days("2 weeks") == 14
    assert parse_horizon_days("1 year") == 180
    assert parse_horizon_days("1-2 days") == 5
    assert parse_horizon_days(None) == 30


def test_levels_keep_valid_agent_levels():
    lv = finalize_levels(100, 98, 92, 120, atr=3)
    assert (lv.entry, lv.stop, lv.target) == (98, 92, 120)


def test_levels_fall_back_to_atr_and_min_reward_risk():
    lv = finalize_levels(100, None, 50, 101, atr=3)  # stop too far, target too close
    assert lv.entry == 100 and lv.stop == 94 and lv.target == 112


def test_levels_skip_without_stop_or_atr():
    assert finalize_levels(100, None, None, None, atr=None) is None


def test_entry_above_market_means_buy_now():
    assert finalize_levels(100, 105, 95, 130, atr=3).entry == 100


def test_position_size_risk_based_and_conviction_weighted():
    lv = Levels(100, 95, 115)
    common = dict(equity=100_000, cash=100_000, levels=lv, sector_exposure=0, settings=SETTINGS)
    buy = position_size(rating="Buy", score_mult=1.0, **common)
    over = position_size(rating="Overweight", score_mult=1.0, **common)
    proven = position_size(rating="Buy", score_mult=1.5, **common)
    assert buy == 150  # min(1% risk / $5 = 200, 15% cap = 150)
    assert over == 120  # 0.6% risk / $5
    assert proven == 150  # capped by max position size
    assert position_size(rating="Hold", score_mult=1.0, **common) == 0


def test_position_size_respects_cash_reserve_and_sector_cap():
    lv = Levels(100, 90, 120)
    assert position_size(equity=100_000, cash=12_000, levels=lv, rating="Buy", score_mult=1.0,
                         sector_exposure=0, settings=SETTINGS) == 20  # 12k - 10k reserve
    assert position_size(equity=100_000, cash=100_000, levels=lv, rating="Buy", score_mult=1.0,
                         sector_exposure=25_000, settings=SETTINGS) == 50  # 30% sector cap


def test_full_reasoning_is_kept():
    state = {"trader_investment_plan": TRADER, "final_trade_decision": PM, "market_report": "RSI 61",
             "investment_debate_state": {"bull_history": "Bull: AI demand", "bear_history": "", "judge_decision": "Go"},
             "risk_debate_state": "unexpected shape"}
    d = parse_decision("NVDA", "2026-10-27", "Overweight", state)
    assert list(d.details) == ["Portfolio manager", "Trader", "Research manager", "Bull case", "Market / technicals"]
