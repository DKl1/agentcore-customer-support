"""Outbound identity: Runtime -> Gateway.

The Gateway accepts only JWTs issued by our Cognito user pool to one allowed client. The
agent never sees a client secret: it asks **AgentCore Identity** for a token
(``@requires_access_token``, M2M / client-credentials flow). Identity authenticates the
Runtime's *workload identity*, reads the client secret from its token vault, calls the
Cognito token endpoint and returns a short-lived access token.

No long-lived credentials exist in code, image, or environment variables.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

REFRESH_MARGIN_SECONDS = 120
FALLBACK_TOKEN_LIFETIME_SECONDS = 300


def jwt_expiry(token: str) -> float | None:
    """Read ``exp`` from a JWT payload *without* verifying it.

    Only used to decide when to refresh the cached token; the Gateway performs the real
    signature, issuer, client and expiry validation.
    """
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
        return float(payload["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


class GatewayTokenProvider:
    def __init__(
        self,
        provider_name: str,
        scopes: tuple[str, ...],
        *,
        fetch: Callable[[], Awaitable[str]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetch = fetch or self._agentcore_identity_fetcher(provider_name, list(scopes))
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _agentcore_identity_fetcher(provider_name: str, scopes: list[str]) -> Callable[[], Awaitable[str]]:
        from bedrock_agentcore.identity.auth import requires_access_token

        @requires_access_token(provider_name=provider_name, scopes=scopes, auth_flow="M2M")
        async def _fetch(*, access_token: str) -> str:
            return access_token

        return _fetch

    async def get_token(self) -> str:
        async with self._lock:
            now = self._clock()
            if self._token and now < self._expires_at - REFRESH_MARGIN_SECONDS:
                return self._token
            token = await self._fetch()
            self._token = token
            self._expires_at = jwt_expiry(token) or now + FALLBACK_TOKEN_LIFETIME_SECONDS
            return token
