// Cedar policies for the Customer Support Agent's AgentCore Gateway.
//
// Evaluated by the AgentCore Policy engine on EVERY MCP tools/call, before the Lambda target
// is invoked, independently of what the LLM "decided". Default is DENY: any tool that is not
// explicitly permitted below cannot be called, and `forbid` always wins over `permit`.
//
// Placeholders rendered by scripts/configure_policy.py:
//   {{GATEWAY_ARN}}   the Gateway these policies protect
//   {{TARGET}}        the Gateway target name (tool names are "<target>___<tool>")
//   {{MAX_REFUND}}    refund limit in cents (100000 = $1,000.00)
//
// Each statement is separated by a line containing only "// ---" (one Policy resource each).

// Read-only tools: always allowed for the agent's authenticated client.
@id("allow-read-only-tools")
permit(
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"{{TARGET}}___get_order_status",
    AgentCore::Action::"{{TARGET}}___get_customer"
  ],
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
);
// ---
// Refunds: allowed only up to the limit.
@id("allow-refund-up-to-limit")
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"{{TARGET}}___refund_customer",
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
)
when {
  context.input has amount_cents &&
  context.input.amount_cents > 0 &&
  context.input.amount_cents <= {{MAX_REFUND}}
};
// ---
// Explicit guard rail: a refund above the limit is forbidden even if another permit is added
// later by mistake (forbid overrides permit).
@id("forbid-refund-over-limit")
forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"{{TARGET}}___refund_customer",
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
)
when {
  context.input has amount_cents && context.input.amount_cents > {{MAX_REFUND}}
};
