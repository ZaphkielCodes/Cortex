# A14.4 live graph execution acceptance — 2026-10-22

## Closure

```ini
MODEL_SELECTED_ORCHESTRATION = YES
VALID_REAL_TASKGRAPH_ACCEPTED = YES
REAL_GRAPH_NODE_COUNT = 5
INDEPENDENT_WORKER_CONCURRENCY = VERIFIED
COMPLETE_LIVE_GRAPH = NO
LIVE_ACCEPTANCE = BLOCKED_PROVIDER_CAPACITY
A14_COMPLETE = NO
A14 = IN_PROGRESS
```

The single normal-runtime request accepted a real model-generated graph and
started real workers. Provider failures prevented worker results, ASUS execution
and synthesis. No graph or model output was injected, and no second inference
request was submitted after the bounded attempt. Production code was not changed
for quota failures or to manufacture a pass.

## Normal-runtime planner and graph evidence

Preflight retained all **11 frozen hashes**, loaded the existing verified model
profiles, and found eligible Groq GPT-OSS, Groq Qwen and Gemini routes. NVIDIA
remained ineligible for INTERNAL context under existing trial terms. No cooldown
or quota record was cleared. The previous catalog reconstructed at 13,607 bytes;
the **actual normal-runtime TaskGraph input was 13,777 bytes**, below the unchanged
**24,576-byte** bound. Native TaskGraph schema size was 1,514 bytes.

```text
REQUEST_TASK_ID = request_6251289df757ee7f8076a1c8
PARENT_AGENT_TURN_ID = turn_02d1e49964744c47bbf3b2cbc19bb441
GRAPH_TASK_ID = graph_b0966c5e6d920a505a2b4502
PLANNER_PROVIDER = groq
PLANNER_MODEL = qwen/qwen3.8-27b
GRAPH_ACCEPTED_AT_UTC = 2026-10-23T01:02:46.075627+00:00
FINAL_REQUEST_STATUS = FAILED
```

The initial real GPT-OSS AgentDecision selected `agent.orchestrate`. The planner
returned the following graph through existing bounded provider failover. Strict
TaskGraph and eligible-scope validation passed before workers were created. The
persisted original plan also passed strict protocol revalidation during evidence
collection. That revalidation did not execute or replace the graph.

| Worker | Kind | Dependencies | Final state |
|---|---|---|---|
| `spaced_retrieval` | reasoning / research | none | FAILED |
| `worked_examples` | reasoning / research | none | FAILED |
| `asus_wait` | capability / execution, declared `ui.wait` | none | FAILED |
| `compare_tradeoffs` | reasoning | both research workers | BLOCKED |
| `final_recommendation` | reasoning / synthesis | comparison and ASUS wait | BLOCKED |

## Concurrency, dependencies and grounding

The two independent workers began at:

```text
spaced_retrieval: 2026-10-23T01:02:46.082194+00:00
worked_examples:  2026-10-23T01:02:46.082470+00:00
```

Their persisted RUNNING-to-FAILED intervals overlapped by **5.960567 seconds**.
Both workers issued actual provider attempts; the provider rejected those calls.
This proves concurrent worker lifecycles and attempted inference, not successful
parallel model analyses. The ASUS execution worker also started independently.

Both dependent workers remained BLOCKED with no `started_at`. This verifies that
failed prerequisites prevent downstream execution. Successful-prerequisite
dependency ordering remains **NOT_REACHED LIVE**, since no research output
succeeded.

**No `ui.wait` was dispatched to ASUS and no physical executor receipt exists.**
The failed `agent.orchestrate` hub snapshot is not an ASUS success receipt.
Synthesis and verification were not called. The recorded final response named
the incomplete required work and reported failure; it did not fabricate a
recommendation or claim successful execution.

## Provider failures and bounded stop

The one request made **8 real provider attempts** across initial decision,
planning and workers, within the existing engine's bounded failover policy.
Two attempts succeeded: the initial decision and the valid graph proposal.
The failures were:

| Provider/model | HTTP | Recorded classification | Evidence |
|---|---:|---|---|
| Groq GPT-OSS | 429 | RATE_LIMIT_429 | TPM: limit 8,000, used 6,057, requested 4,287; remaining 1,943; Retry-After 18s |
| Gemini Flash | 503 | SERVICE_UNAVAILABLE_503 | Real planner alternate unavailable |
| Groq GPT-OSS | 400 | INVALID_OUTPUT / tool_use_failed | Native generation rejected; sanitized generation shape retained, no generated text |
| Groq Qwen | 429, three attempts across workers | RATE_LIMIT_429 | Numeric Limit 1,000, but unit/scope not captured; not labelled TPM, RPD or daily exhaustion without evidence |

The Qwen responses also carried the separate token header limit 8,000. A header
and a different error-body number are not treated as the same restriction.
The worker failures are external admission/availability or model-output failures;
no demonstrated scheduler, graph-schema or dispatch defect required a production
repair in this phase. HTTP 400 is not taken as proof that the model lacks tool
support in general. No quota waits, retries or alternate eligibility were widened.

No repeated acceptance, manually supplied graph, paid route, additional credential,
OAuth scope or privacy override was used. The existing public acceptance prompt
was unchanged. Voice was mocked; the text/AgentTurn/provider/task path was real.
All acceptance-owned processes were stopped.

## Native Workspace evidence and limitation

Computer Use located the actual Python-owned `Cortex Control Center`, window
**42404468**, and observed its native Overview. Opening Workspace failed with:

```text
SendInput sent 0 of 1 events; GetLastError=87
```

After refreshing, the accessibility tree still identified Cortex but the returned
screenshot showed a different foreground application. Those coordinates were not
reused. No browser/DOM substitute was used to claim native acceptance. The current
five-node graph's native rendering and live subtask progress remain **UNVERIFIED**.
The prior A14.3 native failure-layout evidence does not close this run's gate.

## Cancellation, truthful failure and restart

Failure propagation is proven on the real graph: unsuccessful prerequisites
blocked dependents and the parent ended FAILED without verified synthesis.

The authenticated Workspace API rejected a stale cancellation of the terminal
request with **HTTP 409**. The entire owned task family remained unchanged.
This verifies a terminal-state guard, **not active graph cancellation**. No new
model-driven cancellation task was submitted after capacity failure.

The original acceptance runtime was stopped. The normal Hub and legitimate ASUS
Agent were then restarted, with fresh authenticated capability advertisement and
no new task submission. The same real graph and worker IDs, failed status,
node evidence, result and completed-step records remained unchanged. The restart
made **0 model attempts** and created no replacement worker tasks. Both restart
processes were cleaned up.

This proves durability/no replay for the **already-terminal failed graph**. It
does not prove interrupted/incomplete-node recovery to UNVERIFIED. That path,
active graph cancellation, and replay protection following an actual ASUS side
effect remain **UNVERIFIED LIVE**. Deterministic tests are kept separate.

## Regression, files and remaining gates

Focused provider/router/A14/recovery checks: **183 passed, 0 failed, 0 skipped**,
**21.90 seconds**. These include the existing cancellation, incomplete restart,
permission and no-replay behavioral checks with controlled providers/executors.

Final full serial regression: **935 passed / 0 failed / 0 skipped / 0 xfailed**, **185.267 seconds**. All **935 baseline checks** retained, **0 missing**; no assertions were changed.

No production or test source was edited in A14.4. Changes are this report and the
architecture-documentation pointer; acceptance scripts/evidence are in the chat's
artifact workspace. Existing uncommitted A11–A14 work was retained. All **11
frozen-file hashes match**; OAuth scopes, approvals, privacy and frozen A10/A10.5
routing/retry/execution invariants remain intact.

Remaining live gates: successful worker outputs; successful dependent execution;
real ASUS receipt; grounded synthesis and verification; native populated graph
inspection; active cancellation; interrupted graph recovery and side-effect
no-replay. Provider capacity must support those bounded steps without bypassing
the current policy. No claim of A14 closure is made from regression results.

Evidence root:
`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a14-4-acceptance`

Key evidence: `live-summary.json`, `graph-structure.json`,
`live/routing-evidence.json`, `provider-failure-metadata.json`, `wire-measurements.json`,
`native-evidence.json`, `terminal-cancel.json`, `before-restart.json`,
`after-restart.json`, `restart-result.json`, both cleanup records, focused/full
regression XML and frozen audit.

```ini
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A12_FOUNDATION = COMPLETE
A13 = COMPLETE
A12_2_GENERATIVE_WORKSPACE = DEFERRED
A14 = IN_PROGRESS
A10_A10_5 = FROZEN
```
