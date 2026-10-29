"""The stocks the agent scans.

Chosen for what TradingAgents feeds on: heavy news and analyst coverage,
deep liquidity, enough volatility to reach a take-profit, and sector spread.
Performance tracking (scoring.py) then promotes or demotes names over time.
Override by creating universe.yaml in the project root: {TICKER: Sector, ...}.
"""

from __future__ import annotations

import yaml

from .config import PROJECT_ROOT

DEFAULT_UNIVERSE: dict[str, str] = {
    # Semiconductors
    "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors",
    "MU": "Semiconductors", "TSM": "Semiconductors", "ARM": "Semiconductors",
    "MRVL": "Semiconductors", "QCOM": "Semiconductors", "SMCI": "Semiconductors",
    # Mega-cap internet
    "META": "Internet", "AMZN": "Internet", "GOOGL": "Internet",
    "MSFT": "Internet", "NFLX": "Internet",
    # Software / cyber
    "PLTR": "Software", "CRM": "Software", "NOW": "Software", "ADBE": "Software",
    "SNOW": "Software", "CRWD": "Software", "PANW": "Software", "DDOG": "Software",
    "NET": "Software", "SHOP": "Software",
    # Mobility / platforms
    "UBER": "Platforms", "ABNB": "Platforms", "TSLA": "Platforms",
    # Consumer
    "NKE": "Consumer", "SBUX": "Consumer", "LULU": "Consumer", "CMG": "Consumer",
    "COST": "Consumer", "DIS": "Consumer",
    # Financials / fintech
    "JPM": "Financials", "GS": "Financials", "COIN": "Financials", "HOOD": "Financials",
    "SOFI": "Financials", "PYPL": "Financials", "V": "Financials",
    # Healthcare
    "LLY": "Healthcare", "NVO": "Healthcare", "UNH": "Healthcare",
    "MRNA": "Healthcare", "ISRG": "Healthcare",
    # Energy / industrials
    "XOM": "Energy", "CVX": "Energy", "CAT": "Industrials", "BA": "Industrials",
    "GE": "Industrials", "DAL": "Industrials",
}


def load_universe() -> dict[str, str]:
    path = PROJECT_ROOT / "universe.yaml"
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {str(t).upper(): str(s) for t, s in data.items()}
    return dict(DEFAULT_UNIVERSE)
