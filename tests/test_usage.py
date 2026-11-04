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


def test_scan_cost_per_plan_and_run(tmp_path):
    from conftest import decision

    from trading_journey.store import Store

    store = Store(tmp_path / "j.db")
    for plan_id, cost in [("202611041445-aaaa", 1.5), ("202611041445-aaaa", 2.0), ("202611051445-bbbb", 0.5)]:
        d = decision("NVDA", "Buy")
        d.usage = {"cost": cost, "models": {}}
        store.record_decision(d, "buy", plan_id=plan_id)
    store.record_decision(decision("AMD", "Hold"), "hold", plan_id="202611051445-bbbb")  # not costed
    assert store.scan_costs() == {"202611041445-aaaa": 3.5, "202611051445-bbbb": 0.5}

    store.add_run(started="2026-11-05T14:45:00+00:00", plan_id="202611051445-bbbb", status="executed")
    store.add_run(started="2026-11-05T15:00:00+00:00", plan_id="202611051500-cccc", status="no_orders")
    assert [r["cost"] for r in store.recent_runs()] == [None, 0.5]


def test_cost_per_agent_through_a_langgraph_graph():
    """LangGraph's node names reach callbacks attached to the LLM itself, sub-graphs included."""
    from typing import TypedDict

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langgraph.graph import END, START, StateGraph

    def reply(i, tool=False):
        return AIMessage(f"r{i}", response_metadata={"model": "claude-haiku-5-5"},
                         usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                         tool_calls=[{"name": "get_news", "args": {}, "id": f"t{i}"}] if tool else [])

    u = UsageTracker()
    llm = GenericFakeChatModel(messages=iter([reply(0, tool=True), reply(1), reply(2)]), callbacks=[u])

    class S(TypedDict):
        x: str

    def node(state):
        return {"x": llm.invoke("hi").content}

    # An analyst: a sub-graph whose last turn is the wrap-up, as in TradingAgents.
    analyst = StateGraph(S)
    analyst.add_node("agent", node)
    analyst.add_node("wrap_up", node)
    analyst.add_edge(START, "agent")
    analyst.add_edge("agent", "wrap_up")
    analyst.add_edge("wrap_up", END)
    graph = StateGraph(S)
    graph.add_node("Market Analyst", analyst.compile())
    graph.add_node("Portfolio Manager", node)
    graph.add_edge(START, "Market Analyst")
    graph.add_edge("Market Analyst", "Portfolio Manager")
    graph.add_edge("Portfolio Manager", END)
    graph.compile().invoke({"x": ""})

    agents = u.summary()["agents"]
    assert set(agents) == {"Market Analyst", "Portfolio Manager"}
    market, pm = agents["Market Analyst"], agents["Portfolio Manager"]
    assert (market["calls"], market["tool_calls"], market["wrap_ups"], market["input"]) == (2, 1, 1, 2000)
    assert (pm["calls"], pm["tool_calls"], pm["wrap_ups"]) == (1, 0, 0)
    assert round(sum(a["cost"] for a in agents.values()), 6) == round(u.cost(), 6)
    assert all(a["seconds"] >= 0 for a in agents.values())
