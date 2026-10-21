# Blind benchmark blockers: follow-up repair

Date: 2026-10-19. A11 was not started. No commit or push.

## What stopped the cases

| Blocker | General repair | Evidence and limit |
|---|---|---|
| Temporary startup window returned as a durable launch target | Launch now observes the same window identity for one second within its existing five-second deadline. A disappearing startup handle is not returned. The application is not launched again. | Native launch succeeded; a new regression simulates a transient handle followed by a stable replacement and asserts exactly one launch. |
| Windows host mistaken for its hosted app | New native ownership resolver uses child-window process IDs for an ApplicationFrameWindow. Only one visible content process is accepted; ambiguity or unreadable ownership retains the host. No window-title app guessing was added. | Actual native receipt: ApplicationFrameHost host PID 16096, CalculatorApp content PID 33756, same window 3410288, 52 accessible controls. This is component proof, not a model-driven task pass. |
| Missing requested window returned as a successful Unknown observation | UI observation returns window_not_found without input. AgentTurn can reacquire the same app's window through native focus and then observe it, rather than inspecting an unrelated foreground window or relaunching. | Focused regression covers missing-window failure; model-driven recovery remains unverified live. |
| Offline benchmark cancelled during quota wait | Cancellation can now require an actual child WAITING receipt for the exact offline device. The prompt is unchanged; the fixture waits for the condition it was meant to test. | Labelled orchestration retest: RUNNING -> WAITING -> CANCELLED, waiting_device=benchmark-offline, reason says no action was sent. No substitute device or native launch was used for that case. Physical reconnect completion was not tested. |
| Provider cooldown ignored protocol reset durations | Parse response durations such as 1m2.5s; honor the appropriate reset header when Retry-After is absent. Daily token limits do not use minute-reset headers as a promise of recovery. | Focused HTTP fixtures cover durations, request/token reset selection, Retry-After precedence, daily-versus-minute separation and provider-body redaction. Desktop actions are never retried. |
| Provider errors lacked actionable diagnostics | Record allowlisted error codes and numeric quota scope/limit/used/requested fields. Never log the raw provider message. | Actual TPD responses below explain why small public calls succeeded while full runtime calls failed. |

The earlier fixture-sharing, CPU-sampling and execution-receipt repairs remain in place; their four-case live retest is described in close49-report.md. They are not new blind samples here.

Native window ownership uses Microsoft's documented [EnumChildWindows](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enumchildwindows) and [GetWindowThreadProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid). The Windows frame class is an OS identity path, not natural-language application routing.

## Actual external quota blocker

The diagnostic run received HTTP 429 with **tokens per day (TPD)**, not just minute throttling:

| Model | Daily limit reported | Used reported | Tokens requested by rejected call |
|---|---|---|---|
| Groq GPT-OSS | 200,000 | 196,134 | 4,068 |
| Groq Qwen | 200,000 | 198,277 | 5,389 |

These are observed provider responses, not locally assumed quota rules or a live account balance. Tiny public probes fit the remaining allowance; a full call did not. No reset time is promised. Live retries stopped after diagnosis. No payment, alternate account/key, privacy downgrade or silent NVIDIA substitution was used to force a pass.

Evidence: task artifacts work/pre-a11-close49/quota-diagnostic-retest-v7/hub.log and results.json. These labelled retests are failed provider-dependent cases; they do not establish new acceptance scores.

## Validation

Final consolidated run: **585 passed, 0 failed, 0 skipped, 0 xfailed**, two dependency warnings, 128.93 seconds. Evidence: final-v6.xml / final-v6.txt in the task artifact directory. The original 49 closure node IDs are all present and passing.

An earlier sandbox run had two TLS transfer failures at the atomic hard-link completion step. The full run outside the sandbox passed both with the no-overwrite protection unchanged. Transfer production code and assertions were not weakened.

One existing launch test changed because a single visible-window poll is no longer the readiness contract: it now advances a fixture clock and verifies repeated stable-window observations while preserving the process-before-window and correct returned identity assertions. New tests cover native host ambiguity, disappearing startup windows and no duplicate launch. Explicit model fixtures remain wiring tests, not language-comprehension claims.

## What is still unproven

- The latest completed fresh blind benchmark remains **12/18**. There is no new full fresh acceptance result after these repairs.
- Calculator identity/readiness works locally, but full model-driven mode selection has no passing live receipt. A separate native menu probe clicked the observed navigation button but did not uniquely identify the requested mode node, so it is explicitly not credited as complete.
- Offline retention/waiting is proven in an isolated transport fixture; reconnect completion on another physical machine is not.
- Daily allowance must replenish before another meaningful full provider-driven run. Compression and bounded waiting cannot guarantee enough daily tokens.
- Notification result privacy, active context/collection heuristics, rendered GUI and microphone paths retain the limitations disclosed in close49-report.md.

Production data/cortex.db hash remains 21643cdbb2ab8f824ad3f3b3a095da5ccc7614cf7dee9eb009ec3461347f2d14.

**ALL BLIND CASES VERIFIED: NO.**

**READY FOR A11: NO.**
