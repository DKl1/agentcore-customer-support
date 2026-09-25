"""Create/update the AgentCore Identity OAuth2 credential provider used by the Runtime to
obtain Gateway tokens (M2M / client credentials).

The Cognito client secret is read with the operator's IAM credentials and passed straight
to the AgentCore Identity token vault (backed by Secrets Manager). It is never printed,
logged or written to disk.

Usage: python scripts/configure_identity.py [--stage dev]
"""

from __future__ import annotations

import argparse

from _common import client, load_outputs, log
from botocore.exceptions import ClientError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="dev")
    args = parser.parse_args()

    outputs = load_outputs(args.stage)
    cognito = client("cognito-idp", outputs)
    control = client("bedrock-agentcore-control", outputs)
    name = outputs["CredentialProviderName"]

    app_client = cognito.describe_user_pool_client(
        UserPoolId=outputs["UserPoolId"], ClientId=outputs["GatewayClientId"]
    )["UserPoolClient"]
    provider_config = {
        "customOauth2ProviderConfig": {
            "oauthDiscovery": {"discoveryUrl": outputs["GatewayDiscoveryUrl"]},
            "clientId": app_client["ClientId"],
            "clientSecret": app_client["ClientSecret"],
        }
    }

    try:
        control.get_oauth2_credential_provider(name=name)
        control.update_oauth2_credential_provider(
            name=name, credentialProviderVendor="CustomOauth2", oauth2ProviderConfigInput=provider_config
        )
        log.info("Updated OAuth2 credential provider %s", name)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        control.create_oauth2_credential_provider(
            name=name, credentialProviderVendor="CustomOauth2", oauth2ProviderConfigInput=provider_config
        )
        log.info("Created OAuth2 credential provider %s", name)


if __name__ == "__main__":
    main()
