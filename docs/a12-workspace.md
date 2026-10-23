# A12 dynamic workspace foundation

October 21, 2026. Scope: native desktop presentation and bounded interaction over existing task execution. No A13/A14 implementation.

Update October 22, 2026: [A12.1 native acceptance](a12-1-native-acceptance.md) completed the minimum native desktop gate and repaired incremental ordering and terminal progress presentation. The interrupted acceptance described below is the original A12 record; consult A12.1 for current acceptance status and exact results. A11 and pre-A11 validation debt remain unchanged.

## Status

| Area | Status |
| --- | --- |
| Typed protocol, durable-state projection, incremental sync | IMPLEMENTED / AUTOMATED-TESTED |
| Native React renderer and WebChannel transport | IMPLEMENTED / BUILT |
| Native PyQt6/WebEngine application launch | DESKTOP-VALIDATED |
| Real provider/native-agent task projection and authorized cancellation | LIVE BACKEND-VALIDATED |
| Clicking through workspace task lifecycle in the native window | DEFERRED: user stopped Computer Use with physical Escape |
| New microphone/STT/TTS behavior | NOT ADDED; existing voice state/IPC reused only |
| Android renderer | DEFERRED; protocol is platform-independent |

**A12 foundation implemented; full desktop acceptance remains incomplete.** No visible task completion or native Cancel-button click is claimed. The live cancellation described below was an authenticated backend call, not a physical UI action.

Preserved validation debt:

```text
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A13_A14_IMPLEMENTATION = NOT_STARTED
```

## Architecture

The native UI remains React/Vite inside PyQt6 `QWebEngineView`, connected to the existing Python `DesktopControlTransport` by Qt WebChannel. No new framework, renderer process architecture, inference router, execution loop, OAuth scope, or approval policy was introduced. The legacy administrative browser dashboard was not replaced or modified by A12.

The vertical path is:

```text
typed/voice request through existing infrastructure
→ existing RequestLifecycle / AgentTurn
→ durable task state, execution receipts and operational traces
→ device-scoped WorkspaceStore projection
→ versioned snapshot/delta envelope
→ existing credential-isolated desktop transport
→ reusable native workspace components
→ explicit user Cancel control
→ authenticated workspace action endpoint
→ existing Control Center task cancellation path
→ authoritative task state projected back to the renderer
```

AgentTurn and A10/A10.5 routing/recovery code remain unchanged. Presentation is derived from recorded facts rather than interpreting user phrases or trusting provider-generated UI. The workspace does not assert that an action succeeded merely because a request or cancellation acknowledgement was received.

## Protocol v1

Source: `hub/workspace.py`. Exported JSON Schema: `shared/workspace/v1.schema.json` (draft 2020-12). The schema includes envelopes, sync cursors and action requests, and contains no Windows-specific UI types.

Envelope fields:

| Field | Meaning |
| --- | --- |
| protocol_version | Exactly `1`; unsupported versions are rejected |
| epoch | Server instance identity; changes after restart |
| revision | Monotonic view revision within an epoch |
| base_revision | Required matching base for a delta |
| mode | `snapshot` replaces the view; `delta` upserts/removes stable IDs |
| generated_at | Generation timestamp, not a task-completion timestamp |
| items | Discriminated, typed declarative components |
| removed | Component IDs to remove |

Supported components:

- `card`: recorded response text.
- `task`: real task identity, status, details and server-issued Cancel affordance.
- `progress`: actual status; indeterminate when no total is known. Only a recorded completed task receives value 1. Active tasks are not assigned invented percentages.
- `timeline`: recorded execution stages, capability names/reasons and timestamps. If no trace exists, it shows the observed durable state instead of inventing past transitions.
- `table`: bounded capability/outcome/executor receipt rows. No receipts renders an explicit empty state.
- `status`: recorded current activity or waiting reason.
- `error`: truthful unsuccessful task outcome.

Every item has stable ID, title, and provenance identifying the durable task/source and recorded update timestamp. Provider payloads, capability arguments, hidden reasoning, token objects and full task contexts are excluded. Text from requests/results is bounded and scrubbed, and is treated as text even if it contains HTML-like markup.

Example sync request:

```http
POST /api/workspace/sync
Content-Type: application/json
X-Cortex-Device: <enrolled device>
X-Cortex-Secret: <handled only by native Python transport>

{"epoch":null,"revision":0}
```

Subsequent syncs send the returned epoch/revision. A lagging client or a server restart receives a complete snapshot. Exact-current cursors receive an empty delta. Component IDs are reconciled in a Map, preventing duplication; stale/out-of-order deltas trigger a snapshot refresh rather than corrupting state.

The renderer supports loading, empty, error and disconnected states. On disconnect it retains the last view with an explicit stale warning and disables actions. It never converts a failed fetch into an empty successful workspace.

## Authenticated interactions

`POST /api/workspace/action` accepts only this typed shape:

```json
{"kind":"cancel_task","task_id":"request_example","action_id":"<32 hex characters>"}
```

No URL, script, shell command, capability name, arbitrary argument dictionary or approval decision is accepted. Extra fields are rejected. Controls are generated by the backend from task state, not by model text.

The endpoint reuses existing enrollment/session authentication and administrator-role requirements. It independently checks that the task belongs to the authenticated source device, is not terminal, and matches the current action-state fingerprint. A descriptor is an affordance, not a credential or authorization grant. Guessed/cross-device/stale descriptors cannot bypass the checks.

Authorized cancellation uses the existing `stop_task_endpoint`, RequestLifecycle and AgentTurn cancellation mechanisms. The client then fetches authoritative state. No automatic action retry, side-effect replay, general execution endpoint, or new approval bypass was added. Existing approvals remain in the Security surface; waiting tasks can be displayed or cancelled here without granting their approval.

## Native UI and conversation integration

`DynamicWorkspace.jsx` renders the declarative items with React text nodes; there is no HTML interpreter, expression evaluator, injected code, or model-generated click handler. The existing desktop sidebar now includes Workspace. Sending a request from Overview opens Workspace, and Workspace also has a request input using the existing `/api/control/message` flow.

Existing task and capability records are projected directly, including parent request/child AgentTurn correlation. A result card is labeled recorded response, and task status remains separate from the prose. This preserves the distinction between model text and verified execution state.

Task details, timelines, progress, receipt tables, provenance, current activity and unsuccessful outcomes share reusable rendering components. Changes update stable React keys incrementally rather than replacing the application.

## Voice and multi-device boundaries

The workspace receives the existing voice-state signal and local IPC connection state. It reports the connected agent's state or that the agent is disconnected. Existing push-to-talk/mute controls are retained elsewhere; A12 does not implement or claim a new microphone session, voice transcript stream, audio output, speech recognition, or wake-word path.

Protocol JSON uses task IDs, enums, text, tables, timestamps and bounded action descriptors, with no QWidget/Windows handles or executable UI payloads. Android can consume it later through an authenticated client; no Android UI implementation or new device trust role was added. Each workspace is scoped to its authenticated device rather than exposing all administrator-visible task data.

## Bounds and lifecycle cleanup

- Database reads select at most 100 tasks for the authenticated device.
- At most 20 root tasks are presented; correlated child tasks supply real receipts/traces without duplicate root cards.
- Envelope hard limit: 128 components/removals; current projection is at most 120 components.
- Titles: 240 characters; text/cells: 2,000 characters; timeline: 32 entries; table: 20 rows and 6 columns.
- Server cache: at most 64 device views and one previous revision per view; old devices are evicted.
- Renderer sync: one in-flight request, polling every two seconds while mounted. Cleanup stops timers and ignores late responses; no private workspace history is stored in browser storage.
- Actions have an independent busy guard and are never assumed successful after a transport timeout.
- HTTP work continues through the existing background native transport pool, keeping model calls off the rendering thread.

## Verification results

| Automated gate | Result |
| --- | --- |
| A12 + desktop transport focused checks | 27 passed / 0 failed |
| Consolidated regression | **823 passed / 0 failed / 0 skipped / 0 xfailed** |
| Baseline retention | All 814 previous cases retained; 9 A12 cases added |
| Workspace protocol JavaScript checks | 5 passed (also exercised by one pytest wrapper; counts are not additive unique cases) |
| Native frontend production build | Passed, 26 modules |

The initial new cancellation assertion expected uppercase database state for every task type. Existing cancellation stores lowercase state for ordinary request rows. The assertion now normalizes this existing contract; production cancellation behavior was not changed. No pre-existing tests were weakened, removed, skipped or marked xfail.

The frontend retains an existing bundle-size warning (about 841 KB uncompressed); no unrelated bundler/framework refactor was performed. The existing external-network pytest guard stayed enabled.

Coverage includes snapshot/delta/no-op/restart behavior, role/authentication/device isolation, stale/forged/executable controls, real AgentTurn cancellation with a simulated model/executor boundary, truthful failure/unknown progress, untrusted text and secret canaries, bounded traces, schema rejection, client revision handling and action-payload restrictions.

## Real acceptance evidence

Artifacts:

`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a12-workspace-20261021`

### Native launch

A disposable local Hub and actual Windows agent authenticated as `a12-windows`. The production-built native PyQt application launched against that Hub with auto-start disabled. Windows accessibility observed the `Cortex Control Center` window, the bundled `Cortex Command Center` WebEngine document, the Workspace navigation button and connected device state. See `desktop-launch-evidence.json` and `desktop-live/runtime-info.json`.

Computer Use was stopped by the user with physical Escape before a Workspace-button interaction could be verified. No further Computer Use input was sent. The first screenshot capture showed an occluding browser rather than useful Cortex visual evidence; it is not offered as a workspace screenshot. Native launch evidence is the returned Cortex accessibility state, not a claim of a complete visual walkthrough.

### Real backend lifecycle and interaction

A separate bounded test used a real inference provider and actual native Windows agent, with no scripted model responses and no desktop input. It submitted a wait/CPU request through the normal `/api/control/message` path. Workspace sync observed `RUNNING`; an authenticated request using the returned Cancel descriptor received HTTP 200; subsequent sync observed `CANCELLED` and a recorded cancellation response.

Task: `request_2f2344afd5ea825e48781f52`. Presented component kinds: card, progress, table, task and timeline. No capability receipt rows were recorded before cancellation; this is not claimed as a successful telemetry execution or native receipt-table walkthrough. See `backend-live/report.json`.

### Explicitly unverified

- Clicking Cancel in the native renderer and watching a task transition in that same desktop view.
- A populated capability receipt table observed in the native window.
- Full native refresh/restart/disconnect walkthrough. Automated protocol/authorization tests cover recovery behavior, but they do not substitute for physical proof.
- Live audio input/output and Android rendering.

## Security findings

The new surface adds no arbitrary execution path. Strict backend models, native endpoint allowlisting, reauthentication, device ownership checks, stale-state checks and existing cancellation policy remain authoritative. Model/provider text cannot supply executable markup or action destinations. Known secret patterns, credential assignments and configured secret values are redacted before projection. This is defensive presentation filtering, not a general-purpose secrets viewer or a universal data-loss-prevention claim.

Only current source-device tasks are selected. Capability receipts omit arguments and raw result bodies. No OAuth scopes, integration permissions, kill-switch rules or model recipient policy were expanded. Native secrets remain in Python; the workspace JavaScript receives presentation data and narrow task descriptors only.

Frozen A10/A10.5 files are hash-checked separately. The Hub entry point changed only to register the new router; AgentTurn, provider selection/failover, compound orchestration/recovery, approval TTL and historical acceptance prompts were not changed. The existing native WebEngine/bridge architecture remains trusted application code; this foundation does not admit remotely generated executable UI.

## Files added or modified

Added:

- `hub/workspace.py`
- `hub/api_workspace.py`
- `agent/ui/workspace-state.js`
- `agent/ui/DynamicWorkspace.jsx`
- `agent/ui/workspace.css`
- `shared/workspace/v1.schema.json`
- `tests/test_a12_workspace.py`
- `tests/workspace_protocol.test.mjs`
- This document.

Modified:

- `hub/main.py`: new router registration only.
- `agent/ui/control_transport.py`: two narrow workspace endpoints in the existing allowlist.
- `agent/ui/CortexDashboard.jsx`: Workspace navigation, existing request flow and renderer mount.
- `agent/ui/main.jsx`: workspace stylesheet.

Production frontend assets were rebuilt. No commit/push or unrelated cleanup was performed.

## A12 follow-up and goal meter

Resume the interrupted native walkthrough: open Workspace, submit a bounded safe request, inspect a populated receipt/timeline, click Cancel while active, refresh/reopen, then verify disconnect/reconnect with controls disabled while stale. This is the remaining acceptance gate for the minimum vertical slice.

After that, richer data-specific presentations and additional explicitly authorized interactions can be considered within A12. General model-generated UI, Android consumption, audio streaming and broader interactions are deferred; no A13/A14 work is started.

**Cortex Goal Meter: 82% — unchanged planning estimate.** A12 now has an implemented/tested foundation and a live backend lifecycle, but its interactive native acceptance was interrupted. Raising the whole-project estimate based only on added tests would overstate verified readiness.
