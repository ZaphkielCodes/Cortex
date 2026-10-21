# A10 final closure: acceptance gate before A10.5

## A10 FINAL STATUS

A10 implementation is complete and frozen. Healthy-provider live acceptance remains external validation debt.
This closure supersedes the earlier combined implementation/live-readiness verdict. Broad A10 refactors are frozen after a clean applied regression. No A10.5 or A11 work is included. A10 may reopen for a real correctness defect exposed by healthy-provider acceptance.

## LIVE CASE CLASSIFICATION

Audited original `scratch/compound-acceptance-20261019-214318/results.json`, agent/hub logs, and a local read-only copy of its SQLite database plus WAL. Original evidence is preserved, not relabelled in place.

| Case | Classification | Evidence |
|---|---|---|
| layout_change | INCONCLUSIVE_PROVIDER_BLOCKED | Actual AgentTurn PLAN; one native Calculator launch completed; command 23bf9e00a449414c8bc40db1ca5f4758 persisted; next reasoning turn failed with RATE_LIMIT_429. No observe/ground/verify compound sequence was completed. |
| canvas_dimensions | NOT_EXECUTED_PROVIDER_BLOCKED | PROVIDER_COOLDOWN before any capability dispatch; empty receipts. |
| unsent_document | NOT_EXECUTED_PROVIDER_BLOCKED | PROVIDER_COOLDOWN before any capability dispatch; empty receipts. |

No case is a live PASS or a demonstrated compound-UI FAIL. Quota evidence was limit 200000 TPD, used 196395, requested 5982. This is evidence from that run, not a current quota check. Frozen prompt manifest SHA256 matches the audited run.

## REAL DEFECTS FOUND

1. Returned structured provider failures bypassed the harness exception classifier and became generic HARNESS_ERROR telemetry. This was an observability defect, not lost execution state.
2. The compact three-case report inherited an impossible minimum of 16 complete cases from the 18-case benchmark. Its outcome fields still required manual review, but the reported threshold was misleading.

No demonstrated stale-input acceptance, false semantic completion, lost receipt, parent mismatch, or duplicate OS dispatch was found. This limited run cannot prove arbitrary compound UI reliability.

## FIXES MADE

- `hub/agent_harness.py`: final error telemetry uses AgentTurn's structured primary provider category when error_code=model_provider_unavailable. Ordinary action/harness failures retain their existing classification. Response status/message, routing, cooldowns, dispatch, recovery, and retries are unchanged.
- `scripts/live_benchmark.py`: metadata counts selected cases and computes the existing 16/18 acceptance ratio's integer minimum. The three-case gate therefore requires 3/3, the full 18-case gate remains 16/18, and a one-case subset requires 1/1. Prompts and runtime execution are unchanged.
- `tests/test_a10_closure.py`: eleven additional offline checks. No existing tests migrated, deleted, skipped, or weakened.

No application recipes, phrase routing, canned completion handlers, provider routing redesign, or failover feature was added.

## PROVIDER FAILURE SAFETY

Scripted providers exercise the real harness/AgentTurn path: launch succeeds -> durable receipt -> next-turn RATE_LIMIT_429/PROVIDER_COOLDOWN/SERVICE_UNAVAILABLE_503 -> WAITING_FOR_PROVIDER, with exactly one dispatch. Before-first-action RATE_LIMIT_429/PROVIDER_COOLDOWN/PROVIDER_NOT_CONFIGURED causes zero OS dispatches, truthful messages, empty receipt history, and the same provider-wait stage. Existing compound tests also cover explicit safe resume and no replay.

These are mechanical contract tests, not live model-language validation. The regression network guard prevents external DNS/connections while allowing local TLS transfer tests.

## DUPLICATE EXECUTION CHECK

Live layout_change: one EXECUTING trace entry, one app.launch receipt, one native command_started and command_finished pair for the same command ID. Other cases: zero dispatches. No duplicate execution is observed. Focused tests retain exactly-once assertions and the prior resume-without-replay coverage.

## EXECUTION RECEIPT CHECK

The launch receipt survived provider failure in the live database, exported child evidence, and parent result data. It records ran=true, side_effects_possible=true, retry_safe=false, actual device benchmark-windows, command ID and task/parent IDs. Dispatch success verified only launch/window identity; it never claimed scientific mode or whole-task completion.

## TASK STATE CHECK

All three persisted compound checkpoints have current_stage=WAITING_FOR_PROVIDER. The layout child is turn_6d99c0237ad7487f8aeca6820eddcc83 with parent request_184b78a7fb91676efa4aa8da. The other two child/parent pairs also match, as recorded in live-evidence-audit.json.

The request and database task rows are FAILED because that attempt returned status=error. This is separate from the durable compound checkpoint's provider-wait stage and explicit resumability. It does not mean the UI action failed, that the task is currently executing, or that an automatic retry is scheduled. This closure intentionally leaves those execution/status semantics unchanged.

## TELEMETRY CLASSIFICATION

New attempts report RATE_LIMIT_429 or PROVIDER_COOLDOWN for those returned provider failures, rather than HARNESS_ERROR. Primary/fallback error details remain intact. Non-provider action errors are not promoted merely because they contain a category-shaped field. Historical raw traces remain unchanged; the audit supplies the corrected interpretation.

## FULL REGRESSION RESULT

**621 passed / 0 failed / 0 skipped / 0 xfailed.** All 610 baseline identities remain passing; 11 new checks. External networking is guarded. Evidence: `C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a10-final-closure\applied-full.xml` and `applied-full.txt`. No existing test migration was needed.
The consolidated manifest covers A1-A10, architecture repair, AgentTurn, GUI/runtime controls, background/recovery, context compression, privacy, provider routing, peer transport and native input boundaries. The original 610 test identities must all remain passing. Final XML/log, baseline comparison, evidence hashes and database checksum are saved under the closure artifact directory.

The staged copy's first diagnostic run lacked its synthetic production-database isolation sentinel; this caused isolation-fixture failures and a skip. It is not a valid closure run. Final acceptance uses the actual repository and its existing production database isolation guards; no production database content is copied into the staging area.

## LIVE ACCEPTANCE STATUS

LIVE_COMPOUND_ACCEPTANCE = PENDING_PROVIDER_CAPACITY

No new live provider call or Windows acceptance run is made in this closure task. Only the acceptance script's ValidateOnly path is run. Existing prompts and the PowerShell gate are preserved.

After provider capacity is healthy, run exactly once from the repository:

```powershell
.\scripts\run_compound_acceptance.ps1
```

If local script policy requires it, the process-scoped invocation is `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_compound_acceptance.ps1`. This does not set a machine-wide policy.

This runs real authenticated GUI text input -> AgentTurn -> actual configured provider -> Windows agent -> OS actions and exported operational traces. Manual model-sharing approvals remain required, destructive approvals remain denied. Use blank test documents. Human review must verify all three final semantic conditions, intermediate observation/grounding/readback, exactly-once effects, and preserved task identities. Provider errors are not UI failures and process/HTTP success alone is not a semantic pass.

## VALIDATION DEBT

One healthy-provider three-case compound acceptance run remains outstanding. Record real semantic completion and no replay for all three cases; preserve traces and any failure evidence. Provider availability/routing improvements belong to separately authorized A10.5 work. This external validation debt does not require more broad A10 architecture changes unless a healthy run exposes a real defect.

## A10 IMPLEMENTATION COMPLETE YES/NO

YES.

## READY TO START A10.5 YES/NO

YES — begin only in a separately authorized task.
A10.5 has not been started. Readiness here is permission to plan the next development phase after this closure, not a claim that live compound acceptance passed or that A11 is ready.

Production database SHA256: `21643cdbb2ab8f824ad3f3b3a095da5ccc7614cf7dee9eb009ec3461347f2d14`. Live evidence, manifest, and PowerShell gate were preserved byte-for-byte. No commit/push was performed.
