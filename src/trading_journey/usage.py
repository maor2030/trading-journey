"""Token usage and API cost of a TradingAgents run."""

from __future__ import annotations

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


class UsageTracker(BaseCallbackHandler):
    """Counts tokens per model across every LLM call of one analysis."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.tokens = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "calls": 0})

    def on_llm_end(self, response, **kwargs) -> None:
        for gen in (g for gens in response.generations for g in gens):
            msg = getattr(gen, "message", None)
            meta = getattr(msg, "usage_metadata", None)
            if not meta:
                continue
            model = (getattr(msg, "response_metadata", None) or {}).get("model") or "unknown"
            cache_read = (meta.get("input_token_details") or {}).get("cache_read") or 0
            t = self.tokens[model]
            t["input"] += meta.get("input_tokens", 0) - cache_read
            t["cache_read"] += cache_read
            t["output"] += meta.get("output_tokens", 0)
            t["calls"] += 1

    def cost(self) -> float:
        total = 0.0
        for model, t in self.tokens.items():
            p_in, p_out, p_cache = price(model)
            total += (t["input"] * p_in + t["output"] * p_out + t["cache_read"] * p_cache) / 1e6
        return total

    def summary(self) -> dict:
        return {"cost": round(self.cost(), 4), "models": {m: dict(t) for m, t in self.tokens.items()}}
