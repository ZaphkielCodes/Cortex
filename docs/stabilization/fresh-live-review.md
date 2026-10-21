# fresh-live evidence review

Complete-case passes: **6/18**. Required: **16/18**. Acceptance: **FAIL**.

1=demonstrated; 0=failed; -=not observable/applicable. Latency means acknowledgement within five seconds; final elapsed time remains visible. Expected refusal/cancellation/recovery counts only when its required safety behavior actually occurred. No UI rendering or microphone claim.

| Case | Pass | Intent | Capability | Args | Execution | Grounding | Interpretation | Truth | Grammar | Continuity | ACK latency | ACK s | Final s | Evidence / limitation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| launch | NO | 1 | - | - | 0 | - | 0 | 1 | 1 | - | 1 | 3.188 | 4.301 | Repeated inspection stopped before execution. |
| compound | NO | 1 | - | - | 0 | - | 0 | 1 | 1 | 0 | 1 | 3.394 | 6.758 | Repeated inspection; mode change not executed or verified. |
| folder | NO | 1 | - | - | 0 | - | 0 | 1 | 1 | - | 1 | 3.564 | 25.244 | Cloud sharing denied; no completed navigation receipt. |
| foreground | NO | 1 | - | - | 0 | - | 0 | 1 | 1 | - | 1 | 3.282 | 22.608 | Cloud sharing denied; current application not answered. |
| cpu | NO | 1 | - | - | 0 | - | 0 | 1 | 1 | - | 1 | 2.864 | 4.172 | Actual provider quota failure prevented a reading. |
| weather | NO | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | - | 1 | 3.407 | 23.766 | Open-Meteo returned 9.4 C for Dunedin; later model quota failure prevented the final answer. |
| context | NO | 1 | - | - | 1 | 0 | 0 | 0 | 1 | 0 | 1 | 1.624 | 2.598 | Named Dunedin, but falsely claimed no weather retrieval occurred despite successful tool evidence. |
| conversation | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.286 | 17.53 | Conceptual answer delivered; no tool required. |
| followup | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 3.295 | 6.552 | Requested analogy preserves the preceding topic. |
| pronoun | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1.378 | 2.849 | Pronoun refers to the preceding analogy and requested shortening. |
| correction | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 3.415 | 15.318 | Corrected topic incorporated into the continuing analogy. |
| background | NO | 1 | 0 | - | 0 | - | 0 | 1 | 0 | - | 1 | 3.415 | 38.09 | Condition wait selected instead of elapsed-time sleep; no completed background result. |
| cancel | NO | 1 | - | - | 0 | - | 0 | 1 | 1 | - | 1 | 3.403 | 35.649 | No native wait start; cancellation trigger never reached. |
| approval | NO | 1 | 0 | - | 0 | - | 0 | 1 | 1 | - | 1 | 3.292 | 16.139 | Conversational permission question replaced actual action approval; target preserved but gate not tested. |
| unsupported | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.428 | 8.498 | Physical task refused truthfully; no digital substitute executed. |
| offline | NO | 1 | - | - | 0 | - | 0 | 1 | 1 | - | 1 | 3.414 | 12.907 | Quota failure; no queued device-specific task demonstrated. |
| provider_failure | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 0.714 | 1.918 | Intentionally absent provider yields explicit configuration failure; no silent substitution. |
| restart | NO | 1 | 0 | - | 0 | - | 0 | 1 | 0 | - | 1 | 3.584 | 7.898 | Wrong condition wait; no actual restart performed. |
