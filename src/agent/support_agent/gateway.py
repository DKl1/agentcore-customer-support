"""MCP connection to the AgentCore Gateway and dynamic tool discovery."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from mcp.client import streamable_http
from strands.tools.mcp import MCPClient

from .telemetry import log_event

TOOL_NAME_DELIMITER = "___"


def streamable_http_transport(url: str, headers: dict[str, str]) -> AbstractAsyncContextManager[Any]:
    """Streamable-HTTP transport for both ``mcp`` major lines.

    ``mcp`` 1.x: ``streamablehttp_client(url, headers=...)``.
    ``mcp`` 2.x: ``streamable_http_client(url, http_client=...)`` with a caller-owned HTTP client.
    """
    if hasattr(streamable_http, "streamablehttp_client"):
        return streamable_http.streamablehttp_client(url, headers=headers)

    @asynccontextmanager
    async def _transport() -> AsyncIterator[Any]:
        async with (
            streamable_http.create_mcp_http_client(headers=headers) as http_client,
            streamable_http.streamable_http_client(url, http_client=http_client) as streams,
        ):
            yield streams

    return _transport()


def open_gateway_client(gateway_url: str, access_token: str) -> MCPClient:
    """MCP client over streamable HTTP, authenticated with a short-lived Bearer JWT."""
    headers = {"Authorization": f"Bearer {access_token}"}
    return MCPClient(lambda: streamable_http_transport(gateway_url, headers))


def public_tool_name(gateway_tool_name: str) -> str:
    """``support-tools___get_order_status`` -> ``get_order_status``."""
    return gateway_tool_name.split(TOOL_NAME_DELIMITER, 1)[-1]


def discover_business_tools(client: MCPClient, allowed: tuple[str, ...]) -> dict[str, Any]:
    """``tools/list`` against the Gateway, filtered to an explicit allow-list.

    The allow-list is least privilege on the agent side: anything else the Gateway exposes
    (e.g. the built-in semantic search tool, or tools added to the target later) is not
    offered to the model until it is deliberately allowed here.
    """
    discovered: dict[str, Any] = {}
    pagination_token: str | None = None
    while True:
        page = client.list_tools_sync(pagination_token=pagination_token)
        for tool in page:
            name = public_tool_name(tool.mcp_tool.name)
            if name in allowed:
                discovered[name] = tool
        pagination_token = getattr(page, "pagination_token", None)
        if not pagination_token:
            break

    missing = sorted(set(allowed) - discovered.keys())
    if missing:
        # Degrade instead of failing the whole conversation; the alarm on this event pages on-call.
        log_event("gateway_tools_missing", logging.ERROR, missing_tools=missing)
    return discovered
