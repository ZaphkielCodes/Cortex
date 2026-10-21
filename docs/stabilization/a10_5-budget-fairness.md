# A10.5 failover budget fairness and final live retest — 2026-10-20

## BUDGET FAIRNESS ROOT CAUSE

The existing TurnRoute gave each background attempt up to 45 seconds while sharing a 60-second active reasoning deadline. An early timeout could leave only 15 seconds for a second model and no invocation for a third. Ranking eligibility was conflated with actually attempting an inference. This was a general scheduling defect, not evidence that the uninvoked model was unavailable.

## GENERAL FIX

A small pure allocator shares the existing deadline across the remaining ranked candidates and remaining attempt slots. It reserves meaningful later windows, apportions surplus by latency, and recalculates after every failure. No provider order was forced, and no provider-specific retry or model substitution was added. Lock waits and event overhead now consume the same reasoning deadline. Existing privacy, cooldown, MAX_ATTEMPTS, recipient approval and no-replay boundaries remain intact.

## NEW BUDGET POLICY

- Interactive: 2-second meaningful floor and existing 12-second per-attempt cap; the configured lane's total deadline still applies (30 seconds by default, 6 in FAST_RESPONSE).
- Background: 10-second meaningful floor and existing 45-second per-attempt cap; the existing background active deadline remains 60 seconds by default.
- Count only eligible ranked candidates that fit remaining meaningful windows and remaining attempt slots. A sole viable candidate can use the larger cap. Earlier unused time remains in the shared pool.
- Reserve the floor for each later candidate plus at most 50 milliseconds per later candidate for bookkeeping. Split surplus using weights from observed successful latency, otherwise a configured qualitative latency prior. Weights are 1.5 times latency bounded by lane floor/cap.
- Unknown latency stays None. It receives a neutral scheduling weight of twice the floor, not a fabricated measurement. The ranking's prior neutral penalty stays numerically unchanged; actual ranking remains authoritative.
- The original bounded explicit Retry-After wait allowance is preserved. It can extend the active deadline only by the already-accounted safe wait; this probe encountered no such wait. No new retry loop was introduced.
- If a meaningful attempt cannot fit, record NOT_ATTEMPTED_BUDGET_EXHAUSTED and NO_USABLE_RESPONSE_WITHIN_FAILOVER_BUDGET. An uninvoked candidate is never counted as a failed external provider. Actual inference failures separately report PROVIDER_TIMEOUT, PROVIDER_ERROR or PROVIDER_UNAVAILABLE.

## HARDCODING CHECK

hub/failover_budget.py contains no provider/model names, user prompt matching, app routing, or scenario handlers. Constants describe task-lane scheduling policy. Production changes are limited to the allocator, its TurnRoute integration and metadata, and representing unknown profile latency without changing the existing neutral ranking score. No A10 executor, compound state machine, receipts, action replay, prompts or recovery code changed. No A11 work occurred.

## NEW TESTS

20 additional offline checks cover three fallbacks after synthetic primary quota exhaustion, hanging first/second candidates, early-failure redistribution, a sole candidate's larger allowance, interactive/background ceilings, observed slow latency, unknown/prior distinction, invalid/tiny budgets, attempt limits, explicit uninvoked-budget classification, bounded lock waits, actual asyncio cancellation under a real deadline, no retry loop, and persisted task/parent identity. All external networking remained forbidden by the unchanged tests/conftest.py guard.

## FULL REGRESSION

**688 passed / 0 failed / 0 skipped / 0 xfailed**, 156.52 seconds, two existing dependency deprecation warnings. All 668 baseline test identities retained, plus 20 new checks. The manifest contains the same consolidated A1–A10/runtime/provider/privacy suite plus tests/test_failover_budget.py; no baseline checks removed or assertions weakened.

The first focused sandbox run produced 106 passes and five fixture setup errors because pytest could not allocate its sandbox temporary directory. The complete rerun used an explicit fresh temporary path and the filesystem access needed by existing local peer tests. It passed cleanly with the external-network guard enabled. No test was skipped or xfailed to obtain this result.

Evidence: regression.xml, regression.txt, regression-summary.json and suite-manifest.json in this evidence directory.

## LIVE CANDIDATE ORDER

Exactly one controlled retest ran after the clean offline gate. Groq's transport was replaced locally by a synthetic provider-wide TPD 429 response, for every Groq route. Zero real Groq requests were sent. Existing configured profiles, canonical ranking, privacy policy and attempt limits selected the alternatives naturally. A one-shot marker prevents automatic repetition.

Initial order: groq/qwen/qwen3.8-27b → groq/openai/gpt-oss-120b → gemini/gemini-3.8-flash → nvidia/deepseek-ai/deepseek-v4.1-flash → nvidia/z-ai/glm-5.3.

Actual selected order: groq/qwen/qwen3.8-27b → gemini/gemini-3.8-flash → nvidia/deepseek-ai/deepseek-v4.1-flash → nvidia/z-ai/glm-5.3.

Initial scores, rejection reasons and all subsequent ranked/rejected candidates are preserved in live-retest.json. Health and task storage are isolated from production.

## ATTEMPT BUDGETS

| Provider/model | Type | Allocated seconds | Actual seconds | Outcome |
| --- | --- | ---: | ---: | --- |
| groq/qwen/qwen3.8-27b | local synthetic | 11.217 | 0.004 | PROVIDER_UNAVAILABLE |
| gemini/gemini-3.8-flash | real inference | 19.875 | 3.548 | PROVIDER_ERROR |
| nvidia/deepseek-ai/deepseek-v4.1-flash | real inference | 26.157 | 26.169 | PROVIDER_TIMEOUT |
| nvidia/z-ai/glm-5.3 | real inference | 30.194 | 14.802 | RETURNED |

Total wall-clock reasoning latency: **44.652 seconds**.

## PROVIDER RESULTS

**LIVE_ACCEPTANCE = SUCCESS**.
- groq/qwen/qwen3.8-27b: DAILY_QUOTA_EXHAUSTED / RATE_LIMIT_429, scope provider, HTTP 429.
- gemini/gemini-3.8-flash: PROVIDER_SPECIFIC_FAILURE / SERVICE_UNAVAILABLE_503, scope provider, HTTP 503.
- nvidia/deepseek-ai/deepseek-v4.1-flash: TIMEOUT / DEADLINE_EXCEEDED, scope model, HTTP not received.
- nvidia/z-ai/glm-5.3: model result returned; usable answer check passed.

No prompt, response text, credentials, hidden reasoning or sensitive provider body was recorded. Response verification uses only a valid-response flag, length and hash. Timeout classifications come from the canonical router after its outer asyncio timeout converts cancellation to TimeoutError. No repeated live diagnostic run was made.

## FINAL PROVIDER/MODEL

nvidia / z-ai/glm-5.3

## FINAL RESPONSE RECEIVED

**YES**. Valid response: YES. Callback count: 1.

## TASK IDENTITY PRESERVED

Task ID: `fairness_ddbc0a459fd44b5daaf13f8e94784181`

Parent ID: `parent_3e3f841fcb5e469e834b574b7c104df9`

SAME_TASK_ID = YES; SAME_PARENT_ID = YES. Every routing event and the persisted final task were checked. No replacement task was created on provider failure.

DUPLICATE_EXECUTION = NO. Zero OS/capability executions were possible in this reasoning-only probe. This is routing/task persistence evidence, not full live AgentTurn/desktop replay acceptance.

## PRIVACY PRESERVED

**YES.** Only synthetic PUBLIC arithmetic input was sent; no desktop observations or personal data were involved. Provider eligibility and recipient binding remain unchanged. Production DB, network test guard, A10 compound manifest and launcher hashes match their pre-change snapshots.

## A10.5 COMPLETE YES/NO

**YES.** Offline gate and controlled real cross-provider response acceptance passed.

## COMPOUND ACCEPTANCE RESULT

FAIL — 0/3 complete-case semantic passes; original launcher ran once.
- layout_change: FAIL: a launch receipt reported success, but the scientific layout and preserved display were not verified. The task stopped because it repeated an action without verified progress.
- canvas_dimensions: FAIL: the observed foreground window belonged to an unrelated phone-call application, not Paint. Model sharing was denied at the manual gate. No 640-by-480 canvas verification completed.
- unsent_document: FAIL: the task ended with invalid_capability_call. The requested two-line unsaved draft and its text verification were not completed.

The unchanged launcher ran exactly once, with its original three prompts and manual sharing gates. Runner exit code zero means evidence collection completed, not semantic acceptance. The detailed semantic review is in compound-review.json; the original results.json is unchanged. No desktop action was performed by the reviewer to improve a test outcome.

## READY FOR A11 YES/NO

**NO.** A11 was not started.

## REMAINING VALIDATION DEBT

A10 compound acceptance must pass semantic review before A11 readiness. This run identified repeated actions without verified progress, an observation of an unrelated foreground window after a requested app launch, and an invalid capability proposal. Those are compound runtime/model-quality validation debts; they were not patched or retested in this focused budget repair. No A11 readiness is claimed.

No credentials changed; no production runtime restart, commit or push performed. Earlier evidence remains intact.
