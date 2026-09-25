"""Inbound identity for the Gateway: a Cognito user pool issuing M2M (client-credentials) JWTs.

Only one app client exists and it is the only client the Gateway accepts
(``allowedClients``). Its secret is never output, never stored in this repo: the
post-deploy script reads it with the operator's IAM credentials and hands it straight to
the AgentCore Identity token vault (OAuth2 credential provider).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from ..config import TOKEN_RESOURCE_SERVER_ID, TOKEN_SCOPE, StageConfig


class GatewayInboundAuth(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{config.resource_prefix}-gateway-auth",
            self_sign_up_enabled=False,
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            removal_policy=config.removal_policy,
        )
        self.domain = self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{config.resource_prefix}-{stack.account}"
            ),
        )

        invoke_scope = cognito.ResourceServerScope(
            scope_name=TOKEN_SCOPE, scope_description="Invoke support tools through the AgentCore Gateway"
        )
        resource_server = self.user_pool.add_resource_server(
            "ResourceServer", identifier=TOKEN_RESOURCE_SERVER_ID, scopes=[invoke_scope]
        )

        self.client = self.user_pool.add_client(
            "AgentRuntimeClient",
            user_pool_client_name=f"{config.resource_prefix}-agent-runtime",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(),  # no user/password flows at all
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.resource_server(resource_server, invoke_scope)],
            ),
            access_token_validity=cdk.Duration.minutes(60),
            prevent_user_existence_errors=True,
        )

        self.scope = f"{TOKEN_RESOURCE_SERVER_ID}/{TOKEN_SCOPE}"
        self.discovery_url = (
            f"https://cognito-idp.{stack.region}.amazonaws.com/{self.user_pool.user_pool_id}"
            "/.well-known/openid-configuration"
        )
