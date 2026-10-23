# A13.1 discovery acceptance and closure

October 22, 2026. **A13 COMPLETE = YES** for the implemented discovery foundation. No A14 implementation was started.

## Exact automated results

| Gate | Passed | Failed | Skipped | Xfailed |
| --- | ---: | ---: | ---: | ---: |
| Retrieved final A13 consolidated run | 846 | 0 | 0 | 0 |
| Final A13 + A12 focused run after repairs | 36 | 0 | 0 | 0 |
| Final consolidated regression after repairs | **850** | **0** | **0** | **0** |

All 846 prior cases were retained. Four behavioral cases were added: three rejected-handshake variants and one workspace state separation case. The focused total comprises 27 A13 cases and 9 A12 cases, already included in the consolidated total. Final regression duration was 141.81 seconds, with two existing dependency deprecation warnings. Runs were serial; the normal live runtime used its existing durable state while pytest retained its isolated test database.

## Repairs

1. **Rejected-handshake registry mutation.** Before the repair, the Hub called A5 registration before validating a new advertisement. An authenticated but invalid replacement handshake removed the existing executor connection when rejected. Three new endpoint-level tests reproduced the defect for a forged identity, unknown capability and changed policy. Discovery preflight now runs before any executor registration or replacement. The tests verify the original connection, handle and persisted capability list remain unchanged. Live probes additionally verified the real connected ASUS catalog remained unchanged.
2. **Ambiguous workspace eligibility.** The table displayed approval-required support as unavailable and omitted independent authorization/locality information. It now displays Capability, Executor, Availability, Authorization, Execution and Reason using the existing A12 table component. Approval-required entries have `APPROVAL_REQUIRED`; offline availability does not erase authorization. A behavioral test checks online, approval-required and disconnected projections. No new protocol component, framework or execution action was added.

A13.1 code changes: `hub/discovery.py`, `hub/main.py`, `hub/workspace.py`, `tests/test_a13_discovery.py`. Documentation: this report and the status link in `docs/a13-discovery.md`. The existing native production bundle rendered the changed declarative data; no JavaScript/CSS redesign or bundle change was needed. No commit/push was performed.

## Real Hub and ASUS acceptance

Used the normal `hub.main:app` entry point on `127.0.0.1:8000`, the actual Windows ASUS Agent with its existing enrolled credential, and `agent.ui.main`. No model requests, paid voice, account writes or new OAuth scopes were used. Voice was mocked for this text/discovery acceptance; inference credentials were empty only in the acceptance processes, without editing saved credentials.

The final live run observed:

| State | Present | Available | Authorized | Immediately executable | Reason |
| --- | ---: | ---: | ---: | ---: | --- |
| Initial authenticated advertisement | 42 | 42 | 42 | 34 | 34 ELIGIBLE; 8 APPROVAL_REQUIRED |
| Actual ASUS Agent stopped | 42 | 0 | 42 | 0 | 42 DEVICE_OFFLINE |
| Actual ASUS Agent restarted, authenticated, re-advertised | 42 | 42 | 42 | 34 | 34 ELIGIBLE; 8 APPROVAL_REQUIRED |

The agent log records the final run's initial authentication at 1:29:22 PM and reconnect authentication at 1:32:36 PM, America/New_York. The API transition back to availability followed the reconnect authentication and fresh advertisement. No manual online flag or fabricated task completion was injected.

One permitted `ui.wait` for one second executed through the existing authenticated CLI → real ASUS executor path and returned success. Final-run command ID: `362f34937ff2471a985677c3e0489960`. An attempted wait while ASUS was disconnected was rejected with `Permission denied: DEVICE_OFFLINE`, without dispatch. Discovery of the other capabilities proves advertised handler support, not that every one was physically exercised.

## Native PyQt/React acceptance

Computer Use targeted the actual **Cortex Control Center** window and its bundled WebEngine application. It opened Workspace and expanded Discovered capabilities. No browser substitute, generated DOM fixture or legacy dashboard was used.

Observed in the real native table and accessibility document:

- Capability names including `app.close`, `app.launch`, `app.list_running`, `browser.open_url`, clipboard and filesystem operations.
- Executor `asus / local_device`, independent Available/Unavailable and Authorized states, permission tiers and eligibility reasons.
- `fs.copy` Tier 2 and `fs.delete` Tier 3 remained available/authorized with `APPROVAL_REQUIRED`; no approval or side-effecting control was invoked.
- After stopping the agent: ASUS rows changed to Unavailable, Authorized, UNAVAILABLE, DEVICE_OFFLINE. The Hub/workspace remained connected, correctly distinguishing an executor outage from a Hub outage.
- After legitimate reconnect: those rows returned to Available, Authorized, EXECUTABLE/ELIGIBLE; approval-required operations remained gated.

The disclosure showed 55 candidate rows: 42 ASUS advertisements plus 13 integration definition/account-status projections. This does not mean 55 operations were authorized or executed. Integration authorization remains account/scope dependent.

Screenshots in the acceptance conversation show the native online, offline, restored and approval-required rows. Some immediate accessibility/screenshot snapshots lagged a just-issued action; refreshed observations were used to confirm the resulting state. Bounded excerpts are saved in `native-evidence.json`. Computer Use was not stopped with Escape during A13.1. All acceptance-owned processes were stopped after saving evidence.

## Live malformed/forged advertisement checks

Seven negative handshakes were sent to the real Hub while the legitimate ASUS Agent remained connected:

| Case | Rejected | ASUS catalog unchanged | Legitimate ASUS remained connected |
| --- | --- | --- | --- |
| Invalid credential | YES | YES | YES |
| Advertisement claiming Lenovo identity | YES | YES | YES |
| Invented executable capability | YES | YES | YES |
| Attempt to override canonical permission tier | YES | YES | YES |
| External schema reference | YES | YES | YES |
| Unsupported discovery protocol version | YES | YES | YES |
| Extra executable command field | YES | YES | YES |

Identity, unknown-capability and policy preflight failures closed with code 4003. Other cases returned an authentication/validation rejection; their subsequent close code was not separately recorded. Before/after fingerprints covered the ASUS candidate definitions, schemas, policy, availability, authorization, execution state and verification evidence, excluding normal lease-expiry renewal. Existing-session preservation also has the new endpoint regression tests.

## Permissions, expiry and isolation

LIVE_VALIDATED: authentication rejection, forged registration rejection without catalog mutation, native approval-required presentation, and offline dispatch denial.

MOCK_TESTED / DETERMINISTIC: lease and heartbeat expiry, stale generations, schema/version changes, permission revocation immediately before send, executor ownership, worker cross-device/account isolation, OAuth scope/multi-account restrictions and provider recovery. These checks passed in the final regression. No additional live lease-expiry or second physical-device experiment is claimed.

Existing role, trust, privacy, approval, kill-switch and OAuth restrictions remain authoritative. Read-only discovery cannot grant a capability or approve its use. A10/A10.5 frozen source hashes matched the prior baseline, including AgentTurn, router/failover, compound orchestration/recovery, acceptance prompts and network-test guard.

## Actual ASUS advertisements

```text
app.close, app.launch, app.list_running, browser.open_url,
clipboard.read, clipboard.write, device.info, device.status,
fs.copy, fs.delete, fs.list, fs.move, fs.open, fs.rename,
fs.shortcut_info, fs.stat, process.list, screen.capture,
screenshot.read, transfer.inspect, transfer.send,
ui.click, ui.double_click, ui.drag, ui.hotkey, ui.keypress,
ui.move_pointer, ui.notify, ui.observe, ui.right_click,
ui.scroll, ui.select, ui.type, ui.wait,
voice.mute, voice.push_to_talk, voice.unmute,
window.focus, window.list, window.maximize,
window.minimize, window.restore
```

## Closure and limitations

All requested A13 closure gates passed: real native discovery view, actual device outage/recovery, authenticated re-advertisement, real forged-advertisement rejection and complete regression.

Android and a second physical device were not live-tested. Live audio, exhaustive execution of all advertised capabilities, and external integration conversational acceptance were not part of this run. The existing native voice/IPC connection label did not recover with the restarted agent in this walkthrough; discovery recovered through the authenticated Hub API. Live voice/IPC recovery remains separate validation debt, and no voice pass is claimed. Narrow table columns wrap long labels; cosmetic layout refinement was deferred.

```text
A13_IMPLEMENTED = YES
A13_MOCK_TESTED = YES
A13_REQUIRED_LIVE_ACCEPTANCE = PASS
A13_COMPLETE = YES
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A14 = NOT_STARTED
CORTEX_GOAL_METER = 82% (unchanged planning estimate)
```

The whole-project estimate remains unchanged: closing A13 establishes discovery reliability but does not close the outstanding A11 and compound live gates, and there is no new calibrated basis for assigning project-wide percentage points.

## Evidence

`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a13-1-acceptance`

- `acceptance-summary.json`: exact counts, capability names, lifecycle and closure flags.
- `native-evidence.json`: actual native accessibility excerpts and observed UI findings.
- `final-live/lifecycle.json`, `execution.json`, `offline-dispatch.json`, `security.json`: final live-run evidence.
- `regression-summary.json`, `regression.xml`, `regression.txt`, `suite-manifest.json`: complete 850-case gate.
- `focused-final.xml`: 36-case focused gate.
- `frozen-audit.json`: preserved frozen files and validation-debt flags.

Diagnostic logs remain local and can contain existing runtime context; they are not public sharing artifacts. The protocol/architecture reference remains `docs/a13-discovery.md` and `shared/discovery/v1.schema.json`.
