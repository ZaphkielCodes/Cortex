# A12.1 native workspace acceptance

Validated October 22, 2026. **A12 minimum workspace vertical slice: COMPLETE.**
This decision covers native text/task presentation and authorized cancellation. It does not close A11 or pre-A11 compound validation, add integrations, or start A13.

## Runtime and test boundaries

Used the normal production entry points: `hub.main:app` at `127.0.0.1:8000`, `agent.agent --device asus`, and `agent.ui.main`. The existing ASUS enrollment and normal durable state were reused. No synthetic task rows, scripted model decisions, fake executor receipts, legacy dashboard, or browser substitute were used for the desktop checks.

The actual PyQt6 application exposed the `Cortex Control Center` window and bundled React `Cortex Command Center` document through Windows accessibility. Computer Use operated Workspace navigation, request input, Send, task details, recorded lifecycle, Refresh workspace, and Cancel task. Screenshots and accessibility observations were inspected. Input interference temporarily interrupted clicks; the user explicitly authorized resuming and the checks continued. Computer Use was not stopped with Escape in this acceptance run.

The run used the existing direct Groq path. Paid voice services and alternate inference providers were disabled in the acceptance process environment. Voice was mocked to keep this a text-only test; no live voice acceptance is claimed. Provider availability was deliberately removed for one harmless question by restarting the test Hub without inference credentials. The Groq environment was restored afterward; saved credentials and `.env` were not edited. All processes launched by the bounded harness were eventually stopped automatically.

## Native acceptance results

| Check | Result and evidence |
| --- | --- |
| Normal Hub + ASUS Agent + native application | PASS: authenticated normal runtime, actual PyQt/WebEngine window |
| Request → real AgentTurn → native task/result | PASS: five-second wait submitted using native input and Send |
| Cards, progress, task details | PASS: completed status, response card, completed progress, expandable identity/result |
| Timeline and table | PASS: expanded recorded REASONING/GROUNDING/EXECUTING/VERIFYING stages; `ui.wait / success / asus` receipt |
| Active task rendering | PASS: second wait visibly RUNNING with indeterminate progress and Cancel task |
| Native cancellation | PASS: native Cancel click produced `/api/workspace/action` HTTP 200; same request became CANCELLED, button disappeared, no completion animation |
| Workspace refresh | PASS: native Refresh workspace retained the cancelled task and its recorded result |
| Reopen after rebuilding | PASS: native application restarted and recovered the completed task from durable state |
| Hub disconnect/reconnect | PASS: native accessibility reported workspace disconnected/stale while retaining results; after Hub restart it reported connected with the same cancelled request |
| Failure presentation | PASS: fresh question failed with explicit provider-not-configured response, error component, empty receipts, no animated running indicator |
| Unauthorized action enforcement | PASS at real backend: missing authentication 401, executable/extra fields 422, unowned task 403; no action dispatched |

The disconnected-state finding uses the returned native accessibility document. Some immediate screenshots/accessibility snapshots lagged a just-issued click; decisions used refreshed observations rather than assuming an action succeeded. Native screenshots confirmed the fresh FAILED error panel and removal of its progress animation.

Cancellation is an orchestration cancellation, not a guarantee that an already-dispatched OS command can be physically interrupted. The UI correctly says an already-sent action may still finish. No successful receipt was invented for the cancelled task, and its durable request remained CANCELLED after reconnect. The two wait requests each produced one native `ui.wait` command start; the completed wait produced a successful finish. No new duplicate command start appeared across reconnects.

## Exact task evidence

| Request ID | Observed durable lifecycle | Recorded receipt |
| --- | --- | --- |
| `request_e8e49c57ee5b3ed515d2ec00` | RUNNING → COMPLETED | `ui.wait`, success, asus |
| `request_daeadb133016f54a578066b4` | RUNNING → CANCELLED | None; truthful cancellation response |
| `request_2ed5f0c24de52e00f0dc41c7` | RUNNING → FAILED | None; controlled inference configuration failure |

The first and second requests used the real provider/native executor. The failure question used no model response and no executor; the missing-provider error was real. The lifecycle artifact records backend transitions; native observations additionally demonstrate RUNNING/CANCELLED, COMPLETED details/receipt/timeline, and FAILED/error presentation. Not every short-lived internal transition was visually captured.

## Defects fixed

1. Incremental envelopes appended new task groups below older history. The reducer now groups by task identity and sorts groups by recorded update timestamp. Stable component IDs remain intact; components for the same task stay together with details first. Snapshot and delta paths yield consistent presentation order.
2. Terminal tasks with unknown numeric progress still rendered an indeterminate progress bar. Failed, cancelled, denied, paused and unknown states now show their truthful state/label without a running animation. Active known states retain indeterminate progress; recorded completion displays full progress.

Only `agent/ui/workspace-state.js`, `agent/ui/DynamicWorkspace.jsx`, `tests/workspace_protocol.test.mjs`, this report, and the A12 documentation status were changed. Production frontend assets were rebuilt. No router, AgentTurn, execution/approval policy, OAuth scope, A10/A10.5 code or legacy dashboard was changed. No commit/push was performed.

## Automated results

| Gate | Exact result |
| --- | --- |
| Consolidated regression after both repairs | **823 passed / 0 failed / 0 skipped / 0 xfailed** |
| Standalone JavaScript workspace checks | **7 passed / 0 failed / 0 skipped** |
| Native frontend production build | PASS, 26 modules |
| Frozen source hash audit | No mismatches |

The consolidated suite retains all 823 A12 baseline checks. Two additional JavaScript behavioral checks cover incremental task ordering and terminal progress presentation; the JavaScript suite is also invoked by a pytest wrapper, so these numbers are not additive unique test totals. No historical assertion was weakened. Pytest emitted two existing warnings; the frontend emitted its existing large-bundle warning.

## Security and remaining limits

Live malformed/unauthenticated/unowned controls were rejected by the actual backend. Forged/stale descriptors, cross-device ownership, strict schemas and data-only action serialization also remain covered by automated tests. No hidden or arbitrary executable UI control was introduced. Existing authorization remains authoritative, and the presentation repairs confer no new capability or permission.

The disconnected warning/recovery was observed natively. Disabling a still-active Cancel button during a disconnect is enforced by the renderer's connection-state guard; that specific simultaneous native combination was not separately exercised because the task was already cancelled before the outage. A supplemental post-run stale-control HTTP replay was not completed: the bounded runtime had shut down, so the connection was refused. It contributes no live pass; existing stale/replay automated checks remain the evidence for that case.

Live microphone/STT/TTS, Android rendering and broader workspace actions remain outside this minimum slice. No exhaustive long-duration, multi-monitor or accessibility certification is claimed. The large frontend bundle remains follow-up performance debt. These do not expand A12.1 acceptance into general computer-use or integration acceptance.

Preserved:

```text
A12_MINIMUM_NATIVE_WORKSPACE_ACCEPTANCE = PASS
A12_COMPLETION = YES (defined foundation scope)
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A10_A10_5_FROZEN_HASH_MISMATCHES = NONE
A13 = NOT_STARTED
```

## Artifacts

`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a12-1-native`

- `native-evidence.json`: bounded excerpts from actual native accessibility observations.
- `live-summary.json` and `lifecycle.json`: exact task identities/states/receipts and live security statuses.
- `security.json`: real HTTP rejection results.
- `regression-summary.json`, `regression.xml`, `regression.txt`, `suite-manifest.json`: consolidated regression evidence.
- `frozen-audit.json`: frozen hash audit and preserved debt flags.
- `hub.log`, `agent.log`, `desktop.log`: local diagnostic logs; these can contain existing runtime context and are not a public sharing artifact.

The original A12 implementation report remains the protocol/architecture reference. This acceptance supersedes its interrupted desktop status, while retaining its deferred audio/Android boundaries. Cortex Goal Meter remains an **82% planning estimate**: A12's defined native gate is now closed, while A11 and compound live validation debt still prevent a broader readiness claim.
