> Historical benchmark snapshot. Current reviewed results: [first fresh run, 6/18](fresh-live-review.md), [second fresh run, 12/18](fresh-live-v2-review.md). See [current report](close49-report.md).

# Blind live benchmark evidence review

The initial frozen-prompt attempt and later retests are separate. Retests are not new blind samples. No aggregate mixes their successes. The privacy classification repair happened after these runs; none proves that repaired boundary live.

The window/process inventory privacy gap observed in a retest blocks acceptance independently of task scores. Titles and paths could enter a cloud reasoning payload without explicit sharing approval before the repair. New denial-boundary tests cover the fix offline.

Scores: 1 demonstrated, 0 failed, - not observed / not applicable. Latency measures ACK <=5 seconds, not final completion. Arguments are unscored where original calls were not persisted.

## live

| Case | Outcome | Intent | Capability | Arguments | Execution | Grounding | Interpretation | Truthful | Grammar | Context | Latency | ACK / final seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| launch | FAIL | - | - | - | 1 | - | 0 | 1 | 1 | - | 1 | 3.351 / 6.544 |
| context | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.341 / 1.244 |
| compound | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.353 / 1.317 |
| folder | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.328 / 1.179 |
| foreground | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.342 / 1.27 |
| telemetry | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.321 / 1.213 |
| weather | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.33 / 1.257 |
| conversation | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.31 / 1.24 |
| followup | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.322 / 1.473 |
| pronoun | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.443 / 1.509 |
| correction | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.417 / 1.429 |
| background | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.379 / 1.378 |
| cancel | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.351 / 1.301 |
| approval | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.349 / 1.29 |
| unsupported | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.336 / 1.258 |
| offline | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.336 / 1.274 |
| provider_failure | PASS | 1 | - | - | - | - | 1 | 1 | 1 | - | 1 | 0.317 / 1.217 |
| restart | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.323 / 1.215 |

Strict complete-case results: 1/18. Partial results do not receive complete-case credit. Acceptance: FAIL.

## live-pacing

| Case | Outcome | Intent | Capability | Arguments | Execution | Grounding | Interpretation | Truthful | Grammar | Context | Latency | ACK / final seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| launch | FAIL | - | - | - | 1 | - | 0 | 1 | 1 | - | 1 | 3.292 / 6.425 |
| context | PASS | 1 | - | - | - | - | - | 1 | 1 | 1 | 1 | 3.418 / 25.472 |
| compound | FAIL | - | - | - | 1 | - | 0 | 1 | 1 | - | 1 | 3.304 / 12.252 |
| folder | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 3.368 / 5.693 |
| foreground | PASS | 1 | 1 | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.551 / 4.585 |
| telemetry | FAIL | - | - | - | 1 | - | 0 | 1 | 1 | - | 1 | 3.568 / 34.859 |
| weather | PASS | 1 | 1 | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.471 / 18.674 |
| conversation | PASS | 1 | - | - | - | - | - | 1 | 1 | - | 1 | 3.475 / 17.152 |
| followup | PASS | 1 | - | - | - | - | - | 1 | 1 | 1 | 1 | 3.531 / 14.583 |
| pronoun | PASS | 1 | - | - | - | - | - | 1 | 1 | 1 | 1 | 3.588 / 16.167 |
| correction | PASS | 1 | - | - | - | - | - | 1 | 1 | 1 | 1 | 3.554 / 13.801 |
| background | PASS | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.311 / 46.383 |
| cancel | PARTIAL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 3.304 / 9.434 |
| approval | PARTIAL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 3.401 / 4.986 |
| unsupported | PASS | 1 | - | - | - | - | 1 | 1 | 1 | - | 1 | 3.408 / 4.974 |
| offline | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 3.411 / 12.219 |
| provider_failure | PASS | 1 | - | - | - | - | 1 | 1 | 1 | - | 1 | 0.59 / 1.761 |
| restart | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 1.856 / 3.4 |

Strict complete-case results: 10/18. Partial results do not receive complete-case credit. Acceptance: FAIL.

## live-final

| Case | Outcome | Intent | Capability | Arguments | Execution | Grounding | Interpretation | Truthful | Grammar | Context | Latency | ACK / final seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| launch | FAIL | - | - | - | 1 | - | 0 | 1 | 1 | - | 1 | 3.508 / 10.452 |
| context | PASS | 1 | - | - | - | - | - | 1 | 1 | 1 | 1 | 1.222 / 2.69 |
| compound | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 1.105 / 2.838 |
| folder | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 3.571 / 6.456 |
| foreground | FAIL | - | - | - | 1 | - | 0 | 1 | 1 | - | 1 | 3.326 / 7.945 |
| telemetry | PASS | 1 | 1 | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.362 / 8.795 |
| background | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 3.389 / 11.079 |
| cancel | PARTIAL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 3.332 / 8.03 |
| approval | PARTIAL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 3.402 / 8.112 |
| unsupported | PASS | 1 | - | - | - | - | 1 | 1 | 1 | - | 1 | 1.67 / 3.517 |
| offline | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 3.605 / 8.761 |
| provider_failure | PASS | 1 | - | - | - | - | 1 | 1 | 1 | - | 1 | 0.541 / 1.957 |
| restart | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 0.99 / 2.271 |

Strict complete-case results: 4/13. Partial results do not receive complete-case credit. Acceptance: FAIL.

## live-restart

| Case | Outcome | Intent | Capability | Arguments | Execution | Grounding | Interpretation | Truthful | Grammar | Context | Latency | ACK / final seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| restart | FAIL | - | - | - | 0 | - | - | 1 | 1 | - | 1 | 2.406 / 3.682 |

Strict complete-case results: 0/1. Partial results do not receive complete-case credit. Acceptance: FAIL.

