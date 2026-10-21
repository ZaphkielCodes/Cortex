# Pre-A11 compound UI generalization repair — 2026-10-20

## ROOT CAUSE — REPEATED ACTION

AgentTurn's exact-call seen set stopped immediately on a repeat. The durable semantic action guard also raised directly into a terminal ValueError. Neither gave the reasoning loop a bounded opportunity to use failed-effect evidence and choose another action. Receipt success and semantic progress were already separate, but failed verification spent one shared retry counter without precise recovery context.

## ROOT CAUSE — WRONG WINDOW

The model was advised to use prior window receipts, but ordinary observations could still read whatever application was foreground. Their contents reached persistence/sharing approval before an authoritative expected-target check. A launched process or disappearing startup handle therefore could lead to an unrelated window being observed. Native observations had process provenance, but the compound loop did not consistently bind it to the requested target.

## ROOT CAUSE — INVALID CAPABILITY

Proposal repair used a small generic counter and vague instructions. Unavailable capabilities often stopped immediately, while invalid names/arguments lacked focused canonical schemas and executor eligibility. Plans could declare a durable goal before their steps had been validated. A recoverable proposal error could escape as invalid_capability_call or lose its precise unsupported/unavailable explanation.

## GENERAL REPAIRS

The model-driven loop now rejects a bad proposal before execution, records bounded structured evidence, refreshes or reacquires the intended UI where appropriate, and asks for a corrected decision. It does not translate hallucinated names, force a provider, weaken privacy, or run rejected input. An observation is checked before its contents enter completed_steps, cloud-sharing approval, or model context. All frozen files match except one documented integration regression repair in provider_failover.py, described below.

## RECOVERY STATE MACHINE

The existing durable stages remain authoritative: REASONING → VALIDATE/GROUND → EXECUTE → VERIFY, with OBSERVING and REASONING for recovery. New durable recovery_counts and bounded recovery_evidence distinguish NO_PROGRESS, TARGET_CONTEXT_MISMATCH, TARGET_DISAPPEARED, CAPABILITY_REJECTED, ARGUMENT_SCHEMA_REJECTED, GROUNDING_AMBIGUOUS and VERIFICATION_FAILED. Each class has its own budget: three correction/recovery events, with two for TARGET_DISAPPEARED. Exhaustion fails truthfully. User denial/cancellation, blocked privacy, unavailable providers and unavailable executors retain their existing terminal/waiting behavior. Existing total task/decision/action bounds remain; read-only ui.observe does not consume the mutation/action allowance.

Model context carries the recent structured failures, expected and observed effects, canonical eligible capabilities/schema where relevant, current bound UI, and prior execution receipts/action fingerprints. Only the latest three recovery records and twenty executed-action summaries enter model_state; operational traces contain category codes and identity metadata, not hidden reasoning or raw UI contents.

## WINDOW/TARGET REACQUISITION

Trusted launch/focus receipts establish per-device expected window/process identity. Subsequent observations target that handle. Device, HWND, PID, process identity, timestamp and observation version are checked before use; full tree observations without a real window/version cannot prove completion. A native observation rechecks ownership after tree extraction and discards a raced tree. Native stale-input fencing and text readback also reject recycled handles owned by another process.

A mismatch triggers bounded canonical window.focus through the same permission, policy, lease and dispatch path. It first uses the observed handle/PID, then an exact canonical process identity if the handle disappeared. Successful reacquisition updates the binding and forces fresh observation. Windows hosted-content identity upgrades require OS ownership proof linking the same frame HWND and host PID. Temporary/splash handles, ambiguous hosts and nonexistent windows remain covered by regression tests. A truly missing target is reported to the model, which may explicitly replan/relaunch using a single-use permit tied to the original launch request; no automatic relaunch recipe is used.

## CAPABILITY CORRECTION

CALL_CAPABILITY and PLAN steps are validated against the canonical registry and selected executor before goal declaration or execution. Unknown/unavailable names produce CAPABILITY_REJECTED; incompatible declared arguments produce ARGUMENT_SCHEMA_REJECTED. Feedback lists currently eligible canonical IDs and the relevant registered argument schema. Repeated invalid proposals and repeated schema inspections are bounded. A corrected proposal proceeds through the normal execution policy. Exhausted correction preserves specific unavailable/unsupported explanations; it never invents a result.

## NO-PROGRESS HANDLING

Semantic action identity uses stable observed control identities, process/window context and protocol arguments, excluding ephemeral observation/element IDs and derived device/security fields. Pointer-call defaults and equivalent canonical right-click forms are normalized by their actual protocol effect. Semantic UI comparison excludes timestamps, enumeration IDs, focus, animation and coordinates while retaining control values and meaningful state. A successful command followed by an unchanged UI and unverified expected effect records NO_PROGRESS. A duplicate is rejected without execution, followed by a fresh bound observation and a request for a materially different action. Unverified text still cannot be resubmitted or followed by Enter; only readback/recovery continues.

## ACTION-REPLAY PROTECTION

Action history, execution receipts, pending exact-text verification, target bindings and recovery counters remain durable under the same task/parent identity. Reacquisition preserves an outstanding input receipt rather than erasing its verification obligation. Existing restart fencing and recovered-action guards remain. Opaque older checkpoints without semantic action metadata conservatively block replay of the last possibly executed capability. Provider changes cannot reset completed steps or authorize another recipient for private data. The A10.5 router, budget allocator, selection/health persistence and network guard are byte-for-byte unchanged. The sole provider integration exception recognizes a typed rate-limit-coded HTTP 413 as REQUEST_TOKEN_LIMIT rather than a proven model context-window failure. Actual context_length_exceeded/ordinary context-413 guards remain. No provider name, quota estimate, rank order, timeout, privacy eligibility or retry limit was changed.

## HARDCODING AUDIT

No application names, benchmark IDs, task phrases, UI recipes or provider-specific recovery rules were added to production recovery code. Decisions use structured capability IDs, registry schemas, native process/window identity, observed control metadata, local evidence predicates and bounded state-machine rules. Existing platform app resolution remains outside this repair. No scenario handler was added for any of the three acceptance prompts.

## NEW TESTS

New invented-app/control fixtures cover unchanged UI, semantic duplicate rejection, fresh readback and an alternative action; wrong foreground content discarded before model/persistence; handle recreation with exact process reacquisition; truly disappeared targets and explicit replan/relaunch; unknown capabilities and malformed arguments corrected before execution; executor mismatch; bounded invalid proposals; provider failover during capability recovery without launch replay; independent class budgets; PID reuse/wrong-device rejection; canonical hosted-content ownership; a raced accessibility tree; equivalent pointer protocols; text readback process mismatch; phantom controls without a window; plan correction before goal freeze; opaque legacy checkpoint no-replay; rate-limit-coded HTTP 413 with an unknown-window alternative; and preserved actual context-capacity guards.

### Behavioral fixture migrations

- test_agent_turn_runtime.py: current UI fixtures now include real-time timestamps/PID; duplicate-launch behavior now asserts one launch plus safe readback and verified continuation instead of immediate failure.
- test_a10_5_routing.py: the next-provider replay test retains exactly one launch and now permits the required recovery observation/verified response. Its synthetic cloud-sharing policy is explicit; production privacy is unchanged.
- test_compound_ui_runtime.py: a stale post-action observation now increments VERIFICATION_FAILED's own counter instead of the former global retry counter. Immutable goals and fail-closed completion assertions remain.
- test_general_ui.py: the shared UI fixture uses a current timestamp, because a fixed 2026-10-17 snapshot must be rejected by the new freshness contract. Unverified typing and cross-device observation tests supply enough readbacks to exhaust bounded recovery and assert no unsafe input; they no longer require an immediate stop with an exact old read count.
- test_privacy_approval_runtime.py: the synthetic private observation now supplies a window/PID and current timestamp so it is a valid target-bound observation before the existing approval assertions run. Approval correlation, recipient binding, revocation, denial, timeout and no-upload assertions remain intact.
- test_phase_a5.py and test_stabilization_contracts.py: finite scripted models now supply four rejected proposals, covering three corrections and the exhausting fourth rejection. Device ownership, no alternate executor, no dispatch and truthful unsupported results remain asserted.

The first consolidated diagnostic found fourteen failures: six generic UI successes used stale fixture timestamps; three generic UI failure assertions also assumed immediate stopping; three unavailable-provider tests exposed loss of precise terminal reporting and were repaired in production; the executor-mismatch fixture lacked enough correction proposals; and the historical repair-budget test expected the old three-call stop. No test was removed, skipped, xfailed or blanket-relaxed.

## FULL REGRESSION

**709 passed / 0 failed / 0 skipped / 0 xfailed**. All 688 baseline identities retained; 21 added checks. The consolidated manifest is the existing A1–A10/runtime/privacy/provider suite plus tests/test_compound_recovery.py. External networking was forbidden by the unchanged conftest guard. Final source and frozen-file hashes are recorded alongside the JUnit results.

The compound source entered the sole live attempt after a clean 707/0/0/0 gate. The live HTTP-413 regression then received the narrow offline-only classifier/message repair and two more tests; the reported final full regression is for that final source. Compound recovery source hashes match the single live run. The classifier correction has not been retested live.

Evidence: regression.xml, regression.txt, regression-summary.json, suite-manifest.json, frozen-hashes.json, live-source-sha256.json and source-sha256.json in this evidence directory.

## COMPOUND ACCEPTANCE

The original run_compound_acceptance.ps1 launcher ran exactly once after the offline gate, with its three unchanged prompts and manual model-sharing approvals. No reviewer typed, clicked, focused a different test window or otherwise completed a task on Cortex's behalf. Runner process success is not semantic success.

### layout_change

FAIL / review-limited: the model corrected a rejected proposal, launched Calculator, and obtained a fresh native observation bound to CalculatorApp's actual HWND/PID. The reviewer took too long checking the window and its cloud-sharing approval expired. A late approval returned HTTP 400; this was the reviewer's delay, not proof of a compound runtime failure.

Final status: denied. Semantic pass: False. Actions: [{'capability': 'app.launch', 'arguments': {'activate': True, 'app': 'Calculator', 'target_device': 'benchmark-windows'}, 'command_id': 'bb7e66efed4947d7913347e80341f42d', 'status': 'success'}]. Observations: [{'observation_id': 'obs_3e80e8e2', 'device_id': 'benchmark-windows', 'window_id': 5310840, 'pid': 33968, 'foreground_app': 'CalculatorApp', 'timestamp': '2026-10-20T15:35:56.124572+00:00', 'control_count': 46, 'native_identity_consistent': True}]. No-progress events: 0. Rejected decisions: 1. Reacquisitions: 0. Replans: 1.

Verification evidence/outcome: Native UIA readback showed Standard Calculator mode and Display is 0. Scientific layout and preserved final display were not verified. No UI mutation beyond launching occurred.

Failure classification: HARNESS_REVIEW_APPROVAL_TIMEOUT; new compound UI completion remains untested in this case.

### canvas_dimensions

FAIL: two incompatible proposals were rejected before execution and fed back as ARGUMENT_SCHEMA_REJECTED; a corrected PLAN then launched Paint once. The following reasoning call received Groq HTTP 413 with rate_limit_exceeded and the then-frozen router classified it as CONTEXT_TOO_LARGE. Unknown alternative context windows then blocked fallback.

Final status: error. Semantic pass: False. Actions: [{'capability': 'app.launch', 'arguments': {'activate': True, 'app': 'mspaint', 'target_device': 'benchmark-windows'}, 'command_id': 'f33a2a53e2ea4eca9c18a46470429dd7', 'status': 'success'}]. Observations: []. No-progress events: 0. Rejected decisions: 2. Reacquisitions: 0. Replans: 2.

Verification evidence/outcome: No canvas observation, dimension edit or readback completed. The 640-by-480 end state was not verified. This is not evidence that every alternate provider lacked model context capacity.

Failure classification: REQUEST_TOKEN_LIMIT_MISCLASSIFIED_AS_CONTEXT; narrow general integration fix applied and verified offline after this sole live run, without a live rerun.

### unsent_document

FAIL / blocked: no capability executed. The task was rejected by the existing provider cooldown/eligibility state left by the preceding case.

Final status: error. Semantic pass: False. Actions: []. Observations: []. No-progress events: 0. Rejected decisions: 0. Reacquisitions: 0. Replans: 0.

Verification evidence/outcome: No unsaved document or exact two-line text readback was produced or verified.

Failure classification: PROVIDER_COOLDOWN; no live evidence for this task's repaired computer-use path.

## A10 GENERAL COMPOUND ORCHESTRATION RESOLVED YES/NO

**NO.**

## READY FOR A11 YES/NO

**NO.** A11 was not started. The previously accepted A10.5 failover path remains intact; the documented rate-limit-413 integration edge was repaired offline only.

## REMAINING VALIDATION DEBT

The requested physical end states remain unverified. The single run corrected invalid proposals and bound a real Calculator observation, but exercised no UI input/readback completion and no live duplicate/reacquisition recovery. The review timeout is an operator/harness limitation. Paint exposed a genuine HTTP-413 request-limit classification edge, now repaired offline only; provider/token capacity and prompt budget still need a healthy live validation. Notepad was blocked before execution. A10.5's prior PUBLIC cross-provider success remains historical proof, not proof of private UI provider availability. No second acceptance was run or claimed.

No production runtime restart, credential change, commit or push was performed. Earlier failure evidence remains intact. No second compound acceptance was run.
