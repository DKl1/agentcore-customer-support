"""AgentCore Memory: short-term events + long-term preference/fact extraction."""

from __future__ import annotations

from aws_cdk import aws_bedrockagentcore as agentcore
from constructs import Construct

from ..config import StageConfig

# Must match src/agent/support_agent/memory.py
PREFERENCES_NAMESPACE = "/preferences/{actorId}"
FACTS_NAMESPACE = "/facts/{actorId}"


class AgentMemory(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig) -> None:
        super().__init__(scope, construct_id)

        self.memory = agentcore.CfnMemory(
            self,
            "Memory",
            name=f"{config.agentcore_prefix}_memory",
            description="Customer support conversations and long-term customer preferences",
            event_expiry_duration=30,  # days of raw short-term events
            memory_strategies=[
                agentcore.CfnMemory.MemoryStrategyProperty(
                    user_preference_memory_strategy=agentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                        name="CustomerPreferences",
                        description="Stated preferences, e.g. preferred AWS region or contact channel",
                        namespaces=[PREFERENCES_NAMESPACE],
                    )
                ),
                agentcore.CfnMemory.MemoryStrategyProperty(
                    semantic_memory_strategy=agentcore.CfnMemory.SemanticMemoryStrategyProperty(
                        name="CustomerFacts",
                        description="Durable facts the customer shared during support conversations",
                        namespaces=[FACTS_NAMESPACE],
                    )
                ),
            ],
        )
