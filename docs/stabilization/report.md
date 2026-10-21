> Historical snapshot. Superseded by [49-failure closure and live orchestration report](close49-report.md): 570 offline passes, latest completed live acceptance 12/18.

# Cortex pre-A11 stabilization report

Date: 2026-10-19. A11 was not started. No commit or push was performed.

## 26 FAILURE CLASSIFICATION

- REAL_REGRESSION: **3**
- OBSOLETE_TEST: **5**
- TEST_EXPECTATION_CHANGED_BY_NEW_ARCHITECTURE: **18**
- NONDETERMINISTIC_INFRA_FAILURE: **0**

All original 26 are present and passing in the final consolidated XML. Each individual classification and changed-test rationale is recorded in historical-classification.md / historical-classification.json. Scripted model fixtures prove wiring and policy, not language comprehension.

## REAL REGRESSIONS FIXED

The original A6/A7/A8 failures exposed missing model-accessible ownership, privacy and transfer facts. Added canonical system.runtime_state with authenticated source identity, enrollment/role scoping and persisted state. Models cannot choose the caller identity through arguments.

Additional repairs found during stabilization:

- Hub-owned canonical capabilities no longer require advertisement by a Windows agent. Role permissions and per-device deny/confirmation overrides still apply.
- Window and process inventory results are now SENSITIVE before cloud interpretation. A live retest revealed that titles/paths could previously enter cloud reasoning payloads without explicit sharing approval. The repair has offline denial tests; it has not been proven in a fresh full live run.
- Provider HTTP 413 has a truthful context-too-large error category. On-demand canonical schema discovery and tighter local history/evidence budgets reduce repeated payloads; reliable live success is still unproven.
- A bounded retry honors actual provider 429/cooldown timing, retries reasoning once and never replays an executor action. It does not add a guessed local request quota.
- Native bounded waits no longer hit a shorter 15-second control timeout; the execution timeout is 45 seconds and the interactive acknowledgment remains separate.
- Shared speech sanitization suppresses raw capability dumps and internal error structures.
- Mock model metadata no longer reports a functioning inference provider.
- Isolated lifecycle and benchmark profiles use their own runtime state/IPC files. Lifecycle tests provision a dedicated credential in their database copy rather than depending on the user's credential store.

## OBSOLETE TESTS MIGRATED

Migrated the five original obsolete tests and 18 changed-contract tests with per-test explanations. Retained source/executor, approval, stale-target and canonical dispatch assertions. Removed assumptions that device names grant privileges or that ordinary language is handled by model-free phrase recipes. Added explicit model decisions where testing an offline boundary.

Additional changed-test explanations:

- Phase 6 mock HTTP responses now provide the headers interface consumed by the real client.
- GUI task synchronization checks the durable parent request identity rather than assuming the child AgentTurn identity is the delivery key.
- Lifecycle fixtures isolate state and mock login registration instead of changing the real Windows startup entry.
- Model metadata checks the actual offline marker and unavailable state. Capability explanation checks exact registry IDs rather than keyword replies.
- The negative cooldown test disables waiting explicitly; a separate test verifies provider-requested waiting and identical request retry without executor replay.
- New schema-discovery tests verify inspection does not execute actions and repeated inspection stops.
- New privacy tests verify private inventory canaries never reach a cloud model after denial.

## CONSOLIDATED REGRESSION TOTAL

**504 passed / 49 failed / 0 skipped / 0 xfailed** across 553 collected checks in 37 selected modules, 108.22 seconds. There were two warnings. A1-A10, architecture repair, AgentTurn, GUI/runtime controls, background/recovery, compressor and provider routing modules were included.

The original broader baseline was 454 passed / 91 failed; it uncovered more failures than the reported historical 26. The remaining 49 are individually listed in remaining-failures.md. They still fail and have not been hidden by skips, xfails or blanket assertion changes. Most invoke removed synchronous phrase planning or expect natural-language inference from the unavailable offline provider. Their replacement behavioral coverage is unfinished; this is not a clean-suite claim.

All 12 lifecycle checks, 20 privacy-approval checks and 15 task-recovery/context component checks pass. These component results do not replace the failed live acceptance gates.

## HARDCODED ROUTING AUDIT

The ordinary request pipeline uses safety controls then AgentTurn. No Notepad/Settings/weather/CPU/Downloads/Calculator request-phrase branch was found there. Canonical schemas, source identity, permissions, leases, privacy and observation freshness remain deterministic boundaries.

The AST/text audit found 119 candidate sites. hardcoding-audit.md lists each reviewed component and hardcoding-candidates.json provides individual locations. Remaining active language heuristics include social/context inference, calendar/person context updates, routine activity labels and background collection categorization. Compatibility keyword trees remain in target resolution, system self-knowledge, ownership, file transfer, goals, routines and workload components. No primary-path callers were found for the listed legacy request handlers; that is not proof every compatibility entry point is unreachable or instrumented.

**Major NL heuristics remain. An all-hardcoding-removed claim would be false.**

## BLIND LIVE BENCHMARK RESULTS

The runner uses authenticated /api/control/message, the same text endpoint as the GUI, through the actual provider and a real Windows agent. It starts isolated runtime processes and persists task evidence. Frozen manifest SHA-256: 0e0ee45f387feaee0944f08493d0d6aee53c20c16131f4624d1f6ad73710f448. The prompts were checked against implementation tests before the first attempt. No phrase-specific production fixes were added.

- Initial blind attempt: **1/18 complete-case passes**; actual 429 and cooldown errors prevented useful task coverage. The one pass is expected missing-provider failure handling.
- Pacing retest of the same prompts: **10/18 complete-case passes**, plus partial cancellation and privacy-sharing denial evidence.
- Later schema-discovery retest: **4/13 complete-case passes**. It is a subset, not a new blind sample or an improved acceptance rate.
- Separate restart retest: **0/1**; unregistered model-proposed capability rejected before execution.

Results from different implementations/attempts are not combined. live-scorecard.md and the local live-scorecard.json score all ten requested dimensions for every case. Unobserved/not-applicable dimensions are explicit nulls, not invented passes. Argument fidelity cannot be scored where the original arguments were not persisted. Quick-ACK latency scores are separate from actual final latency. The target 90% complete-case acceptance rate was not reached.

Weather, foreground metadata, cache explanation/follow-up/pronoun/correction and one background wait completed in the pacing run. CPU telemetry completed in the later run. App launch, compound UI work and folder handling were not reliably completed. Invalid decisions, unavailable capabilities, oversized payloads, privacy approvals and real provider quotas caused failures. Privacy denials are not credited as successful UI execution.

The runner denied every pending approval. It did not authorize sensitive sharing. The window/process classification gap nevertheless exposed a pre-fix cloud-payload boundary problem, documented above. No full live attempt after that repair is claimed.

## LIVE PROVIDER RESULTS

- Groq direct: live arithmetic request succeeded, answer 15, 27 total reported tokens. Real Cortex turns also exercised it, with both successes and actual quota failures.
- NVIDIA DeepSeek: live inference timed out after 40 seconds; model catalog authentication had succeeded.
- NVIDIA GLM: public direct request succeeded, answer 15, about 6.8 seconds. This is direct-provider validation, not proof of an NVIDIA AgentTurn end-to-end path.
- Missing-provider path: actual isolated runtime returned a truthful configuration error.
- Cooldown: actual 429/cooldown responses observed; bounded retry covered separately with a controlled response test.
- Privacy restriction and recipient pinning: offline checks pass; live denial observed. No full live failover/recipient-pinning acceptance proof.

NVIDIA remains restricted to PUBLIC data. Ordinary runtime context defaults to INTERNAL and cannot silently be relabeled public to make NVIDIA tests pass. No successful NVIDIA desktop-reasoning path is claimed.

## BACKGROUND TASK LIVE RESULT

One real 22-second native wait received an ACK in **3.311 seconds**, appeared in the control-state API task activity and delivered the final result after **46.383 seconds**. The parent request identity remained the same; its child evidence contains one successful ui.wait command. No duplicate wait is present in that evidence.

This proves one authenticated text/provider/Windows-agent background path. It does not prove rendered GUI updates, microphone input, spoken final delivery or repeatable current-version reliability. A later retest failed at model capability selection.

## RESTART RECOVERY RESULT

**NOT VERIFIED LIVE.** The benchmark did not get a durable task to the intended safe restart point: earlier attempts hit an isolated-runner provider-restoration bug, and the corrected runner's separate attempt rejected an unregistered capability. No actual restart_performed event was recorded for that task.

Lifecycle process recovery and task/session recovery component tests pass, but no side-effect replay / incomplete-action marking claim is made for the missing live durable-task experiment.

## REMAINING LIMITATIONS

1. Forty-nine historical contract tests still need individual migration and preserved behavioral assertions.
2. Live model decisions are unreliable for app/UI/folder orchestration; safe rejection is not task success.
3. Active context/background language heuristics remain.
4. The current final code has no complete fresh live benchmark after the privacy repair.
5. Durable restart recovery, remote/offline-device handoff, native-action approval and interruption of dispatched actions lack full live proof.
6. Microphone/STT/TTS and rendered desktop GUI activity were not tested by this runner.
7. Free-provider quota/context limits remain real; local compression and bounded waiting cannot eliminate provider failures.
8. Ten-dimensional benchmark evidence is incomplete where arguments/observations were not recorded. Error and partial cases remain acceptance failures.

Production cortex.db SHA-256 matches the pre-run baseline: 21643cdbb2ab8f824ad3f3b3a095da5ccc7614cf7dee9eb009ec3461347f2d14. Existing unrelated work was retained.

## PRE-A11 STABILIZATION COMPLETE YES/NO

**NO**

## READY FOR A11 YES/NO

**NO**
