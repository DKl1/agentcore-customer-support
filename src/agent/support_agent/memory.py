"""AgentCore Memory integration.

* **Short-term memory** — every turn is stored as an event under
  (``actor_id`` = customer, ``session_id`` = runtime session). A new microVM for the same
  session restores the conversation from Memory, not from local state.
* **Long-term memory** — the ``UserPreference`` and ``Semantic`` strategies (configured in
  CDK) asynchronously extract durable facts such as "preferred AWS region is eu-west-1"
  into per-customer namespaces. On every turn the relevant records are retrieved and
  injected into context — this is what makes the preference available in a *new* session.

Namespaces are keyed by ``actorId`` (the authenticated customer), never by prompt content,
so one customer can never retrieve another customer's memories.
"""

from __future__ import annotations

import logging
from typing import Any

from .telemetry import log_event

# Must match the namespaces of the memory strategies in infra (constructs/memory.py).
PREFERENCES_NAMESPACE = "/preferences/{actorId}"
FACTS_NAMESPACE = "/facts/{actorId}"


def build_session_manager(
    *, memory_id: str | None, session_id: str, actor_id: str, region: str
) -> Any | None:
    if not memory_id:
        log_event("memory_disabled", logging.WARNING, reason="MEMORY_ID not configured")
        return None
    try:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
            RetrievalConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=actor_id,
            retrieval_config={
                PREFERENCES_NAMESPACE: RetrievalConfig(top_k=5, relevance_score=0.3),
                FACTS_NAMESPACE: RetrievalConfig(top_k=5, relevance_score=0.5),
            },
        )
        return AgentCoreMemorySessionManager(agentcore_memory_config=config, region_name=region)
    except Exception as error:
        # Memory is an enhancement, not a hard dependency: answer without it rather than fail.
        log_event("memory_unavailable", logging.ERROR, error=type(error).__name__, detail=str(error)[:300])
        return None
