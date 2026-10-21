# A10 final hardening: compound desktop orchestration

Date: 2026-10-19. Scope: general controller mechanics. No A11 work or live desktop benchmark was run. Earlier consolidated runs exposed unintended real-provider access in an existing A9 GUI test; see the full regression disclosure below. Scripted decisions prove execution and safety contracts, not language generalization.

## COMPOUND UI AUDIT

The primary path is `RequestPipeline.execute` -> safety controls -> `AgentTurn.run` -> persisted agent task -> `ComputerUseTaskController.agent_runtime` -> model decision -> canonical schema validation -> strict target grounding -> policy/approval -> executor selection/lease validation -> durable in-flight receipt -> device dispatch -> validated execution result -> local receipt/evidence checkpoint -> post-action observation -> local effect verification -> next model turn -> final evidence predicates.

`hub/compound_ui.py` holds the durable controller state and verifiers. The existing computer-use compatibility entry delegates autonomous decisions to AgentTurn and requires its control/model runtime. The primary path does not call app-specific legacy recipes or fuzzy/ordinal target selection. No Calculator, Settings, Notepad, browser, or benchmark language handlers were added. This is not a claim that every old compatibility helper elsewhere in the repository has been removed.

## STATE LOSS / LOOP RISKS FOUND

- In-memory observations and pending text checks were lost across interruption. Recovery now invalidates snapshots and retains a private exact field-readback checkpoint.
- A model response could finish a compound task after dispatch without proving the final UI state. Completion now requires the immutable evidence contract.
- Old element IDs, duplicate labels, coordinate fallbacks, and selection by blind typing could send input to an unintended control. The primary path now requires exact, unique observed targets.
- Native scrolling lacked a freshness check. It now checks before cursor movement/wheel input and rejects missing or moved targets.
- Results could reach a provider/privacy failure before durable execution facts were saved. Receipts and actual evidence now persist first.
- A restarted turn could repeat an action with a new observation/element ID. Stable target/action fingerprints and recovered-action history prevent equivalent unverified replay.
- A success predicate using `exists` could accept ambiguous controls; an absence predicate could consult the wrong device. Both now fail closed.

## STATE MACHINE CHANGES

Persisted `tasks.context.compound_ui` includes goal, immutable success_condition, stage, current/previous observation IDs, action, safe target selector, expected_effect, execution receipt/history, verification, retry count, blocked reason, task status, source/parent identity, and operational trace.

Explicit stages: OBSERVING, REASONING, GROUNDING, WAITING_FOR_APPROVAL, EXECUTING, VERIFYING. End/wait states: COMPLETED, FAILED, CANCELLED, WAITING_FOR_EXECUTOR, WAITING_FOR_PROVIDER. Transition validation and the completion guard live in the controller, rather than being model instructions alone.

The same child and parent task identities survive quick acknowledgements, approvals, executor waits, provider waits, and explicit resume. GUI task state exposes the current stage, retry count, blocked reason, and trace sequence. Existing ownership, fencing, approval and privacy checks remain on the dispatch path.

## OBSERVATION FRESHNESS

Every primary UI input requires an explicit current observation tied to the target device/window. Native freshness checks reject missing/expired snapshots (30-second limit), foreground/window changes, disappearance, and materially changed window geometry. Input targets are reread against native accessibility identity; materially moved or missing controls are rejected. Click, typing, selection, drag endpoints, pointer movement and scrolling use these boundaries. Keyboard input retains its focus/ownership checks.

The primary native target resolver no longer substitutes the latest snapshot or silently captures one when the requested ID is unavailable. Reusing the same observation ID after input cannot satisfy post-action verification. Restart/resume always requires a new observation before any side effect.

Native UI snapshots now expose selection/toggle/expand states when accessibility provides them. Layout hashes include actual identity/control data, but are diagnostic and never semantic proof. Native extraction does not yet certify a complete tree: absence predicates therefore remain conservatively unverified unless a trustworthy observer explicitly supplies completeness.

## EXECUTION RECEIPTS

Checkpoint before dispatch: task/parent ID, attempt fingerprint, device, capability, in-flight status, ran=unverified, side-effect possibility, retry safety, verification requirement. After dispatch: actual command ID/status and proved no-input or possible-input facts. A success receipt alone is not UI completion. Readback observations have their own safe receipts without overwriting the pending mutation receipt.

Actual bounded capability results are saved locally before cloud-sharing or interpretation. Private pending text state stays local and is excluded from the operational trace/model-state payload. No-input preflight rejection permits re-grounding; uncertain input never grants replay permission.

## REPLANNING LOGIC

The model gets structured state, observed evidence and rejected-proposal/verification feedback. It chooses the next atomic action. Invalid proposals are repaired before dispatch; stale/missing targets prompt observation or clarification. Failed expected effects increment a shared bounded replan count (maximum three); action and turn budgets also remain bounded.

Equivalent unverified actions are blocked using capability/device/window/observed-target/argument fingerprints, independent of changing snapshot-local element IDs, including both drag endpoints. A changed success condition is rejected. Different actions remain possible after fresh evidence, within the retry budget; no app recipe supplies them.

## SEMANTIC COMPLETION

Final conditions are typed literal predicates over unique observed controls or structured successful capability data, with device identity checks. Dispatch status and result prose cannot be capability predicates. Compound UI input needs UI evidence at completion; launching, clicking, typing, focusing, layout changes, or a model saying done are insufficient.

Expected effects are checked after a fresh observation. Text uses stable-field readback, including exact replacement checks and credential exclusions. A provider interruption after typing preserves that pending check so a multi-field goal can resume by verifying the first field, rather than retyping it or incorrectly requiring every remaining field to be finished already.

The model still chooses conditions from the user's goal. Mechanics tests cannot establish that those conditions fully capture arbitrary natural-language intent; this remains a live acceptance requirement.

## FAILURE/RETRY SAFETY

Cancellation records uncertain in-flight effects truthfully. Executor loss enters the existing wait/reconnect path and invalidates old observations. Provider cooldown is reflected as WAITING_FOR_PROVIDER without changing privacy-bound routing. Provider failure preserves receipts and leaves completion unverified.

Hub restart marks incomplete agent turns unverified and disables automatic side-effect replay. Explicit controller resume verifies stored owner/task/parent identities, reuses the task ID, and starts with fresh observation. This is a controller API, not a newly implemented conversational or GUI resume feature. The restart test invokes the real recovery loader with a durable in-flight task; it does not claim a live process-restart demonstration.

## OPERATIONAL TRACE FORMAT

Each bounded trace entry contains sequence, turn, prior stage, observation_id, decision type, capability, safe target IDs/fingerprint/device/window, result status, verification booleans/criterion indices, next_state, latency_ms, timestamp, task_id and parent_task_id. The last 128 entries and 40 safe receipts are retained per checkpoint; sequence numbers remain monotonic across resume.

No hidden chain-of-thought, raw model prompts, typed text, secret values or raw control labels are placed in this trace. Existing private local evidence is separately retained under existing policy. Benchmark exports include compound_trace and a final compound_summary so diagnosis need not inspect provider prompts.

## PROVIDER-INDEPENDENT TEST RESULTS

The new `tests/test_compound_ui_runtime.py` uses invented Quasar/Nebula/Aurora controls and explicit scripted model decisions. Coverage includes launch-observe-click-observe-select-verify; launch-type-readback; stale/missing target re-observation; ambiguous targets; provider failure after input; exact pending text recovery across a multi-field goal; cancellation; executor loss/reconnect; source approval/parent identity; restart recovery; no equivalent replay; false model-done rejection; freshness/control movement; native scroll/select no-input boundaries; duplicate readback rejection; immutable goals; wrong-device absence and ambiguous existence predicates; and operational provider cooldown state.

### Existing fixture migrations and preserved assertions

No baseline test was deleted, skipped, xfailed, or replaced with unconditional success. Changed fixtures now provide the evidence contracts required of a real model, and actual native-control readback required by the input fence:

| Existing test or fixture | Why changed; assertion preserved |
|---|---|
| AgentTurn calculator compound | Declares Mode.text=Scientific and its expected effect, with explicit device/window/control observations. Original launch/observe/hotkey/readback order and final evidence checks remain. |
| AgentTurn stale/cross-device observation | Declares a final predicate so the proposal reaches the fencing boundary; still forbids clicks against stale or another device's observation. |
| AgentTurn duplicate mutation | Declares structured launch data so the test exercises duplicate dispatch protection; still exactly one mutation. |
| AgentTurn real confirmation resolver | Waits for actual persisted approval readiness instead of assuming a fixed tiny sleep is sufficient; source scope and exactly one approved execution remain. |
| General UI app-parameterized observe/type/readback, missing-target and real-dispatch cases | Explicit Composer text predicates replace model decisions without contracts; same typed field, input count, actual readback and no guessed target assertions remain. |
| General UI unverified typing variants | Supply valid structured conditions even for the attempted Enter proposal, so the pending-readback guard is actually tested; false success/submission remain forbidden. |
| General UI native focus fixture | Supplies an actual same-control native reread for the new movement check; focus/Unicode input boundary assertions remain. |
| Stabilization compound completion/followup identity | Declares structured actual launched window ID; identity persistence, private title exclusion and followup context checks remain. |
| A2 conversational visual verification | Declares a predicate over the actual structured screen-analysis summary; screenshot identity, exact provider/capability calls and interpretation remain. |
| A1 private/local hostname detection and shared runtime connectivity fixture | Public hostname resolution now uses explicit numeric DNS answers; original loopback/private/link-local/public classifications remain asserted. The shared OS connectivity probe is explicitly offline so context publication cannot contact the network. Actual peer TLS still runs on loopback. |
| A9 conversation message flow | Replaces the configured real provider with one explicit scripted decision through the actual GUI endpoint/harness/AgentTurn path. Now checks exact success/text and one model call, rather than accepting any success/error/unsupported outcome. Removes unintended quota-dependent networking. |
| A10 pointer, scroll and drag fixtures | Supply explicit observation ID, current HWND and native endpoint/control rereads; exact pointer coordinates, wheel direction/amount/event and multiple native drag movements remain asserted. The old scroll fixture omitted an observation, which the hardened boundary correctly rejects. |
| A10 compound Notepad fixture | Declares final text condition while still requiring clarification before guessed typing; launch-once/no-input assertions remain. |

These are scripted contract migrations, not evidence that the provider understood any prompt.

## FULL REGRESSION TOTAL

Final source-frozen, external-network-blocked run: **610 passed / 0 failed / 0 skipped / 0 xfailed**, 85.56 seconds, two dependency deprecation warnings. All **585 baseline test identities** remain and pass; **25 new checks** pass. The original **49/49** and **26/26** historical closure identities also pass. Evidence: `C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a10-final-hardening\final-v6.xml`, `final-v6.txt`, `suite-manifest.json`, and `verification-summary.json` in the same artifact folder. No external network attempt was recorded by the final run guard.

 Earlier final-v4 had 609 passed / 1 failed: A9 conversation_message_flow attempted configured Groq calls, received HTTP 429 quota errors, and returned an asynchronous acknowledgement that its old assertion rejected. Previous green runs also lacked an external-network guard and must not be represented as guaranteed provider-independent. The A9 fixture is now explicit, and tests/conftest.py rejects any external DNS/connect attempt (including attempts caught by application error handlers), while permitting loopback TLS transport and numeric IP parsing without DNS. The existing runtime connectivity probe is explicitly replaced with an offline fixture, and the A1 hostname-classification test supplies deterministic DNS answers rather than querying the internet. No deliberate live benchmark was performed, but the earlier unintended calls violated the requested no-live-call boundary. The suite is the existing 37-module consolidated manifest plus live-evidence repairs, window identity, and the new compound runtime module. It covers A1-A10, architecture repair, AgentTurn, GUI controls/delivery, recovery/background work, context compression, provider routing, privacy and local TLS peer transfer. The original 585 test identities and the historical 49/26 closure lists are compared with the final XML.

## LIVE RETEST READY YES/NO

YES: `scripts/run_compound_acceptance.ps1` prepares one compact three-case run with a frozen manifest in `benchmarks/a10_compound_acceptance.json`. These prompts were not used to build production recipes or scripted implementation tests. `-ValidateOnly` checks preparation without opening apps or contacting providers.

After quota reset, run from the repository: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_compound_acceptance.ps1`. It runs each case once with existing isolated hub/agent infrastructure and manual model-sharing approvals, writes task/parent identity, action evidence, timing and operational traces, and requires human outcome review. Use blank test documents. This is not an automated semantic pass scorer. No new live score is claimed; the prior benchmark remains 12/18.

## A10 STABILIZATION COMPLETE YES/NO

NO. The deterministic controller mechanics are hardened and tested; live provider-driven compound completion and real app reliability are still unproven. Native accessibility coverage, goal-to-predicate alignment, and end-to-end process restart behavior need live acceptance after quota reset.

## READY FOR A11 YES/NO

NO. A11 was not started.

Production `data/cortex.db` SHA256 remained `21643cdbb2ab8f824ad3f3b3a095da5ccc7614cf7dee9eb009ec3461347f2d14`. No commit or push was performed. Existing unrelated dirty changes were preserved.
