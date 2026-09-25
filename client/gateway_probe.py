"""Direct MCP client for the AgentCore Gateway — used by tests to simulate a *fully
compromised model*: it sends arbitrary ``tools/call`` requests with a valid token, bypassing
the agent's prompt, guardrail and tool proxy entirely. Whatever the Gateway + Cedar policy
allows here is the true blast radius of the agent.

The operator's IAM credentials are used to read the Cognito client secret in memory only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import boto3
import httpx
from mcp import ClientSession
from mcp.client import streamable_http
from mcp.shared import exceptions as mcp_exceptions

# Renamed from McpError (mcp 1.x) to MCPError (mcp 2.x).
McpError = getattr(mcp_exceptions, "MCPError", None) or mcp_exceptions.McpError


@dataclass(frozen=True, slots=True)
class ProbeResult:
    is_error: bool
    text: str

    def json(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.text)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None


class GatewayProbe:
    def __init__(self, outputs: dict[str, str]) -> None:
        self._outputs = outputs
        self._region = outputs["Region"]

    def access_token(self) -> str:
        cognito = boto3.client("cognito-idp", region_name=self._region)
        app_client = cognito.describe_user_pool_client(
            UserPoolId=self._outputs["UserPoolId"], ClientId=self._outputs["GatewayClientId"]
        )["UserPoolClient"]
        discovery = httpx.get(self._outputs["GatewayDiscoveryUrl"], timeout=10).raise_for_status().json()
        token = httpx.post(
            discovery["token_endpoint"],
            data={"grant_type": "client_credentials", "scope": self._outputs["GatewayTokenScope"]},
            auth=(app_client["ClientId"], app_client["ClientSecret"]),
            timeout=10,
        ).raise_for_status()
        return str(token.json()["access_token"])

    def tool_name(self, tool: str) -> str:
        return f"{self._outputs['GatewayTargetName']}___{tool}"

    async def list_tools(self, token: str) -> list[str]:
        async with self._session(token) as session:
            result = await session.list_tools()
            return [tool.name for tool in result.tools]

    async def call_tool(self, token: str, tool: str, arguments: dict[str, Any]) -> ProbeResult:
        async with self._session(token) as session:
            try:
                result = await session.call_tool(self.tool_name(tool), arguments)
            except McpError as error:  # e.g. policy DENY surfaced as a JSON-RPC error
                return ProbeResult(is_error=True, text=str(error))
            text = "\n".join(getattr(block, "text", "") for block in result.content)
            return ProbeResult(is_error=bool(result.isError), text=text)

    @asynccontextmanager
    async def _session(self, token: str) -> AsyncIterator[ClientSession]:
        headers = {"Authorization": f"Bearer {token}"}
        # mcp 1.x yields (read, write, get_session_id); mcp 2.x yields (read, write).
        async with (
            _transport(self._outputs["GatewayUrl"], headers) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            yield session


@asynccontextmanager
async def _transport(url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
    if hasattr(streamable_http, "streamablehttp_client"):  # mcp 1.x
        async with streamable_http.streamablehttp_client(url, headers=headers) as streams:
            yield streams
        return
    async with (  # mcp 2.x
        streamable_http.create_mcp_http_client(headers=headers) as http_client,
        streamable_http.streamable_http_client(url, http_client=http_client) as streams,
    ):
        yield streams
