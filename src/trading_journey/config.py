"""Settings loaded from .env, plus the TradingAgents config built from them."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    deep_model: str = os.getenv("TA_DEEP_MODEL", "claude-opus-5-5")
    quick_model: str = os.getenv("TA_QUICK_MODEL", "claude-sonnet-5-5")
    tv_mcp_url: str = os.getenv("TV_MCP_URL", "https://mcp.tradingview.com/mcp")
    oauth_callback_port: int = int(os.getenv("TV_OAUTH_CALLBACK_PORT", "8765"))
    token_dir: Path = PROJECT_ROOT / ".tokens"
    reports_dir: Path = PROJECT_ROOT / "reports"


SETTINGS = Settings()


def tradingagents_config(settings: Settings = SETTINGS) -> dict:
    """Return a TradingAgents config that uses Anthropic for both thinking tiers."""
    from tradingagents.default_config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = "anthropic"
    config["deep_think_llm"] = settings.deep_model
    config["quick_think_llm"] = settings.quick_model
    return config
