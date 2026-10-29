"""Client for the official TradingView MCP server (streamable HTTP + OAuth 2.1).

The first connection opens a browser to sign in to TradingView. Tokens are
then cached in .tokens/ so later runs connect silently.
"""

from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .config import SETTINGS, Settings


class FileTokenStorage(TokenStorage):
    """Keeps OAuth tokens and the dynamic client registration on disk."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._tokens = directory / "tokens.json"
        self._client = directory / "client.json"

    async def get_tokens(self) -> OAuthToken | None:
        if self._tokens.exists():
            return OAuthToken.model_validate_json(self._tokens.read_text())
        return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens.write_text(tokens.model_dump_json())

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if self._client.exists():
            return OAuthClientInformationFull.model_validate_json(self._client.read_text())
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client.write_text(client_info.model_dump_json())


class _CallbackServer:
    """One-shot localhost server that receives the OAuth redirect."""

    def __init__(self, port: int):
        self.port = port
        self.result: dict[str, str | None] = {}
        self._done = threading.Event()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                params = parse_qs(urlparse(self.path).query)
                outer.result = {
                    "code": params.get("code", [None])[0],
                    "state": params.get("state", [None])[0],
                    "error": params.get("error", [None])[0],
                }
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2>TradingView connected. You can close this tab.</h2>")
                outer._done.set()

            def log_message(self, *args):
                pass

        return Handler

    def wait(self, timeout: float = 300) -> tuple[str, str | None]:
        server = HTTPServer(("127.0.0.1", self.port), self._handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            if not self._done.wait(timeout):
                raise TimeoutError("Timed out waiting for TradingView OAuth callback")
        finally:
            server.shutdown()
        if self.result.get("error") or not self.result.get("code"):
            raise RuntimeError(f"OAuth failed: {self.result.get('error') or 'no code returned'}")
        return self.result["code"], self.result.get("state")


def _oauth_provider(settings: Settings) -> OAuthClientProvider:
    redirect_uri = f"http://localhost:{settings.oauth_callback_port}/callback"

    async def redirect_handler(authorization_url: str) -> None:
        print(f"Opening browser for TradingView login:\n  {authorization_url}")
        webbrowser.open(authorization_url)

    async def callback_handler() -> tuple[str, str | None]:
        return await asyncio.to_thread(_CallbackServer(settings.oauth_callback_port).wait)

    return OAuthClientProvider(
        server_url=settings.tv_mcp_url,
        client_metadata=OAuthClientMetadata(
            client_name="trading-journey",
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=FileTokenStorage(settings.token_dir),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


@asynccontextmanager
async def tradingview_session(settings: Settings = SETTINGS) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP session with the TradingView server."""
    async with streamablehttp_client(settings.tv_mcp_url, auth=_oauth_provider(settings)) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_tools(settings: Settings = SETTINGS) -> list[dict]:
    async with tradingview_session(settings) as session:
        result = await session.list_tools()
        return [{"name": t.name, "description": t.description or ""} for t in result.tools]


async def call_tool(name: str, arguments: dict, settings: Settings = SETTINGS) -> str:
    async with tradingview_session(settings) as session:
        result = await session.call_tool(name, arguments)
        parts = [getattr(c, "text", None) or json.dumps(c.model_dump()) for c in result.content]
        return "\n".join(parts)
