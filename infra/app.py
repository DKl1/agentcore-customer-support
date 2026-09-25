#!/usr/bin/env python3
"""CDK entry point: ``cdk deploy -c stage=dev [-c policyEngineArn=...]``."""

from __future__ import annotations

import aws_cdk as cdk
from support_agent_infra.config import StageConfig
from support_agent_infra.stack import CustomerSupportAgentStack

app = cdk.App()
config = StageConfig.from_context(app)

CustomerSupportAgentStack(
    app,
    f"CustomerSupportAgent-{config.stage}",
    config=config,
    description="Production-like Customer Support Agent on Amazon Bedrock AgentCore",
)

cdk.Tags.of(app).add("project", "agentcore-customer-support")
cdk.Tags.of(app).add("stage", config.stage)

app.synth()
