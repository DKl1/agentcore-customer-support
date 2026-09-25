"""Operator CLI.

python client/cli.py chat "Why is my order 123 delayed?" [--customer CUST-1001] [--session ID] [--operation-id X]
python client/cli.py list-tools
python client/cli.py call-tool refund_customer '{"customer_id": "CUST-1001", ...}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from _common import load_outputs
from gateway_probe import GatewayProbe
from support_client import SupportAgentClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--stage", default="dev")
    commands = parser.add_subparsers(dest="command", required=True)

    chat = commands.add_parser("chat", help="Invoke the agent on AgentCore Runtime")
    chat.add_argument("prompt")
    chat.add_argument("--customer", default="CUST-1001")
    chat.add_argument("--session", help="Existing runtimeSessionId (default: new session)")
    chat.add_argument("--operation-id")

    commands.add_parser("list-tools", help="tools/list against the Gateway")
    call = commands.add_parser("call-tool", help="tools/call against the Gateway (bypasses the agent)")
    call.add_argument("tool")
    call.add_argument("arguments", help="JSON object")

    args = parser.parse_args()
    outputs = load_outputs(args.stage)

    if args.command == "chat":
        agent = SupportAgentClient(outputs["RuntimeArn"], outputs["Region"])
        session_id = args.session or agent.new_session_id(args.customer)
        reply = agent.invoke(
            customer_id=args.customer,
            session_id=session_id,
            prompt=args.prompt,
            operation_id=args.operation_id,
        )
        print(json.dumps(reply.raw, indent=2))
        return

    probe = GatewayProbe(outputs)
    token = probe.access_token()
    if args.command == "list-tools":
        print(json.dumps(asyncio.run(probe.list_tools(token)), indent=2))
    else:
        result = asyncio.run(probe.call_tool(token, args.tool, json.loads(args.arguments)))
        print(json.dumps({"is_error": result.is_error, "text": result.text}, indent=2))


if __name__ == "__main__":
    main()
