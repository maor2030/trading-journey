import json

from trading_journey.config import PROJECT_ROOT, tradingagents_config


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
