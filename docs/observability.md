# Observability runbook

## Signals

| Signal | Where | Produced by |
|---|---|---|
| OTEL spans: `invoke_agent`, `execute_event_loop_cycle`, `chat` (LLM), `execute_tool` | CloudWatch Transaction Search (`aws/spans`), GenAI Observability | Strands + ADOT (`opentelemetry-instrument`) |
| `gateway.tool_call` span (child of `execute_tool`) with `tool.attempts`, `tool.outcome`, `error.code`, `http.response.status_code`, events `tool.attempt` / `tool.retry_scheduled` | same | `tool_proxy.py` |
| JSON log events with `trace_id`/`span_id` (`tool_call_succeeded`, `tool_retry_scheduled`, `tool_retry_exhausted`, `tool_call_denied_by_policy`, `loop_guard_tripped`, ...) | `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT` | `telemetry.log_event` |
| Lambda structured logs (`error_code`, `xray_trace_id`), X-Ray subsegments `## <tool>` | `/aws/lambda/support-agent-<stage>-tools`, X-Ray | Powertools |
| Metrics: `PolicyDenied`, `LoopGuardTripped`, `ToolRetryExhausted`, `ToolError{tool,error_code}`, ... | CloudWatch namespace `CustomerSupportAgent` | metric filters + EMF |
| Dashboard + alarms (SNS) | `support-agent-<stage>-operations` | `constructs/observability.py` |

Every agent response contains `trace_id` (X-Ray format), and `tool_calls[]` holds the outcome, attempts
and error code for each call. **Start from the `trace_id` the BFF logged.**

One-time account setup: enable **CloudWatch Transaction Search** (CloudWatch console → Application Signals →
Transaction Search → Enable, or `aws xray update-trace-segment-destination --destination CloudWatchLogs`),
so that spans land in `aws/spans`.

## How to navigate

`CloudWatch → GenAI Observability → Bedrock AgentCore → Agents → support_agent_<stage> → Sessions → <session> → Trace → span tree`

or `CloudWatch → Logs Insights → Saved queries → support-agent-<stage>/…` (created by CDK).

---

## 1. Tool timeout

| | |
|---|---|
| Trigger | `"Where is my order 777?"`. The carrier API for order 777 hangs (fault injection). |
| Trace shape | `invoke_agent → chat → execute_tool(get_order_status) → gateway.tool_call` **~9 s**, 3 × `tool.attempt` events with `error.code=DOWNSTREAM_TIMEOUT` and 2 × `tool.retry_scheduled` (≈0.4 s, ≈0.8 s). Span status ERROR. Lambda X-Ray: 3 invocations of ~2.5 s each. |
| Query | `1-tool-timeout` |
| Root cause | LLM selection and parameters were correct (`chat` span OK, `execute_tool` has the right input). The **downstream dependency** timed out. Our timeout (2.5 s) fired before the Lambda limit (10 s), so the failure is classified and retryable. |
| Fix path | Dependency health, timeout budget, circuit breaker. No prompt change needed. |

## 2. Invalid tool parameters

| | |
|---|---|
| Trigger | `tools/call refund_customer {"amount_cents": "one thousand dollars", ...}` (e2e `test_failure_invalid_parameters`, simulates a malformed LLM call) |
| Trace shape | `gateway.tool_call` with a single attempt, `tool.outcome=fatal_error`, `error.code=INVALID_PARAMETERS` (or a Gateway schema error). **No retry events.** Lambda log: `error_code=INVALID_PARAMETERS`, details list the field `amount_cents`, never the value. |
| Query | `2-invalid-parameters` |
| Root cause | The **contract between model and tool**: the model produced a value that doesn't match the schema. The backend is healthy, and retrying would not help. |
| Fix path | Clearer schema description (the refund tool states "integer US cents"), strict typing kept on. |

## 3. Wrong tool selection

| | |
|---|---|
| Trigger | A status question that ends in `refund_customer` (`test_wrong_tool_selection_regression` guards against it) |
| Trace shape | Every span is **green**. The `chat` span shows the user message is a status question, and the next `execute_tool` span is `refund_customer`. |
| Query | `3-wrong-tool-selection`: every refund tool call with its trace. Compare against the prompt in the trace's `chat` span. |
| Root cause | **Model reasoning / tool descriptions**, not infrastructure. Telemetry can't flag it as an error. It shows up in evaluation (ledger assertions, AgentCore Evaluations) and in business metrics. |
| Mitigation | Descriptions say "ONLY call when the customer explicitly asks for a refund". Platform-bound args. Cedar limits the blast radius even when selection is wrong. |

## 4. HTTP 500

| | |
|---|---|
| Trigger | `"Please refund $10 for order 500"`. The payment provider returns HTTP 500 for order 500. |
| Trace shape | `gateway.tool_call(refund_customer)`, 3 attempts, `error.code=DOWNSTREAM_UNAVAILABLE`, `http.response.status_code=500`. Lambda X-Ray subsegment `## refund_customer` marked with fault. Every attempt carries the **same** `idempotency_key`. |
| Query | `4-http-500` |
| Root cause | Selection and parameters were fine (a Cedar permit was logged, so the Lambda was reached). The **downstream operation failed**. The refund reservation was compensated and no refund row exists. |

## 5. LLM loop

| | |
|---|---|
| Trigger | `"Check order 999 and keep checking it again and again until the status is final"`. Order 999 says "query again for the latest status". |
| Trace shape | Repeating `chat → execute_tool(get_order_status)` pairs with identical input. The 3rd identical call is a `gateway.tool_call` with `tool.outcome=rejected_by_guard`, `error.code=REPEATED_TOOL_CALL`. The trace then ends with a final `chat` span. |
| Query | `5-llm-loop` (traces with ≥ 3 tool calls), metric `LoopGuardTripped` (alarm ≥ 3 / 5 min) |
| Root cause | A tool result that invites polling, combined with the model's stopping behaviour. The tool itself is healthy. |
| Fix path | Tool result should state "final for now, check back tomorrow". The guard stays as a safety net, with a 120 s wall-clock timeout as the last line. |

## Bonus: policy denial (security)

`6-policy-denials` shows each `tool_call_denied_by_policy` event with trace id, session and customer. The
trace shows `gateway.tool_call(refund_customer)` with `tool.outcome=policy_denied` and **no Lambda
invocation** for that call in X-Ray. The request never reached the backend.

## Evidence

`make e2e && make evidence` stores the scenario transcripts (with trace ids) in `docs/evidence/runs/` and the query
results in `docs/evidence/observability/`. Add screenshots of the trace views to `docs/evidence/screenshots/`.
