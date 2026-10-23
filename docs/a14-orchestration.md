# A14: bounded multi-agent orchestration

Status: implementation present; real multi-agent/native graph acceptance remains open.
See `a14-acceptance.md` for the initial measurements and
`a14-1-acceptance.md` for the subsequent live acceptance and
`a14-2-acceptance.md` for inference-boundary diagnosis and
`a14-3-acceptance.md` for provider capacity recovery and
`a14-4-acceptance.md` for real graph admission/concurrency and current closure.

## Entry and authority

The existing user path remains text/voice → RequestLifecycle → AgentHarness →
AgentTurn → canonical capability selection. The catalog adds `agent.orchestrate`
as a hub capability with no executable arguments. The model can select it when a
request benefits from independent work. Simple requests retain the existing
single AgentTurn and incur no extra classifier or planning call.

A14 supplies the orchestration capability's complete admission contract at the
existing inference boundary. This is model guidance, not request matching or a
substituted decision. A14.2 places it after the frozen general instructions and
adjacent to CAPABILITIES, preserving both CAPABILITIES/STATE JSON boundaries.
It explains hub execution, whole-request coverage and the difference between
delegation and one constituent action. Actual model output, provider selection, privacy
recipient identity and failure handling remain authoritative and unchanged.
Delegated scope instructions use the same safe placement rather than following
the trailing STATE JSON. Graph/worker schema annotations are compacted without
removing validation constraints. Complete JSON objects, optionally in an explicit
fenced block, still pass strict protocol and scope validation; no graph is filled
in or repaired locally. The one existing planner replan receives bounded field
locations and validation codes without model inputs or exception bodies.

Dispatch obtains the original request from the owned, active parent AgentTurn.
It never accepts a client-supplied graph, worker authority, credential or provider
override. A graph ID is derived from that parent; duplicate submissions return
the recorded outcome or an explicit unverified/no-replay result. A delegated
AgentTurn cannot call `agent.orchestrate`.
Legacy goal/task/routine management and spawning controls are excluded from
delegated scope as well; they cannot bypass graph bounds through another engine.
New registry capabilities marked `privacy_metadata.delegates_work` are rejected.

The coordinator is asynchronous Python inside the existing Hub. Workers are
logical, isolated inference/task contexts, not arbitrary Python subprocesses or
generated executable agents. Coding workers can analyze code; execution still
requires a declared registered capability and existing authorization.

A14.3 uses the existing sourced model-profile configuration for context capacities.
Native schema text is not duplicated in system prompts when already passed as
function parameters. The A14 native transport requests a function proposal
explicitly while retaining all original safety instructions and JSON boundaries.
Planner catalogs losslessly intern repeated argument definitions and target sets;
delegated workers see only their declared catalog scope. Neither optimization
changes the canonical registry or dispatch authority. Oversize input is rejected
explicitly, without silently truncating user requests or verification criteria.

## Protocol

`hub/orchestration_protocol.py` defines strict version-1 Pydantic messages.
`shared/orchestration/v1.schema.json` is its JSON schema for independent clients.

A graph contains 2–8 nodes. Each node declares:

- ID and configured role name.
- Objective, required/optional status, and success criteria.
- Dependencies, expected primitive output type (text, number, boolean).
- Kind: reasoning or capability; capability nodes declare their exact tools.
- Optional target device and up to four explicit memory keys.
- A bounded node deadline.

Duplicate IDs/dependencies, unknown dependencies, cycles, executable extensions,
undeclared roles and capability-free execution nodes are rejected. Reasoning
nodes cannot acquire tools or an execution target. Capabilities and devices must
be eligible at planning, and executable workers revalidate them at dispatch.
Capability output is a natural-language summary backed by execution receipts.

Worker results contain a typed value, optional keyed claims and limitations.
Synthesis includes contributing node IDs. Verification independently checks the
original request, graph criteria, actual results and proposed answer. Neither
synthesis nor verification can execute an action or create another worker.

## Scheduling and models

There are at most two active graphs and three scheduled workers per graph.
Independent reasoning runs concurrently; dependent nodes wait for successful
dependencies. Failed dependencies block downstream work. Optional failures are
passed to synthesis and remain in the recorded outcome.

Capability workers use the full frozen AgentTurn implementation through a
scope-restricting adapter. A reference-counted per-device lock serializes entire
execution workers, including observation/action/verification, so sibling UI work
does not race. Lock and task handles are removed on completion or cancellation.

Role definitions contain a name, purpose and A10.5 task category. Defaults cover
research, reasoning, coding, execution, verification and synthesis. An optional
local `CORTEX_WORKER_ROLES_PATH` JSON file can replace these bounded definitions;
it cannot add permissions or choose a provider. For example:

```json
[
  {"name":"analysis","purpose":"Analyze supplied evidence.","category":"reasoning"},
  {"name":"execution","purpose":"Execute declared capabilities and inspect receipts.","category":"computer_use"}
]
```

The existing A10.5 selector chooses each inference recipient from the role/task
profile and current provider health, privacy, cooldown and quota conditions.
Provider selection and failover policy are unchanged. Provider/model metadata is
recorded as provenance, including the selected route after failover. Each graph
has a shared budget of 32 inference turns, a 120-second total deadline, node
deadlines of at most 120 seconds, and at most one invalid-plan replan before any
worker starts. The graph budget stays inside the frozen parent's 130-second
fencing lease. The existing router's bounded internal attempts still apply.

## Scoped data and permissions

Workers receive their own objective and only their explicit successful
dependencies. Full session history, arbitrary memory, credentials and approval
objects are excluded. ContextManager's already-retrieved relevant memory can be
used only by explicit keys present in that request's context; unrelated rows are
withheld. The planner receives no memory values. Selected memory raises the
privacy floor to at least PERSONAL, respecting stronger row annotations.

Each sharing step uses the existing AgentTurn privacy/approval mechanism for its
actual selected recipient. Approval from another worker or provider is not
inherited. Dependency privacy propagates upward; roles and model output cannot
lower it. Payloads are bounded to 24 KiB, with credential fields and configured
secrets redacted. Capability planning receives signatures rather than unrelated
account identities or stored account defaults.

DelegatedControl intersects the existing control policy with fresh enrolled
requester RBAC and per-device overrides. This prevents an existing hub-internal
control shortcut from being mistaken for worker authority. Execution otherwise
uses existing policy gates, human approval, kill switch, resource affinity,
leases/fencing, audit, integrations and A13 fresh advertisements. Workers cannot
approve themselves or substitute another device. Capability scope is checked
even when a model proposes a different tool than its declared scope.

Private graph results remain in the source-owned local Workspace. Their contents
are withheld from the parent AgentTurn's separate response interpreter, which
receives only a metadata receipt. A worker's sharing approval never silently
authorizes that parent recipient.

## Grounding and recovery

Capability-node success requires the full AgentTurn to succeed and every
declared capability to have a successful real receipt. A model saying an action
completed cannot replace a missing receipt. Claims with conflicting keyed values
block successful synthesis. Required failed/incomplete nodes, unknown citations,
or a verifier reporting unsupported conclusions prevent overall success.

Verification of model analysis is an inference check, not a formal proof of
external factual correctness. Execution evidence is labelled separately.

Before a side effect, the coordinator durably claims a fingerprint of target,
capability and exact arguments. A second identical action in the graph is
rejected, including after ambiguous failure; the graph permits at most 24 such
claims. The frozen AgentTurn retains its own UI postcondition/recovery rules.

The existing tasks database stores the graph, bounded lifecycle events, plan,
per-node state/output/receipts, verification and final result. Separate worker
task rows isolate concurrent provider audit and approval updates. Cancellation
awaits child cleanup; cancelling an active subtask stops the enclosing graph.
The normal HTTP/native and control-WebSocket cancellation paths reach A14.

On Hub restart, completed node results and action claims remain. Incomplete nodes
become UNVERIFIED and the graph ends truthfully as failed/unverified. Incomplete
work is never automatically replayed. There is no automatic side-effect resume.

## Native workspace

The existing A12 typed protocol and PyQt/React renderer are reused unchanged.
The owned task family projects a subtask table (role, dependencies, state, result,
receipt evidence), progress, a 32-event worker timeline and unified result card.
Awaiting approval is projected from the delegated worker task. Task actions remain
the existing authenticated cancel control. All text remains declarative escaped
content; workers cannot emit HTML, JavaScript, commands or new action types.

Workspace histories retain the existing device isolation and revision behavior.
Projection reserves discovery slots within the 128-component protocol bound.
Voice continues to use the existing request/session lifecycle; A14 live audio
acceptance has not been claimed.

## Validation and limits

Run `tests/test_a14_orchestration.py` serially with other database tests. The
normal live runner is `scripts/live_orchestration_acceptance.py --out <folder>`.
It uses real configured providers, normal local state, ASUS credentials and the
actual native application. Its public probes request reasoning and bounded
`ui.wait` only. It injects neither model decisions nor provider responses.

Real graph admission and overlapping independent worker lifecycles were observed
in A14.4. Successful worker reasoning, successful dependent execution, ASUS
receipts, native populated graph inspection and verified synthesis still need a
healthy provider acceptance run. Scoped context deliberately omits full conversational
history; unresolved references require clarification or explicit relevant memory
keys rather than unrestricted history sharing. Automatic conflict reconciliation
and post-execution graph replanning are conservative: conflicting or incomplete
work is surfaced instead of replayed.

Preserved checkpoints:

```text
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A12_FOUNDATION = COMPLETE
A13 = COMPLETE
A12_2_GENERATIVE_WORKSPACE = DEFERRED
A14 = IN_PROGRESS
```
