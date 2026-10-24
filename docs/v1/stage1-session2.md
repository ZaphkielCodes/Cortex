# Stage 1 Session 2 — retry safety, accounting and benchmark handoff

Date: 2026-10-24. STAGE_1 = IN_PROGRESS. STAGE_2 = NOT_STARTED. No paid model calls, purchases, provider activation, billing changes or model winner. The owner subsequently authorized committing and pushing the stage changes to GitHub for cloud Codex continuation.

## Implemented

- Shared AgentTurn correction scope: one corrective proposal, same recipient/model/privacy, then truthful termination. Rejected proposals never dispatch. Existing task deadlines, approval-route checks and bounded transient transport retries remain. Unsupported capability/device details are preserved on termination.
- Persistent version 2 accounting: daily and total experiment caps, atomic admission, per-task totals, versioned policy/rate snapshots, per-transport attempts, reasoning-token accounting, conservative unknown/cancel/crash reservations and offline evidence-backed reconciliation.
- Direct Groq/NVIDIA transports and non-streaming Gemini SDK are metered in opt-in budget mode. Hidden Gemini SDK retries are disabled in that mode; explicit permitted 503 recovery is separately charged. Unsupported adapters/streaming and incomplete rate contracts fail closed. No live configuration was installed.
- Executable synthetic benchmark via the production single-agent pipeline, 100 reproducibly scheduled trials/model, separate held-outs, reviewable evidence, cost exports and offline scoring. Definitions and rubric remain fixed. Preparation was exercised without model calls. Model-quality and positive complete-suite execution remain unvalidated.

## Frozen compatibility and regression migration

The unavoidable AgentTurn/router issue was documented in Session 1 and the Session 2 instruction authorized its narrow repair. Only hub/agent_turn.py and hub/direct_router.py change among eleven frozen files. Nine others remain byte-identical. Before/after SHA-256 values and compatibility rationale are saved separately; no A14 changes.

Initial consolidated run: 978 passed, 9 failed, zero skipped/xfailed. Two are the existing peer-transfer failures. Four failures exposed lost unsupported/unavailable details in the new early stop; implementation was repaired to preserve those existing assertions unchanged. Three expectations conflicted with the explicitly requested new contract and were migrated:

1. tests/test_stabilization_contracts.py::test_repair_budget_exhaustion_never_dispatches now requires exactly two proposals and corrective_retry_exhausted instead of four proposals/recovery_exhausted. No-dispatch remains asserted.
2. tests/test_compound_recovery.py::test_repeated_invalid_capabilities_stop_after_one_correction similarly requires two proposals with no execution. Independent execution-recovery budgets remain tested elsewhere unchanged.
3. tests/test_compound_recovery.py::test_corrective_provider_failure_keeps_receipts_without_recipient_change requires truthful provider failure, no alternate recipient call and exactly the already completed launch. Receipt preservation, parent identity and routing identity assertions remain. Its old expectation that a correction silently used another provider is expressly prohibited by the new requirement.

Session 1 cost fixtures were upgraded to configuration version 2 because total allowance and versioned policy/rates are mandatory. The unmetered SDK rejection test now names an unsupported SDK rather than Gemini, which has a metered boundary. Rejection assertions were retained. No pre-existing failure was skipped, xfailed or hidden.

## Test evidence

Focused retry/accounting/router/AgentTurn/provider/benchmark run: 140 passed, zero failed, one dependency deprecation. The targeted repaired-behavior run passed 117 tests. Final consolidated regression: 985 passed / 2 failed / 0 skipped / 0 xfailed. Exact machine-readable evidence is in stage1-session2-results.json. The two peer-transfer failures remain unchanged from Stage 0/Session 1 and are not claimed fixed. The consolidated manifest retains all prior cases and adds 19 Session 2 checks. Exact final results must be read from that file; the earlier 966/2 Session 1 record remains unchanged.

Offline evidence includes atomic concurrent admission, total cap across midnight/restart, crash reservations, once-only audited reconciliation, immutable experiment policy, same-provider correction, no dispatch for rejected JSON/arguments, privacy/recipient rejection, two real adapter mock-HTTP fallback attempts with separate charges, Gemini thought-token accounting and one SDK invocation with hidden retries disabled. Mocked transports are explicitly not real-provider success.

Benchmark preparation wrote a 100-case schedule; scorer correctly reported zero recorded/completed cases, 100 missing, selection_ready=false. Smoke test used a deliberately failing offline provider with the real RequestPipeline; it supplied no successful model decision. No live benchmark results exist.

## Files in this session

Runtime: hub/decision_retry.py, hub/cost_accounting.py, hub/agent_turn.py, hub/direct_router.py, hub/direct_provider.py, hub/llm_provider.py, hub/tracing.py.
Benchmark: scripts/evaluate_v1.py, scripts/score_v1.py, benchmarks/v1/fixtures.json, held-out.json and freeze.json. Session 1's benchmarks/v1-single-agent-20.json is unchanged.
Tests: tests/test_v1_session2.py, tests/test_v1_benchmark.py, tests/test_v1_cost_accounting.py and the three assertion migrations in tests/test_stabilization_contracts.py and tests/test_compound_recovery.py.
Documentation: current accounting, benchmark runner, compatibility and evaluation/provider update notes; Session 2 results/manifest/hash records; cloud-handoff.md and this report. Session 1 changes to README and roadmap are included in publication, not discarded.

## Remaining requirements before paid evaluation and selection

Review positive synthetic fixture paths for all 20 cases, including authorization denials, context links and cancellation. Validate the exact eligible provider/model's text/reasoning usage contract, request ceilings, account capacity, pricing expiry and private-data terms. The current fixture runner is executable but not proven end-to-end across every case. Google/device fixtures are bounded simulations and require argument/receipt adjudication, not automatic semantic pass labels. Held-out execution remains separate and not scheduled by the main runner.

Obtain explicit funding/provider/daily/total-cap authorization; initialize one durable ledger and reviewed rate card shared by every evaluation process. Verify cloud environment dependencies and report Windows-only acceptance limits. No approval to run paid trials is implied by publishing this code. After authorized trials, reconcile unknown charges, complete independent scoring, apply all predeclared selection gates and held-outs. Stage 1 stays open until measurements pass; Stage 2 has not begun.

A11 live acceptance remains blocked, pre-A11 compound validation open, A12/A13 foundations complete, A14 implementation frozen/live acceptance open, A12.2 deferred. No production-readiness claim or replacement Goal Meter.
