from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from trading_journey.usage import UsageTracker, price


def _result(model, inp, out, cache_read=0):
    msg = AIMessage("x", response_metadata={"model": model},
                    usage_metadata={"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
                                    "input_token_details": {"cache_read": cache_read}})
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


def test_cost_per_model():
    u = UsageTracker()
    u.on_llm_end(_result("claude-haiku-5-5", 1_000_000, 100_000))
    u.on_llm_end(_result("claude-sonnet-5-5", 200_000, 10_000, cache_read=100_000))
    # Haiku: 0.10 + 0.05; Sonnet: 100k fresh 0.20 + 100k cached 0.02 + 10k out 0.10
    assert round(u.cost(), 4) == 0.47
    assert u.summary()["models"]["claude-haiku-5-5"]["calls"] == 1
    u.reset()
    assert u.cost() == 0


def test_snapshot_ids_and_unknown_models():
    assert price("claude-sonnet-5-5-20261001") == price("claude-sonnet-5-5")
    assert price("claude-sonnet-5") == (2.00, 10.00, 0.20)
    assert price("mystery") == price("claude-opus-5-5")


def test_decision_cost_is_journaled(tmp_path):
    from conftest import decision

    from trading_journey.store import Store

    store = Store(tmp_path / "j.db")
    d = decision("NVDA", "Buy")
    d.usage = {"cost": 0.08, "models": {}}
    store.record_decision(d, "buy", ts="2026-10-30T14:00:00+00:00")
    store.record_decision(decision("AMD", "Hold"), "hold", ts="2026-10-30T14:05:00+00:00")
    assert store.api_cost("2026-10-30") == (0.08, 1)
    assert store.recent_decisions()[1]["cost"] == 0.08
