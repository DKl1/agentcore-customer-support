# Evidence

Evidence is **generated from a real deployment**. Nothing in this folder is hand-written.

| Folder | Produced by | Shows |
|---|---|---|
| `runs/<UTC timestamp>/*.json` | `make e2e` (one file per scenario) | Prompt, agent reply, `tool_calls` ledger (outcome, attempts, error code), `trace_id`, operation ids |
| `observability/*.json` | `make evidence` | Results of the saved Logs Insights queries: timeouts, invalid params, wrong-tool candidates, HTTP 500, loops, policy denials |
| `policy/gateway.json`, `policy/policies.json`, `policy/runtime.json` | `make evidence` | Gateway authorizer + policy engine in `ENFORCE` mode, active Cedar statements, Runtime MMDSv2/lifecycle |
| `screenshots/` | manual | Trace views (see checklist) |

## Screenshot checklist

1. GenAI Observability → agent → session → trace for **"Why is my order 123 delayed?"** (span tree with `chat` → `execute_tool get_order_status` → `gateway.tool_call`).
2. Trace for **order 777** (tool timeout): `gateway.tool_call` with 3 `tool.attempt` events, ERROR status.
3. Trace for **order 500** (HTTP 500): `http.response.status_code=500`, 3 attempts.
4. Trace for the **loop** prompt (order 999): repeated `execute_tool` spans, then `rejected_by_guard`.
5. Trace or log line for the **$5,000 injection**: `tool_call_denied_by_policy`, and no Lambda invocation in X-Ray for that call.
6. Logs Insights `support-agent-dev/2-invalid-parameters` result.
7. Memory: session 1 and session 2 transcripts plus the extracted record (`test_memory_works_across_two_sessions.json`).
8. DynamoDB `refunds` table filtered by `idempotency_key = operation-123` → exactly one item.
9. CloudWatch dashboard `support-agent-dev-operations`.
