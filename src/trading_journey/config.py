"""Settings loaded from .env, plus the TradingAgents config built from them."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.getenv(name) or default


def _default_data_dir() -> Path:
    # Outside OneDrive: sync can lock or corrupt the SQLite database.
    base = os.getenv("LOCALAPPDATA") or str(Path.home())
    return Path(_env("TJ_DATA_DIR", str(Path(base) / "trading-journey")))


@dataclass(frozen=True)
class Settings:
    # LLM
    deep_model: str = _env("TA_DEEP_MODEL", "claude-opus-5-5")
    quick_model: str = _env("TA_QUICK_MODEL", "claude-sonnet-5-5")

    # TradingView MCP
    tv_mcp_url: str = _env("TV_MCP_URL", "https://mcp.tradingview.com/mcp")
    oauth_callback_port: int = int(_env("TV_OAUTH_CALLBACK_PORT", "8765"))
    token_dir: Path = PROJECT_ROOT / ".tokens"

    # Execution
    broker: str = _env("BROKER", "local_paper")  # local_paper | tradingview
    trading_mode: str = _env("TRADING_MODE", "paper")  # paper | live
    live_confirm: str = _env("LIVE_CONFIRM", "")
    starting_cash: float = float(_env("PAPER_STARTING_CASH", "100000"))

    # Scheduling
    scans_per_hour: int = int(_env("SCANS_PER_HOUR", "5"))
    monitor_minutes: int = int(_env("MONITOR_MINUTES", "5"))
    reanalyze_hours: float = float(_env("REANALYZE_HOURS", "20"))

    # Risk
    max_positions: int = int(_env("MAX_POSITIONS", "10"))
    risk_per_trade: float = float(_env("RISK_PER_TRADE", "0.01"))
    max_position_pct: float = float(_env("MAX_POSITION_PCT", "0.15"))
    max_sector_pct: float = float(_env("MAX_SECTOR_PCT", "0.30"))
    cash_reserve_pct: float = float(_env("CASH_RESERVE_PCT", "0.10"))
    daily_loss_limit: float = float(_env("DAILY_LOSS_LIMIT", "0.03"))
    limit_expiry_days: int = int(_env("LIMIT_EXPIRY_DAYS", "3"))

    data_dir: Path = field(default_factory=_default_data_dir)
    reports_dir: Path = PROJECT_ROOT / "reports"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trading_journey.sqlite3"

    @property
    def kill_switch(self) -> Path:
        return self.data_dir / "STOP"


SETTINGS = Settings()


def tradingagents_config(settings: Settings = SETTINGS) -> dict:
    """Return a TradingAgents config that uses Anthropic for both thinking tiers."""
    from tradingagents.default_config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = "anthropic"
    config["deep_think_llm"] = settings.deep_model
    config["quick_think_llm"] = settings.quick_model
    return config
