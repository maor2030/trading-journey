"""Command line entry point: `trading-journey tools` / `trading-journey analyze`."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date


def main() -> None:
    parser = argparse.ArgumentParser(prog="trading-journey")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tools", help="Log in to TradingView MCP and list its tools")

    analyze = sub.add_parser("analyze", help="Run TradingView analyst + TradingAgents on a ticker")
    analyze.add_argument("ticker")
    analyze.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")

    args = parser.parse_args()

    if args.command == "tools":
        from .tv_mcp import list_tools

        for tool in asyncio.run(list_tools()):
            print(f"- {tool['name']}: {tool['description'].splitlines()[0] if tool['description'] else ''}")
    elif args.command == "analyze":
        from .pipeline import run

        print(run(args.ticker.upper(), args.date))


if __name__ == "__main__":
    main()
