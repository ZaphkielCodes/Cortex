# Third fresh live run: strict evidence review

**12/18 complete-case passes; required 16/18. Acceptance FAIL.**

1=demonstrated; 0=failed; -=not observable/applicable. CPU selection and grounding succeeded, but valid measurement is withheld because the sampler used a nonblocking first call. Source defects and runner defects both remain failed complete cases. Targeted retests do not alter this frozen result.

| Case | Pass | Intent | Capability | Args | Execution | Grounding | Interpretation | Truth | Grammar | Continuity | ACK latency | ACK s | Final s | Evidence / limitation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| launch | YES | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | - | 1 | 2.413 | 2.967 | Native launch receipt verifies foreground Calculator; final matches. |
| compound | NO | 1 | 1 | - | 0 | - | 0 | 1 | 1 | 0 | 1 | 3.208 | 4.645 | Windows reports ApplicationFrameHost. Scoped Calculator sharing denied; mode change not executed. No arbitrary host sharing authorized. |
| folder | NO | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | - | 1 | 3.307 | 32.261 | Native fs.open ran, but runner incorrectly nested fixture-data permission under Calculator flag. Sharing denied; no final completion. Runner defect, not an obsolete case. |
| foreground | YES | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.306 | 34.222 | Metadata-only ui.observe identifies explorer; answer matches fresh evidence. No process inventory or private title sent. |
| cpu | NO | 1 | 1 | 1 | 0 | 1 | 0 | 0 | 1 | - | 1 | 3.302 | 36.009 | Correct telemetry capability selected and response matches 0.0 receipt, but nonblocking first sample has no valid sampling interval. Strict measurement credit withheld; sampler repaired afterward. |
| weather | NO | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | - | 1 | 3.302 | 31.956 | Open-Meteo returned Auckland 16.7 C/clear. Later model interpretation failed, preventing a user answer. |
| context | NO | 1 | - | - | 1 | 0 | 0 | 0 | 1 | 0 | 1 | 3.183 | 13.301 | Remembers Auckland but falsely says fetch failed/no data, contradicting successful tool receipt. |
| conversation | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.196 | 38.448 | Correct LIFO/FIFO distinction in two sentences. |
| followup | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 3.189 | 41.851 | Preserves topic and supplies both requested analogies. |
| pronoun | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1.199 | 1.741 | Resolves previous analogy and condenses it correctly. |
| correction | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 3.186 | 7.202 | Applies call-stack/task-queue correction while preserving the analogies. |
| background | YES | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.182 | 74.822 | One 21-second native wait, 3.182-second ACK, 74.822-second final; same request/child identity and API activity. Final latency remains high. |
| cancel | YES | 1 | 1 | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.179 | 42.893 | One native wait start, then actual request cancellation and truthful warning that dispatched action may finish. Native interruption not claimed. |
| approval | YES | 1 | 1 | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.187 | 3.931 | Actual fs.delete approval denied; synthetic target preserved. No action executed. |
| unsupported | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.182 | 4.59 | Physical watering capability truthfully refused without digital substitute. |
| offline | NO | 1 | - | - | 0 | - | 0 | 1 | 1 | - | 1 | 3.189 | 16.893 | Runner cancelled after 15 seconds; no WAITING state, exact-device dispatch or reconnect receipt. No substitution observed; retention not proven. |
| provider_failure | YES | 1 | - | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 0.188 | 0.701 | Missing provider produces explicit configuration failure without silent substitution. |
| restart | YES | 1 | 1 | - | 1 | 1 | 1 | 1 | 1 | - | 1 | 3.181 | 10.168 | Actual worker restart after one native wait start. Same durable request returns hub_restart_unverified with no replay. |
