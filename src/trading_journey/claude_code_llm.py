"""TradingAgents' LLM calls run through headless Claude Code (`claude -p`) instead of the API.

Each LangChain call becomes one `claude -p` process, logged in with the user's
Claude subscription: no built-in tools, no MCP servers, no settings, no session
saved. The conversation goes in on stdin as a transcript. Tool calling and
structured output both go through `--json-schema`: with tools bound, Claude
answers with the tool calls to make (TradingAgents runs them and calls again
with the results), and `with_structured_output` gets JSON matching the schema.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool

logger = logging.getLogger("trading_journey.claude_code_llm")

SYSTEM = ("You are one agent in a multi-agent stock analysis pipeline. The message holds your instructions "
          "and the conversation so far. Do what the instructions ask. When tools are listed, they are how "
          "you get data: you have none until you call them.")
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "Your answer. Empty while you call tools."},
        "tool_calls": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "args": {"type": "object"}},
            "required": ["name", "args"],
        }},
    },
    "required": ["content", "tool_calls"],
}
# The API key in .env would make each `claude -p` bill the API instead of the subscription.
_API_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_slots: threading.BoundedSemaphore | None = None
_slots_lock = threading.Lock()


def claude_path() -> str:
    found = shutil.which("claude")
    if found:
        return found
    native = Path(os.path.expanduser("~")) / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(native)


def _semaphore(n: int) -> threading.BoundedSemaphore:
    """One limit for the whole process: the scan runs analyses, and their analysts, in parallel."""
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(max(1, n))
        return _slots


def transcript(messages: list[BaseMessage]) -> str:
    """The conversation as text: instructions first, then each turn, tool calls and results written out."""
    system = [m.content for m in messages if isinstance(m, SystemMessage)]
    parts = ["# Instructions\n\n" + "\n\n".join(_text(c) for c in system)] if system else []
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        if isinstance(m, ToolMessage):
            parts.append(f"# Result of tool call {m.name or ''} ({m.tool_call_id})\n\n{_text(m.content)}")
        elif isinstance(m, AIMessage):
            calls = "".join(f"\n[Called {c['name']}({json.dumps(c['args'], default=str)}) id={c['id']}]"
                            for c in m.tool_calls or [])
            parts.append(f"# Your earlier reply\n\n{_text(m.content)}{calls}".rstrip())
        else:
            parts.append(f"# {'User' if isinstance(m, HumanMessage) else m.type}\n\n{_text(m.content)}")
    return "\n\n".join(parts)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content)


def _tool_instructions(tools: list[dict]) -> str:
    listed = "\n".join(f"- {t['function']['name']}: {t['function'].get('description', '')}\n"
                       f"  parameters: {json.dumps(t['function'].get('parameters', {}))}" for t in tools)
    return ("# Data tools (requested through your answer)\n\n" + listed + "\n\nThese tools are not in your session, "
            "so don't try to call them directly: request them in your structured answer. You have no data except "
            "what the conversation above shows. To get data, answer with the requests in `tool_calls` (exact tool "
            "names, arguments per the parameters; several at once is fine) and an empty `content`. The pipeline "
            "runs them and asks you again with the results. Only when you have the data you need, answer with "
            "your final reply in `content` and an empty `tool_calls`.")


class ChatClaudeCode(BaseChatModel):
    """A LangChain chat model whose every call is a headless `claude -p` run."""

    model: str
    effort: str | None = None
    timeout: float = 900
    max_parallel: int = 4
    retries: int = 1
    workdir: str | None = None  # an empty folder, so no project CLAUDE.md is read

    @property
    def _llm_type(self) -> str:
        return "claude-code"

    @property
    def _identifying_params(self) -> dict:
        return {"model": self.model, "effort": self.effort}

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def with_structured_output(self, schema, **kwargs):
        json_schema = schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema

        def parse(message: AIMessage):
            data = json.loads(message.content)
            return schema.model_validate(data) if hasattr(schema, "model_validate") else data

        return self.bind(response_schema=json_schema) | RunnableLambda(parse)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, tools=None,
                  response_schema=None, **kwargs) -> ChatResult:
        prompt = transcript(messages)
        schema = response_schema
        if tools:
            prompt += "\n\n" + _tool_instructions(tools)
            schema = TOOL_SCHEMA
        out = self._run(prompt, schema)
        usage = out.get("usage") or {}
        fresh, read = usage.get("input_tokens", 0), usage.get("cache_read_input_tokens", 0)
        created = usage.get("cache_creation_input_tokens", 0)
        meta = {"input_tokens": fresh + created + read, "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": fresh + created + read + usage.get("output_tokens", 0),
                "input_token_details": {"cache_read": read, "cache_creation": created}}
        structured = out.get("structured_output")
        tool_calls = []
        if tools:
            structured = structured or {}
            content = structured.get("content", "")
            names = {t["function"]["name"] for t in tools}
            for c in structured.get("tool_calls") or []:
                name = (c.get("name") or "").split(".")[-1]  # "functions.get_x" -> "get_x"
                if name not in names:
                    logger.warning("%s asked for an unknown tool %r", self.model, c.get("name"))
                    continue
                tool_calls.append({"name": name, "args": c.get("args") or {}, "id": f"call_{uuid.uuid4().hex[:12]}",
                                   "type": "tool_call"})
        elif schema:
            content = json.dumps(structured if structured is not None else json.loads(out.get("result") or "null"))
        else:
            content = out.get("result") or ""
        message = AIMessage(content=content, tool_calls=tool_calls, usage_metadata=meta,
                            response_metadata={"model": self.model, "cost_usd": out.get("total_cost_usd"),
                                               "duration_ms": out.get("duration_ms")})
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _run(self, prompt: str, schema: dict | None) -> dict:
        cmd = [claude_path(), "-p", "--output-format", "json", "--model", self.model, "--system-prompt", SYSTEM,
               "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--setting-sources", ""]
        if self.effort:
            cmd += ["--effort", self.effort]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        env = {k: v for k, v in os.environ.items() if k not in _API_ENV}
        workdir = self.workdir or os.path.join(os.path.expanduser("~"), ".trading-journey-llm")
        os.makedirs(workdir, exist_ok=True)
        last = None
        for attempt in range(self.retries + 1):
            with _semaphore(self.max_parallel):
                try:
                    proc = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, env=env,
                                          cwd=workdir, timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    last = f"timed out after {self.timeout:.0f}s"
                    continue
            stdout = proc.stdout.decode("utf-8", "replace")
            try:
                out = json.loads(stdout)
            except json.JSONDecodeError:
                last = f"exit {proc.returncode}: {(stdout or proc.stderr.decode('utf-8', 'replace'))[-500:]}"
                continue
            if out.get("is_error") or proc.returncode != 0:
                last = f"exit {proc.returncode}: {str(out.get('result') or out.get('subtype'))[:500]}"
                continue
            if schema and out.get("structured_output") is None:
                last = "no structured output"
                continue
            return out
        logger.warning("claude -p failed for %s: %s", self.model, last)
        raise RuntimeError(f"claude -p ({self.model}) failed: {last}")


def tier_client_factory(settings):
    """A stand-in for TradingAgents' `create_tier_client` that returns headless-Claude models."""

    class _Client:
        def __init__(self, llm):
            self.llm = llm

        def get_llm(self):
            return self.llm

    def create_tier_client(config: dict, tier: str, **extra):
        llm = ChatClaudeCode(model=config[f"{tier}_think_llm"], effort=settings.effort or None,
                             max_parallel=settings.claude_parallel, callbacks=extra.get("callbacks"),
                             workdir=str(settings.data_dir / "claude-llm"))
        return _Client(llm)

    return create_tier_client
