> Latest follow-up: [blind benchmark blocker repairs](blind-blockers-followup.md). Final regression is now 585 passed; latest fresh live acceptance remains 12/18.

# Cortex: 49-failure closure and live orchestration

Date: 2026-10-19. No A11 work, commit or push. This report supersedes the earlier stabilization snapshot in report.md.

## Historical failure closure

The original 26 classifications remain: 3 REAL_REGRESSION, 5 OBSOLETE_TEST, 18 TEST_EXPECTATION_CHANGED_BY_NEW_ARCHITECTURE, 0 NONDETERMINISTIC_INFRA_FAILURE. See historical-classification.md for individual explanations.

All subsequent 49 failures are closed: 48 migrated behavioral assertions and one justified obsolete legacy-alias contract replaced with canonical-ID acceptance and alias rejection. Every original test function remains collected and passing. See [49-closure.md](49-closure.md) for each preserved assertion and the reason its old contract changed. Explicit model fixtures prove context, validation, execution, approval and result wiring; they do not prove language comprehension.

## Repairs made during this closure

- Preserved actual executor provenance through dispatch. Source, device, capability and retrieval time come from execution receipts.
- Added concise canonical argument signatures, on-demand full schemas and bounded invalid-proposal repair before execution. Groq uses validated native function proposals; it cannot execute actions directly. No replay of a side effect to repair wording.
- Preserved clarification states and actual screenshot/file/context references. Successful window receipts retain minimal device-scoped identity across turns, never private titles or contents. Fresh observation is still required for UI actions.
- Native launch now waits for a visible window and verifies activation. Unverified focus no longer reports success. Generic executor replies preserve structured paths and verification flags across the WebSocket boundary.
- Advertised enrolled offline devices as offline without granting liveness, authentication or capabilities. Instructions permit exact-device waiting without substitution; live reliability remains incomplete.
- Kept background reasoning alive beyond the interactive acknowledgement deadline, while retaining action limits and bounded provider waits. Actual native start receipts distinguish a queued command from a running command.
- Kept A5-A10 checks, source-scoped privacy, real approvals and ownership checks in the consolidated regression. No prompt-specific app/CPU/weather/folder routing was added.

## Consolidated regression total

Final evidence: work/pre-a11-close49/final-v4.xml and final-v4.txt in the Codex task artifact directory.

| Passed | Failed | Skipped | Xfailed | Warnings | Duration |
|---|---|---|---|---|---|
| 570 | 0 | 0 | 0 | 2 | 61.21 s |

The 38-module consolidated run (the original 37-module manifest plus live-evidence repair tests) covers A1-A10, architecture repair, AgentTurn, GUI/control boundaries, task/recovery, context compression and provider routing. All 49 closure node IDs were checked against the passing XML. Warning categories are dependency deprecations; they are not hidden test failures.

## Hardcoded routing audit

The primary request path has no Notepad/Settings/weather/CPU/Downloads/Calculator phrase branch. This does not mean all natural-language heuristics are gone. [hardcoding-audit.md](hardcoding-audit.md) lists the remaining compatibility trees and active social/session/collection heuristics. Collection and context heuristics remain blockers to an all-hardcoding-removed claim. Exact OS identifiers, schema validation, security, approval and protocol controls remain deterministic.

## Blind live benchmark results

The real authenticated text-control endpoint, model provider, Windows agent and native executor were used in isolated runs. All results belong to their own implementation snapshot. Retests are not new blind samples and successes are never pooled across runs. Each reviewed case records ten dimensions, acknowledgement/final latency and an evidence explanation; unknown dimensions stay null.

| Completed run | Complete-case passes | Required | Outcome |
|---|---|---|---|
| fresh-live | 6/18 | 16/18 | FAIL |
| fresh-live-v2 | 12/18 | 16/18 | FAIL |
| fresh-live-v3 | 12/18 | 16/18 | FAIL |

Reviewed tables: [first run](fresh-live-review.md), [second run](fresh-live-v2-review.md), [third run](fresh-live-v3-review.md). Raw evidence: work/pre-a11-close49/{fresh-live,fresh-live-v2}/results.json, review.json and review.md in the task artifact directory. Raw logs/results are unchanged; separate reviews correct the preliminary 7/18 and 13/18 estimates.

The first run's context answer falsely said no weather was retrieved, although Open-Meteo had returned data before a later model quota failure. It therefore fails continuity/truthfulness. In the second run, CPU returned battery information and explicitly admitted it lacked CPU usage. This is truthful but does not complete the requested measurement, so it fails.

The second completed run passes launch, weather, context, conversation, follow-up, pronoun, correction, cancellation, actual approval denial, unsupported physical request, missing-provider handling and restart recovery. Its six failures were:

| Case | Failure / remaining general issue |
|---|---|
| Compound UI | Unrelated foreground Chrome observed instead of continuing with the named application's window; sharing correctly denied. |
| Folder | Native fs.open occurred, but resource path was dropped by the wire envelope; scoped sharing and final completion failed. Envelope preservation is repaired offline in the latest candidate, awaiting live proof. |
| Foreground | Model selected process inventory instead of minimal foreground metadata. Privacy denial is correct but the question remains unanswered. |
| CPU | Chose battery-only device.status instead of system usage telemetry. No CPU value reported. |
| Background | Native wait ran once; subsequent notification receipt was blocked by PERSONAL cloud policy. Final response failed. No policy downgrade is claimed. |
| Offline | Model claimed queuing was unavailable and never attempted the exact-device wait. It did not substitute another device. |

The user subsequently authorized the third live run, and all 18 cases completed. Its strict score is 12/18: launch, foreground metadata, conversation, follow-up, pronoun, correction, background completion, cancellation, approval denial, unsupported action, provider absence and restart recovery pass. Its six failures are compound UI, folder completion, CPU sampling validity, weather interpretation, context truthfulness and offline retention. Detailed ten-dimensional evidence is in the third review. The original declined invocation is not counted as a run.

The third run's CPU capability and answer agreed, but its 0.0 receipt came from an unprimed/nonblocking sampling path, so strict measurement credit is withheld. Weather returned actual Auckland data before model interpretation failed; the following context answer incorrectly called the fetch unsuccessful. Offline retention was not established before the runner's scheduled cancellation (parent states RUNNING to CANCELLED, without a WAITING receipt).

## Repairs and labelled live retest after the third run

The isolated runner incorrectly required the Calculator permission flag before evaluating fixture-folder sharing. This is repaired without broadening the fixture path, source-device or image restrictions. A generic Windows ApplicationFrameHost is still not treated as proof of Calculator identity; the compound UI gate correctly remains closed.

AgentTurn now carries bounded execution-status receipts separately from the final response status. Session completion and reload preserve these receipts, and reasoning context applies its existing privacy check before exposing them. Receipts contain capability/status, executor, command identity and observation time; they omit arguments, paths, content and images. A provider failure during interpretation cannot erase the earlier execution receipt. Focused coverage proves no repeated execution, persisted failure versus successful action, and omission when context sharing is denied. This is wiring evidence, not a guarantee of every future model answer.

CPU sampling now uses a bounded 0.1-second sample, including on its first call. Sampling exceptions remain unknown and return an explicit unavailable result rather than a fabricated zero. No user-phrase routing was added.

The labelled live-repair-retest reused the four known prompts; it is not a new blind acceptance sample. All four completed: native fixture fs.open with scoped approval and final response, valid CPU sample 14.4%, actual Auckland weather 17.1 C/clear, and correct recall that the fetch succeeded. Acknowledgement/final timings and native arguments are in work/pre-a11-close49/live-repair-retest/results.json. Successful weather in this retest does not itself reproduce a later interpretation failure; that specific failure-continuity path is covered offline.

Current source includes these repairs after the third fresh run. It has 570 passing consolidated checks and a 4/4 targeted live retest, but no subsequent fresh 18-case acceptance run.

## Live provider results

Groq direct native function proposals worked with GPT-OSS and Qwen, including a public full-catalog protocol probe. GPT-OSS's measured probe used 3,531 prompt tokens; Qwen used 4,271. These are one-off observations, not universal usage promises. Real quota failures remain possible. Cooldown/429 waiting is bounded and never retries desktop actions.

Prior stabilization verified one NVIDIA GLM public direct request; a DeepSeek attempt timed out. No successful NVIDIA full-runtime path is claimed here. Missing-provider failure was exercised without silent substitution. Privacy denials in actual runs demonstrate blocking; they do not certify all privacy/provider combinations.

## Background task live result

A labelled signature retest completed one 17-second native wait: acknowledgement 3.411 s, final 40.883 s, one native wait start, same request/child identity. API activity was visible. This is useful background-path proof, not a fresh 18-case acceptance pass. The later fresh-v2 notification completion still failed privacy interpretation, as listed above.

Cancellation in fresh-v2 acknowledged in 3.533 s, reached a cancelled result in 20.526 s after one native wait start, and truthfully warned that an already dispatched action may finish. It does not prove interruption of native sleep.

## Restart recovery result

Fresh-v2 actually restarted both isolated workers after a native wait began. Acknowledgement: 3.709 s; final recovered state: 12.321 s. Exactly one native wait start was recorded. Recovered state reports error_code=hub_restart_unverified and says completion is unverified and automatic action replay is disabled. This is the expected safe recovery outcome, not successful completion of the interrupted wait. The separate signature retest also achieved this proof.

Actual fs.delete approval denial in fresh-v2 returned through the real gate and preserved its synthetic target. No user file was deleted.

## Remaining limitations

- Latest fresh acceptance is 12/18, below 16/18. The subsequent repaired candidate has only the labelled four-case retest, not a fresh full run.
- Microphone/STT/TTS, spoken final delivery and rendered GUI activity were not tested by the text/API runner.
- Offline queuing/reconnect completion, compound UI controls and model capability selection remain unreliable live.
- The PERSONAL notification result policy and Windows hosted-app identity remain unresolved for reliable compound completion; no privacy downgrade was introduced to force a pass.
- No always-on host or Android physical runtime is certified by this Windows benchmark.
- Native wait recovery demonstrates no replay at that safe point, not every possible destructive-action crash boundary.
- Active context/background language heuristics remain as disclosed in the hardcoding audit.

Production data/cortex.db SHA-256 was rechecked unchanged: 21643cdbb2ab8f824ad3f3b3a095da5ccc7614cf7dee9eb009ec3461347f2d14. Existing unrelated work remains untouched by this report.

**PRE-A11 STABILIZATION COMPLETE: NO**

**READY FOR A11: NO**
