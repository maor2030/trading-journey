import json

from trading_journey.config import PROJECT_ROOT, SETTINGS, tradingagents_config
from trading_journey.universe import DEFAULT_UNIVERSE


def test_tradingagents_config_uses_anthropic():
    config = tradingagents_config()
    assert config["llm_provider"] == "anthropic"
    assert config["deep_think_llm"]
    assert config["quick_think_llm"]


def test_mcp_json_registers_tradingview():
    data = json.loads((PROJECT_ROOT / ".mcp.json").read_text())
    server = data["mcpServers"]["mcp-tradingview"]
    assert server["type"] == "http"
    assert server["url"] == "https://mcp.tradingview.com/mcp"


def test_data_dir_is_outside_onedrive():
    assert "OneDrive" not in str(SETTINGS.data_dir)


def test_universe_size():
    assert 40 <= len(DEFAULT_UNIVERSE) <= 100
