"""Token usage and API cost of a TradingAgents run."""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from langchain_core.callbacks import BaseCallbackHandler

# $ per million tokens: input, output, cache read (Anthropic list prices).
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}


def price(model: str) -> tuple[float, float, float]:
    # Snapshot ids carry a date suffix; match on the longest known prefix.
    for name in sorted(PRICES, key=len, reverse=True):
        if model.startswith(name):
            return PRICES[name]
    return PRICES["claude-opus-5-5"]  # unknown: assume the expensive end


def agent_name(metadata: dict | None) -> str:
    """The TradingAgents agent a call belongs to, from LangGraph's run metadata.

    Analysts run as sub-graphs named after their parent node ("Market Analyst"),
    so the checkpoint namespace's first segment names them; other agents are
    plain nodes ("Bull Researcher", "Portfolio Manager", "Memory Log").
    """
    metadata = metadata or {}
    ns = metadata.get("langgraph_checkpoint_ns") or ""
    return ns.split("|")[0].split(":")[0] or metadata.get("langgraph_node") or "other"


def _blank() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "calls": 0}


class UsageTracker(BaseCallbackHandler):
    """Counts tokens per model and per agent across every LLM call of one analysis."""

    def __init__(self):
        self._lock = threading.Lock()  # the analysts run in parallel
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.tokens = defaultdict(_blank)
            self.agents = defaultdict(lambda: {**_blank(), "cost": 0.0, "seconds": 0.0, "tool_calls": 0,
                                               "wrap_ups": 0})
            self._runs: dict = {}  # run_id -> (agent, wrap_up, start time)

    def on_chat_model_start(self, serialized, messages, *, run_id=None, metadata=None, **kwargs) -> None:
        with self._lock:
            self._runs[run_id] = (agent_name(metadata), (metadata or {}).get("langgraph_node") == "wrap_up",
                                  time.monotonic())

    def on_llm_end(self, response, *, run_id=None, **kwargs) -> None:
        with self._lock:
            agent, wrap_up, started = self._runs.pop(run_id, ("other", False, None))
            a = self.agents[agent]
            if started is not None:
                a["seconds"] += time.monotonic() - started
            a["wrap_ups"] += int(wrap_up)
            for gen in (g for gens in response.generations for g in gens):
                msg = getattr(gen, "message", None)
                meta = getattr(msg, "usage_metadata", None)
                if not meta:
                    continue
                model = (getattr(msg, "response_metadata", None) or {}).get("model") or "unknown"
                cache_read = (meta.get("input_token_details") or {}).get("cache_read") or 0
                fresh, out = meta.get("input_tokens", 0) - cache_read, meta.get("output_tokens", 0)
                for t in (self.tokens[model], a):
                    t["input"] += fresh
                    t["cache_read"] += cache_read
                    t["output"] += out
                    t["calls"] += 1
                p_in, p_out, p_cache = price(model)
                a["cost"] += (fresh * p_in + out * p_out + cache_read * p_cache) / 1e6
                a["tool_calls"] += len(getattr(msg, "tool_calls", None) or [])

    def cost(self) -> float:
        total = 0.0
        for model, t in self.tokens.items():
            p_in, p_out, p_cache = price(model)
            total += (t["input"] * p_in + t["output"] * p_out + t["cache_read"] * p_cache) / 1e6
        return total

    def summary(self) -> dict:
        return {"cost": round(self.cost(), 4), "models": {m: dict(t) for m, t in self.tokens.items()},
                "agents": {n: {**a, "cost": round(a["cost"], 5), "seconds": round(a["seconds"], 2)}
                           for n, a in self.agents.items()}}
