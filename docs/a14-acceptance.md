# A14 implementation and acceptance — 2026-10-22

## Closure decision

```text
A14_IMPLEMENTATION = IMPLEMENTED_BOUNDED_FOUNDATION
A14_DETERMINISTIC_ACCEPTANCE = PASS
A14_REAL_MULTI_AGENT_ACCEPTANCE = BLOCKED
A14_NATIVE_GRAPH_ACCEPTANCE = NOT_REACHED
A14_COMPLETE = NO
A14 = IN_PROGRESS
```

The real normal-runtime requests reached AgentTurn and real providers, but neither
produced an accepted orchestration graph. No real concurrent workers, delegated
capability receipt, dependency execution or unified graph answer is claimed.
Native launch, request progress, truthful failure and native cancellation were
observed separately. This is not a full A14 desktop acceptance pass.

## Architecture and files

See [a14-orchestration.md](a14-orchestration.md) for the design, protocol, limits,
configuration, authorization, privacy, recovery and native projection contracts.
The entry is a new canonical `agent.orchestrate` capability selected by the
existing model-driven AgentTurn. A10.5 selects inference recipients; A14 schedules
bounded workers; executable workers reuse the frozen AgentTurn.

Added:

- `hub/orchestration_protocol.py`: strict graphs, configurable role descriptions,
  typed results, synthesis and verification messages.
- `hub/orchestration.py`: admission, planning, scope validation, bounded DAG
  scheduling, context propagation, checkpoints, deduplication and recovery.
- `hub/orchestration_workers.py`: frozen-router adapters, inference budgets,
  scoped AgentTurn workers and fresh delegated requester authorization.
- `shared/orchestration/v1.schema.json`: exported graph protocol.
- `scripts/live_orchestration_acceptance.py`: bounded normal-runtime acceptance
  runner and real authenticated HTTP rejection probes.
- `tests/test_a14_orchestration.py`: 44 deterministic behavioral/security checks.
- This report and `docs/a14-orchestration.md`.

Modified for A14:

- `hub/capability_registry.py` and `hub/permissions.py`: canonical entry/tier.
- `hub/pipeline/execution_dispatcher.py`: normal authorized dispatch into A14.
- `hub/task_orchestrator.py`: conservative graph/worker restart reconciliation.
- `hub/api_control.py`: existing HTTP/WebSocket cancellation reaches graphs and
  active subtasks, preserving existing authenticated control surfaces.
- `hub/workspace.py`: existing typed task family projection includes workers,
  dependencies, receipts, progress, lifecycle and unified outcome within bounds.

The repository also contains existing uncommitted A11/A12/A13 work; those files
were preserved. No commit/push or unrelated cleanup was performed.

## Exact automated evidence

- Focused A14: **44 passed, 0 failed**.
- Final consolidated regression: **894 passed, 0 failed, 0 skipped, 0 xfailed**
  in **159.35 seconds**. All **850 baseline identities retained**, **44 added**;
  no missing baseline checks. Two existing dependency deprecation warnings.
- Existing frontend protocol: **7 passed, 0 failed**; also exercised by the
  retained A12 Python regression test. These are not added again to the Python
  consolidated total.
- Frozen A10/A10.5 evidence: **11 hashes checked, 0 mismatches**.

The first restricted run produced 876 passes and two existing TLS file-transfer
failures. A rerun with the required local filesystem/TLS access passed all 888
then-collected checks. Subsequent A14 privacy/scope/security additions received
focused verification and a new complete serial regression. No existing tests
were removed, skipped or weakened. Baseline identity retention is measured from
the previous 850-test XML, not inferred from a larger total.

One composed deterministic acceptance test traverses the production pipeline,
parent AgentTurn, model graph, concurrent worker reasoning, scoped AgentTurn,
executor receipt, verification, unified answer and Workspace projection. Only
model responses and the harmless OS endpoint are fixtures in that test; it is
not live provider/native evidence.

Focused coverage includes DAG rejection/order/overlap/concurrency bounds,
capability serialization, dependencies and scoped memory, existing-router role
hints, privacy refusal before inference, credential removal, worker failures,
deadlines, parent/subtask cancellation, restart unverified state, durable
side-effect claims, repeated submission, RBAC/per-device revocation, missing
advertisement, human approval enforcement, malicious structured output,
fabricated completion without receipts, conflicts, verifier refusal, optional
failure disclosure, workspace projection and protocol bounds.

## Real Hub / ASUS / provider evidence

The acceptance runner launched the normal Hub on localhost:8000, the ASUS Agent
with its existing legitimate credential, and the actual PyQt/QWebEngine native
application. The production task database was used for these new bounded
acceptance requests. Voice providers were mocked to avoid paid audio; inference
responses and model plans were never mocked or injected.

The primary public request asked for independent programming-study analyses, a
dependent comparison, and a one-second ASUS wait. Its identity was:

```text
REQUEST_TASK = request_b289652b96962d30fad50397
AGENT_TURN = turn_36462f0baea049e2809ee0a6aa78a80a
ACCEPTED_ORCHESTRATION_GRAPHS = 0
```

Real structured routing events show:

| Provider/model | Observed result | Classification |
|---|---|---|
| Groq / openai/gpt-oss-120b | HTTP 400, `tool_use_failed` | Unsupported tool-use / invalid structured output |
| Groq / qwen/qwen3.8-27b | HTTP 429 | Rate limit |
| Gemini / gemini-3.8-flash | HTTP 503 | Service unavailable |
| Groq / qwen/qwen3.8-27b | HTTP 200 on a bounded retry | AgentTurn rejected an invalid success-condition proposal before dispatch |
| Groq / qwen/qwen3.8-27b | HTTP 413, provider code `rate_limit_exceeded` | Existing frozen router recorded `CONTEXT_TOO_LARGE`; the HTTP status/code alone do not prove the model's true context capacity |

The final primary result was `model_provider_unavailable`, primary category
`RATE_LIMIT_429`, fallback category `CONTEXT_TOO_LARGE`, fallback status 413.
This is a mixture of provider protocol/output reliability and capacity/outage
failures, not proof that every model call was unavailable. Two bounded real
requests were used (primary and cancellation); repeated open-ended retries and
changes to the frozen router were avoided.

## Native Computer Use evidence

Computer Use selected the unique returned native `Cortex Control Center` window
(Python/QWebEngine, returned window ID 67946). It opened the actual Workspace
navigation, observed Hub connected / ASUS online / Workspace connected, a
RUNNING real request and indeterminate progress. The primary provider failure
then appeared as FAILED with the real usage-limit/backup-failure message.

A separate harmless cancellation request used these identities:

```text
REQUEST_TASK = request_9bb12c54e839012d571693a8
AGENT_TURN = turn_8470ce73222e4764b85d9c15fc94aa71
```

Computer Use clicked that request's actual native Cancel task button. The Hub
recorded `POST /api/workspace/action` HTTP 200. Both request and AgentTurn became
CANCELLED in durable state, and the native Workspace displayed CANCELLED.
The final message explicitly preserved uncertainty about already dispatched
actions. No orchestration graph had been accepted for this request, so this
proves native request cancellation, not cancellation of real parallel workers.

The screenshots were observed directly through Computer Use; no scripted browser
substitute or synthetic completed graph was used. Graph/subtask tables, worker
timelines, concurrent live progress, native approval display within a graph,
restart recovery of an actual multi-agent task and verified unified output
remain unverified live. Their deterministic checks are separate evidence.

All owned Hub/Agent/native acceptance processes were stopped; `cleanup.json`
records `all_owned_processes_stopped: true`.

## Security findings

The new execution adapter rejects missing/expired/incompatible A13 advertisement
evidence instead of using the legacy no-advertisement compatibility allowance.
All actual execution still traverses the existing permission, policy, approval,
ownership/fencing and dispatch infrastructure.

Delegation additionally intersects legacy control decisions with fresh requester
RBAC and per-device overrides. This closes a new-path escalation risk where some
hub-internal controls treat an internal caller as allowed. Frozen control/router
implementations were not rewritten.

The graph deadline is bounded to 120 seconds inside the frozen parent's
130-second fencing lease. Legacy goal/task/routine spawning and management
controls are excluded from worker scope, preventing delegation through another
engine from bypassing worker limits. The existing goal wait primitive remains
available. Both guards are included in focused verification.

Independent worker inference does not inherit another recipient's approval.
Private results are withheld from the parent interpreter and retained in the
source-owned Workspace. Credentials and unrelated memory/account metadata are
excluded or redacted. Model graph/results are strictly typed data; UI messages
cannot create executable controls or approve themselves.

Live HTTP rejection checks passed:

| Probe | Status |
|---|---:|
| Forged requesting-device identity | 403 |
| Executable/delegating workspace action payload | 422 |
| Unowned task control | 403 |
| Invalid device credential | 401 |

These used no new identity, permission change, inference, OAuth scope, external
write or OS modification. Role/worker privilege and approval tests were
deterministic; no live malicious model delegation was accepted for testing.

## Remaining limitations and next acceptance

The next bounded acceptance must obtain a valid model-selected orchestration
entry and real graph, observe at least two concurrent independent workers and
ordered dependent work, collect a legitimate harmless executor receipt, verify
synthesis, and inspect those actual worker components in the native app. It must
also cancel/fail an active graph and verify conservative restart recovery.

Full session-history sharing, unrestricted memory, recursive delegation,
automatic conflict resolution and automatic replay of incomplete actions are
outside this bounded implementation. Named relevant memory keys and explicit
dependency outputs are supported. Model verification remains fallible; real
capability receipts are required for action claims.

```text
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A12_FOUNDATION = COMPLETE
A13 = COMPLETE
A12_2_GENERATIVE_WORKSPACE = DEFERRED
A14 = IN_PROGRESS
CORTEX_GOAL_METER = 82% (unchanged)
```

The meter does not advance merely for added code or offline checks while the A14
real execution/native acceptance gate remains open. No later phase was begun.

Artifact root:
`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a14-orchestration`

Key files: `focused.xml`, `regression.xml`, `regression-summary.json`,
`suite-manifest.json`, `frozen-audit.json`, `live/primary-evidence.json`,
`live/primary-result.json`, `live/cancellation-evidence.json`,
`live/cancellation-result.json`, `live/provider-events.json`,
`live/live-security.json`, `live/runtime.json`, `live/cleanup.json`.
`native-evidence.json` records the Computer Use observations and explicit
unverified graph checks; `acceptance-summary.json` records the final gate decision.
