import json
import subprocess

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from trading_journey import claude_code_llm
from trading_journey.claude_code_llm import ChatClaudeCode, transcript
from trading_journey.usage import UsageTracker


@tool
def get_price(ticker: str) -> str:
    """The latest price of a stock."""
    return "100"


class Verdict(BaseModel):
    rating: str
    summary: str


@pytest.fixture
def fake_claude(monkeypatch):
    """Replaces `claude -p`: records each call and answers with the queued outputs."""
    calls, answers = [], []

    def run(cmd, input, env, cwd, **kw):
        calls.append({"cmd": cmd, "prompt": input.decode("utf-8"), "env": env})
        out = {"is_error": False, "result": "", "usage": {"input_tokens": 10, "cache_read_input_tokens": 90,
                                                           "cache_creation_input_tokens": 0, "output_tokens": 20},
               "total_cost_usd": 0.001, **answers.pop(0)}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out).encode(), b"")

    monkeypatch.setattr(claude_code_llm.subprocess, "run", run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return calls, answers


def test_plain_call_uses_the_subscription_not_the_api_key(fake_claude, tmp_path):
    calls, answers = fake_claude
    answers.append({"result": "Hold, the trend is flat."})
    llm = ChatClaudeCode(model="claude-opus-5-5", effort="low", workdir=str(tmp_path))
    reply = llm.invoke([SystemMessage("You are the portfolio manager."), HumanMessage("Rate NVDA.")])

    assert reply.content == "Hold, the trend is flat."
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5" and cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--tools") + 1] == "" and "--json-schema" not in cmd
    assert "ANTHROPIC_API_KEY" not in calls[0]["env"]
    assert "You are the portfolio manager." in calls[0]["prompt"] and "Rate NVDA." in calls[0]["prompt"]
    assert reply.usage_metadata["input_tokens"] == 100 and reply.usage_metadata["output_tokens"] == 20


def test_bound_tools_come_back_as_tool_calls_and_results_go_back_in(fake_claude, tmp_path):
    calls, answers = fake_claude
    answers.append({"structured_output": {"content": "", "tool_calls": [
        {"name": "get_price", "args": {"ticker": "NVDA"}}, {"name": "not_a_tool", "args": {}}]}})
    llm = ChatClaudeCode(model="claude-haiku-5-5", workdir=str(tmp_path)).bind_tools([get_price])
    reply = llm.invoke([HumanMessage("Analyze NVDA.")])

    assert [(c["name"], c["args"]) for c in reply.tool_calls] == [("get_price", {"ticker": "NVDA"})]
    cmd = calls[0]["cmd"]
    assert json.loads(cmd[cmd.index("--json-schema") + 1])["required"] == ["content", "tool_calls"]
    assert "get_price: The latest price of a stock." in calls[0]["prompt"]

    answers.append({"structured_output": {"content": "NVDA trades at 100.", "tool_calls": []}})
    final = llm.invoke([HumanMessage("Analyze NVDA."), reply,
                        ToolMessage("100", tool_call_id=reply.tool_calls[0]["id"], name="get_price")])
    assert final.content == "NVDA trades at 100." and final.tool_calls == []
    assert "[Called get_price" in calls[1]["prompt"] and "Result of tool call get_price" in calls[1]["prompt"]


def test_structured_output_is_validated_against_the_schema(fake_claude, tmp_path):
    calls, answers = fake_claude
    answers.append({"structured_output": {"rating": "Buy", "summary": "Strong trend."}})
    verdict = ChatClaudeCode(model="claude-opus-5-5", workdir=str(tmp_path)).with_structured_output(Verdict) \
        .invoke("Rate NVDA.")

    assert verdict == Verdict(rating="Buy", summary="Strong trend.")
    cmd = calls[0]["cmd"]
    assert "rating" in json.loads(cmd[cmd.index("--json-schema") + 1])["properties"]


def test_a_failed_run_is_retried_then_raises(fake_claude, tmp_path):
    calls, answers = fake_claude
    answers += [{"is_error": True, "result": "usage limit reached"}, {"is_error": True, "result": "usage limit reached"}]
    with pytest.raises(RuntimeError, match="usage limit reached"):
        ChatClaudeCode(model="claude-opus-5-5", workdir=str(tmp_path)).invoke("Rate NVDA.")
    assert len(calls) == 2


def test_usage_tracker_still_counts_tokens_per_model(fake_claude, tmp_path):
    _, answers = fake_claude
    answers.append({"result": "ok"})
    tracker = UsageTracker()
    ChatClaudeCode(model="claude-haiku-5-5", workdir=str(tmp_path), callbacks=[tracker]).invoke("hi")
    assert tracker.tokens["claude-haiku-5-5"] == {"input": 10, "output": 20, "cache_read": 90, "calls": 1}


def test_transcript_writes_out_tool_calls():
    ai = AIMessage(content="", tool_calls=[{"name": "get_price", "args": {"ticker": "AMD"}, "id": "c1"}])
    text = transcript([SystemMessage("rules"), HumanMessage("go"), ai, ToolMessage("50", tool_call_id="c1")])
    assert text.startswith("# Instructions\n\nrules") and 'get_price({"ticker": "AMD"}) id=c1' in text
