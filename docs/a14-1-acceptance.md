# A14.1 live multi-agent acceptance — 2026-10-22

## Decision

```text
A14_LIVE_GRAPH_ACCEPTANCE = BLOCKED
ACCEPTED_LIVE_GRAPHS = 0
A14_COMPLETE = NO
A14 = IN_PROGRESS
```

Three bounded requests used the normal Hub, legitimate ASUS Agent credentials,
real configured inference providers and actual PyQt/QWebEngine application.
The same existing public acceptance request was used throughout; no model
decision, graph, worker result or executor result was injected. Two normal
requests completed a real one-second wait, but neither performed the requested
delegation. Those are acceptance failures, not multi-agent passes. The final
contract retest could not obtain a usable model decision because of provider
failures. Live attempts stopped at that point.

## Actual request results

| Attempt | Request task | Result | A14 acceptance |
|---|---|---|---|
| 1, existing implementation | `request_f18700def858e304f6db41bf` | Root marked COMPLETED; one real ASUS wait, incomplete answer; no graph | FAIL: delegation omitted |
| 2, clarified compact catalog description | `request_44c882bcb131ee54959ceddd` | Root marked COMPLETED; one real ASUS wait, recommendation; no graph | FAIL: delegation omitted |
| 3, complete inference-boundary contract | `request_fc9ad81a3c03486a072cc2cd` | FAILED: provider unavailable, no graph or new executor action | BLOCKED: provider failure |

The two actual executor command IDs were
`6e13b67e38cf44f4852c55e20f55764f` and
`9b26894a9f1a4f13ae507fc8668bf846`. The parent request and AgentTurn mirror each
receipt; evidence collection deduplicates by command ID. They prove only the
bounded waits, not independent worker reasoning or synthesis.

Valid ordinary AgentDecision proposals were returned in attempts 1 and 2.
No TaskGraph was generated or validated live because orchestration was never
entered. Consequently, worker concurrency, dependency ordering, delegated worker
receipts, grounded graph synthesis, graph cancellation and conservative restart
recovery remain NOT_REACHED live. Existing deterministic tests prove those
mechanisms only under controlled model/executor responses.

## Provider failures versus implementation defects

The earlier A14 record contained tool-use HTTP 400, Groq 429/413, Gemini 503 and
invalid success-condition proposals. A14.1 did not assume all providers remained
unavailable: actual Groq responses in the first two attempts demonstrated usable
transport and ordinary capability execution.

The final attempt used existing router ranking/failover:

1. Gemini `gemini-3.8-flash`: HTTP **503**, `SERVICE_UNAVAILABLE_503`, provider
   scope, **7.714 seconds**.
2. Groq `qwen/qwen3.8-27b`: HTTP **413**, provider code
   `rate_limit_exceeded`, **0.244 seconds**. The frozen router recorded
   `CONTEXT_TOO_LARGE`; the observed code/status does not establish a true model
   context-window limit.
3. No eligible continuation remained: Qwen/Gemini were already attempted;
   Groq `openai/gpt-oss-120b` and the configured NVIDIA candidates were rejected
   with `context_window_unknown` under the existing failover capacity policy.

No quota reset, provider-profile override, privacy downgrade, forced provider or
new model was used. The final response retained primary category
`SERVICE_UNAVAILABLE_503`, fallback category `CONTEXT_TOO_LARGE`, fallback HTTP
status **413**. Changing the frozen failure classification/ranking is outside
this repair. No further live calls were made after this bounded failure.

The demonstrated A14 defect was incomplete admission guidance. The frozen
AgentTurn catalog truncates initial descriptions to 32 characters; A14's prior
entry description did not expose its actual parallel-worker contract there.
Changing the description alone did not repair the live omission. A14.1 now
supplies general orchestration guidance at the existing inference boundary:
the model must select the canonical capability to create actual workers rather
than perform one constituent step and call it done. There is no phrase matcher,
prebuilt graph, forced tool decision or additional classifier/model call.

The prompt adapter introduced two compatibility issues during development:
an unused callback was forwarded positionally to providers using keyword
extensions, and appended guidance broke the frozen trailing STATE JSON boundary.
Both were fixed. Unused callbacks are omitted, supplied callbacks are forwarded
by keyword, and the guidance precedes the unchanged original prompt. Regression
tests retain exact model output, request data, schemas, privacy/fallback flags,
recipient identity and parseable structured state. Existing assertions were not
migrated or weakened.

**The repaired admission contract is deterministically verified but its live
effectiveness is still unverified.** The provider failure occurred before a
post-repair decision could demonstrate successful admission.

## Native Workspace findings

Computer Use selected the actual returned `Cortex Control Center` native window
(window ID **461054**) and opened Workspace. It displayed Hub connected, ASUS
online, Workspace connected, and the exact final request as **FAILED**. The
recorded response said the primary AI service was temporarily unavailable and
the backup failed. The failed task did not animate as ongoing successful work.

This proves truthful failure propagation through AgentTurn → durable request →
native Workspace. It does not prove real graph rendering: no graph existed to
inspect. No synthetic graph was inserted to claim a native pass. No graph was
available for meaningful live cancellation or restart recovery. The prior A14
native request-cancellation evidence remains separate from live graph evidence.
Voice was mocked for these text-path probes; live voice was not claimed.

Each attempt's owned Hub, ASUS Agent and native application were stopped. All
three `cleanup.json` files confirm complete owned-process cleanup.

## Changes and tests

Changes are limited to:

- `hub/capability_registry.py`: orchestration purpose and contract description.
- `hub/orchestration_contract.py`: model-guidance adapter; transparent provider,
  privacy, model-result and callback forwarding.
- `hub/pipeline/pipeline.py`: attach that A14 contract to the existing AgentTurn
  inference boundary.
- `tests/test_a14_orchestration.py`: four additional compatibility/contract tests,
  retaining the previous 44 A14 checks.
- A14 architecture documentation and this acceptance report.

Final consolidated regression on the final code: **898 passed, 0 failed, 0 skipped, 0 xfailed**, in **152.55 seconds**. All **894 baseline test identities** were retained, with **4 added checks** and **0 missing checks**.
Focused compatibility checks: **160 passed, 0 failed, 0 skipped**. A separate
earlier core/privacy run passed **117 checks**; these overlap and are not added to
the consolidated total. The A14 module now contains **48 checks**.

The first full development regression was **853 passed / 44 failed**, with all
894 baseline identities retained; it exposed the new adapter incompatibilities
described above. That output is archived, not presented as an external provider
failure. A focused sandbox run encountered **157 passes / 3 setup errors** from
Windows' temporary directory and passed all 160 checks when rerun with the
required test access and dedicated temporary directory. The initial core probe
also required the same `CORTEX_HUB_DEVICE_ID=asus` setting used by the consolidated
runner; no production change or assertion relaxation was used to address it.

All **11 frozen-file hashes match**. Frozen A10/A10.5 selection, failover,
transport, AgentTurn execution and compound UI behavior were not rewritten.
OAuth scopes, approval and privacy policy remain unchanged.

## Remaining blockers and preserved checkpoints

A healthy permitted provider must first demonstrate that the repaired admission
contract selects orchestration. Then a real model-generated graph must prove at
least two concurrent independent workers, a dependent step, an authorized ASUS
receipt and verified synthesis. Those real task states must be inspected in the
native Workspace, followed by graph cancellation/failure and safe restart proof.
Offline green results do not close these live gates.

```text
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A12_FOUNDATION = COMPLETE
A13 = COMPLETE
A12_2_GENERATIVE_WORKSPACE = DEFERRED
A14 = IN_PROGRESS
```

Evidence root:
`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a14-1-acceptance`

Primary evidence: `live-summary.json`, `native-evidence.json`, per-attempt
`routing-evidence.json`, `primary-evidence.json`, `primary-result.json`,
`runtime.json`, `cleanup.json`, plus `focused-compat.xml`, `regression.xml`,
`regression-summary.json`, `suite-manifest.json` and `frozen-audit.json`.
