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
    deep_model: str = _env("TA_DEEP_MODEL", "claude-sonnet-5-5")  # research/portfolio managers
    quick_model: str = _env("TA_QUICK_MODEL", "claude-haiku-5-5")  # analysts, debaters, trader
    effort: str = _env("TA_EFFORT", "low")  # thinking effort where the model supports it
    analysts: tuple = tuple(_env("TA_ANALYSTS", "market,social,news,fundamentals").replace(" ", "").split(","))

    # Alpaca (read by Python for syncing; orders are placed by Claude via the Alpaca MCP)
    alpaca_key: str = _env("ALPACA_API_KEY", "")
    alpaca_secret: str = _env("ALPACA_SECRET_KEY", "")
    alpaca_paper: bool = _env("ALPACA_PAPER_TRADE", "true").lower() != "false"
    live_confirm: str = _env("LIVE_CONFIRM", "")

    # Scheduling: one screened scan each morning, hourly housekeeping (no LLM) after it
    scan_time: str = _env("SCAN_TIME", "09:45")  # New York time
    daily_scans: int = int(_env("DAILY_SCANS", "15"))  # new candidates analyzed per scan
    scan_workers: int = int(_env("SCAN_WORKERS", "3"))  # TradingAgents analyses run in parallel
    scan_cutoff_min: int = int(_env("SCAN_CUTOFF_MIN", "90"))  # no scan this close to the close
    reanalyze_hours: float = float(_env("REANALYZE_HOURS", "20"))

    # Risk
    allow_shorts: bool = _env("ALLOW_SHORTS", "true").lower() != "false"  # Sell/Underweight opens a short
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
    def plans_dir(self) -> Path:
        return self.data_dir / "plans"

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
    config["anthropic_effort"] = settings.effort or None
    return config
