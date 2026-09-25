"""Bedrock Guardrail: prompt-attack filter on user input (one layer of defence in depth).

A guardrail lowers the probability that an injection reaches the model; it cannot make a
money-moving action safe on its own. The Cedar policy at the Gateway is the enforcement point.
"""

from __future__ import annotations

from aws_cdk import aws_bedrock as bedrock
from constructs import Construct

from ..config import StageConfig

_BLOCKED_MESSAGE = "Sorry, I can't help with that request. A human support agent can assist you further."


class InputGuardrail(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig) -> None:
        super().__init__(scope, construct_id)

        self.guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"{config.resource_prefix}-input",
            description="Prompt-attack and abuse filtering for the customer support agent",
            blocked_input_messaging=_BLOCKED_MESSAGE,
            blocked_outputs_messaging=_BLOCKED_MESSAGE,
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    # PROMPT_ATTACK only applies to input.
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="HATE", input_strength="HIGH", output_strength="HIGH"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="VIOLENCE", input_strength="HIGH", output_strength="HIGH"
                    ),
                ]
            ),
        )
        self.version = bedrock.CfnGuardrailVersion(
            self, "Version", guardrail_identifier=self.guardrail.attr_guardrail_id
        )
