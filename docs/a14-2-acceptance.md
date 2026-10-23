# A14.2 inference-boundary diagnosis and repair — 2026-10-22

## Closure decision

```ini
A14_COMPLETE = NO
A14_LIVE_GRAPH_ACCEPTANCE = BLOCKED_BEFORE_MODEL_DECISION
ACCEPTED_LIVE_GRAPHS = 0
```

The one authorized bounded real retest failed before an inference decision.
Implementation defects were repaired and compatibility verified deterministically,
but real multi-agent coordination was not demonstrated. The admission changes'
causal effectiveness remains unverified; no mocked graph is counted as live proof.

## Trace and evidence

The production path is authenticated text request → durable RequestLifecycle →
AgentHarness → RequestPipeline → frozen AgentTurn, wrapped by the A14 contract →
canonical registry catalog → existing A10.5 model selection/transport → strict
AgentDecision → existing dispatch → `agent.orchestrate` → owned active parent →
Orchestrator → eligible delegated catalog → model-generated TaskGraph → strict
protocol/scope validation → workers → synthesis/verification → native projection.

`agent.orchestrate` is registered among **111** capabilities, available, with no
arguments, a hub target and tier-1 policy. Its enum appears in the actual model
schema. A13 fresh device advertisements govern delegated device tools; A13 does
not filter this hub capability from AgentTurn's canonical catalog. Existing
authorization, autonomy and parent ownership are checked at dispatch. No executor
advertisement or permission was added to force eligibility.

Previous durable task diagnostics show two different implementation-facing
failures: the initial A14 turns rejected `success_condition[0]` with `value_error`
before any execution; A14.1 turns selected and completed only `ui.wait`, without
any graph. Those ordinary model responses disprove an explanation based solely
on provider outages. They do not reveal private model reasoning or establish a
single proven psychological cause for preferring that action.

## Demonstrated defects and bounded repairs

1. **Admission contract loses execution context.** The frozen initial catalog
   reduces descriptions to 32 characters and omits target type. A hub capability
   has an empty device list, while the general instructions focus on individual
   actions. A14.2 places its existing general delegation contract immediately
   before CAPABILITIES, after the general instructions. It explains hub execution,
   respects unavailable state, requires whole-request coverage and distinguishes
   actual workers from constituent action receipts. All original safety text,
   catalog rows and STATE values are retained. The actual model still chooses.
2. **Delegated prompt boundary is broken.** BudgetProvider appended scope text
   after STATE JSON. A deterministic probe reproduced JSON parsing failure; the
   repaired scope placement leaves the complete trailing JSON parseable. This
   was a latent downstream defect, not the cause of earlier parent omissions.
   Callback forwarding also preserves keyword-only provider extensions.
3. **Different proposal protocols share an ambiguous envelope.** Graph planning,
   reasoning output, synthesis and verification use the existing decision-channel
   transport, whose native function is named `agent_decision`. Their model-facing
   instructions now state that the supplied schema is the function's parameters,
   and no AgentDecision/ModelResult wrapper should be added. Transport-level mock
   checks exercise the real Groq payload/response parser with the TaskGraph schema
   for both configured Groq model identifiers. These are compatibility checks,
   not live model-compliance evidence.
4. **Validation feedback was unactionable.** The sole existing planner replan
   previously received only `ValueError`/`PermissionError`. Invalid A14 outputs
   now produce `INVALID_OUTPUT` with at most six input-free locations/types;
   scope errors use fixed local codes. Unknown input keys are replaced rather
   than echoed. Complete explicitly fenced JSON is accepted, consistently with
   AgentTurn, but prose, incomplete JSON, wrong wrappers and invalid graphs are
   rejected without creating a replacement graph. Retry counts are unchanged.

No phrase matcher, graph template, forced tool choice for `agent.orchestrate`,
synthetic decision, new model, provider pin, privacy downgrade or quota bypass
was introduced. No baseline test was removed or relaxed.

## Prompt size, schemas and provider compatibility

The isolated registry prompt measured **16,249 bytes**, decision schema **5,179
bytes**, and wire payload **23,011 bytes** before adding real session state. This
shows that schema overhead is additional to the existing transport's text-byte
metric. Live A14.2 admission recorded **19,996 system-prompt bytes** and **23,045
transport text bytes**, with **6,763** estimated tokens including the router's
output allowance. The initial profile was `general/unknown`, interactive and
INTERNAL; no semantic classifier guessed a planning category. Graph inference
continues using role-derived planning/reasoning categories and background policy.

A14 graph schemas now omit decorative titles/default annotations, retaining
every validation constraint, property name, enum, required field and closed
object boundary. TaskGraph decreased **1,966 → 1,514 bytes**; WorkerOutput
**802 → 689**; Synthesis **408 → 362**; Verification **591 → 493**. The top-level
schema identity remains. Inference input remains bounded to 24 KiB, response
validation to 64 KiB; oversized input is rejected instead of silently losing
criteria or authority. No history/evidence was stripped based on guessed intent.

The existing Groq transport uses native function arguments; NVIDIA uses the JSON
text contract; Gemini requests JSON MIME output with schema instructions. Local
strict validation remains necessary. Groq documents that function-call generation
can return HTTP 400 when generation is invalid, and that JSON syntax alone is
not schema adherence: [local tool calling](https://console.groq.com/docs/tool-use/local-tool-calling),
[structured outputs](https://console.groq.com/docs/structured-outputs).
Strict structured-output modes have model/schema restrictions; they were not
enabled globally or combined with tool calling in this repair.

Historical classifications remain explicit: HTTP 400 `tool_use_failed` maps to
UNSUPPORTED_TOOL_USE/INVALID_OUTPUT; 429 to rate/quota handling; 503 to transient
service unavailability. HTTP 413 takes precedence over contradictory
`rate_limit_exceeded` metadata in the frozen failover policy and maps to
CONTEXT_TOO_LARGE, requiring verified capacity on continuation. This is the
existing policy, not proof that a provider's true context window was exceeded.
The existing 1,024-token direct proposal output cap remains a possible constraint
for verbose graphs; no live truncated graph was obtained to diagnose it here.

## Actual single live attempt

Normal Hub, legitimate ASUS Agent and actual native PyQt/React application were
launched. The existing public acceptance prompt was unchanged: independent
analyses, dependent comparison, harmless one-second ASUS wait and verified
recommendation. No external account access, writes or paid service was requested.
Voice was mocked; provider output and graph decisions were real and not injected.

```text
REQUEST_TASK_ID = request_4c00a98680c93c4d41b8f08c
AGENT_TURN_ID = turn_801b84e9167043a6a422013547324c2d
SELECTED_PROVIDER = groq
SELECTED_MODEL = qwen/qwen3.8-27b
HTTP_STATUS = 413
PROVIDER_ERROR_CODE = rate_limit_exceeded
ROUTER_CATEGORY = CONTEXT_TOO_LARGE
PROVIDER_LATENCY_SECONDS = 0.285
ROOT_STATUS = FAILED
GRAPH_TASK_ROWS = 0
ACCEPTED_GRAPHS = 0
EXECUTOR_RECEIPTS = 0
```

The existing continuation policy rejected Groq GPT-OSS, both configured NVIDIA
candidates and Gemini as `context_window_unknown`; Qwen was `already_attempted`.
They were not called again or forced eligible. The final response was:
“No eligible model with sufficient verified context capacity is available.
Completion is unverified.” It did not manufacture successful work. All owned
processes were stopped; `cleanup.json` confirms cleanup. No second live attempt
was made after this bounded blocker.

## Native acceptance and remaining proof

Computer Use found actual native window **33754102**, `Cortex Control Center`,
owned by Python. The native accessibility tree reported Overview, Hub connected
and one online ASUS device. Workspace navigation first encountered detected user
input; after refreshing, the control returned an out-of-bounds coordinate. It
was not replaced with scripted browser inspection. The exact failed task's native
Workspace display remains unverified in this run. No graph existed to inspect.

Real model selection of orchestration, valid graph admission, two independent
concurrent workers, dependency ordering, delegated ASUS receipt, grounded
synthesis, native graph rendering, graph cancellation/failure and conservative
restart recovery all remain **NOT_REACHED LIVE**. Deterministic coverage of
these mechanisms does not close the live gates. No synthetic durable graph was
created to claim acceptance.

## Automated results and files

Focused compatibility: **181 passed / 0 failed / 0 skipped**, **22.27 seconds**.
This includes **69 A14 checks**, preserving the previous 48 and adding 21 checks.
The earlier A14-only development run passed 67 before the two transport checks
were added; totals overlap and are not added together.

Final full serial regression: **919 passed / 0 failed / 0 skipped / 0 xfailed**, in **182.14 seconds**. All **898 baseline checks** retained, with **21 added** and **0 missing**.

All **11 frozen-file hashes match**. Permissions, approvals, OAuth scopes,
provider ranking/failover/retry limits, and execution policies were preserved.

Production changes: `hub/orchestration_contract.py`,
`hub/orchestration_workers.py`, `hub/orchestration.py`, and new
`hub/orchestration_inference.py`. Tests: `tests/test_a14_orchestration.py`.
Documentation: this report and `docs/a14-orchestration.md`.

Evidence root:
`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a14-2-acceptance`

Artifacts include `diagnosis.json`, `schema-metrics.json`, `focused-compat.xml`,
`live-summary.json`, `native-evidence.json`, `live/routing-evidence.json`,
request/result/runtime/cleanup records, `regression.xml`,
`regression-summary.json` and `frozen-audit.json`.

```ini
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A12_FOUNDATION = COMPLETE
A13 = COMPLETE
A12_2_GENERATIVE_WORKSPACE = DEFERRED
A10_A10_5 = FROZEN
A14 = IN_PROGRESS
```
