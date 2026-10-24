# Stage 1 accounting — Session 2

Status: implemented and tested offline; NOT activated for paid evaluation. Session 1 results remain in stage1-session1.md and stage1-test-results.json. This document supersedes the version 1 configuration notes.

## Coverage and limits

Groq and NVIDIA non-streaming direct HTTP calls reserve before every actual attempt. Gemini non-streaming SDK calls now reserve before each SDK invocation; SDK retry attempts are explicitly one in budget mode, while existing bounded explicit transient recovery is separately metered. Streaming Gemini and unknown SDK adapters are denied in budget mode. NVIDIA's PUBLIC-only restriction remains; it is not an eligible private daily-summary provider. DeepSeek direct has no adapter and remains ineligible. No paid adapter, account or model was activated.

Ledger units are integer nanodollars (1 USD = 1,000,000,000). Each attempt retains provider/model, reservation, charged upper-bound estimate, input/output counts, optional reasoning count, latency, status, price snapshot reference and hashed task identity. Prompts, responses, credentials, mail contents and raw provider errors are excluded. Reasoning tokens are included in completion tokens once for OpenAI-format usage; Gemini output is candidates plus thoughts, checked against total usage. Unsupported, missing or inconsistent usage retains the full reservation.

SQLite BEGIN IMMEDIATE serializes concurrent admission. UTC daily limits include unresolved calls from prior days. A persistent total cap applies across the experiment, across days and restarts. All calls for a given experiment must share one absolute ledger path and immutable policy snapshot. Changing its caps under the same experiment ID is rejected. A new experiment ID is an administrator action, not a model-controlled way to replenish spending. The administrator must not split one experiment across ledgers. Per-task aggregation and content-free per-attempt export include failed and unknown calls.

Admission reserves the full reviewed model input/output ceilings at upper-bound rates. Direct serialized payload bytes and Gemini instruction/content bytes are additional conservative text admission checks, not exact tokenizers. Pricing must cover provider overhead and all billable reasoning at verified model ceilings. No search, audio, image, remote-tool or other surcharges are supported. Cache discounts are not assumed. Estimates are conservative; invoices remain authoritative. An observed overrun blocks further admission. A missing/corrupt/locked ledger, missing model rate, expired price, exhausted cap or unsupported billing contract denies before dispatch with the existing nonretryable budget_guard sentinel.

## Version 2 configuration (not installed)

Required top-level fields: version=2, absolute ledger_path, experiment_id, policy_version, daily_limit_nusd, total_limit_nusd, models. Each provider/model entry requires version, timezone-aware valid_until, text_only_all_tokens=true, positive integer input_nusd_per_token, output_nusd_per_token, max_input_tokens, max_output_tokens. Do not use test fixture rates as vendor prices. Configuration is trusted owner input and must be backed by current billing evidence. It is not model-generated.

CORTEX_BUDGET_CONFIG must point to that reviewed file for every paid process. With no config, legacy runtime behavior continues; there is no paid safety claim. Version 1 configs now fail closed. Initialize the ledger explicitly using BudgetLedger(..., initialize=True) only after reviewing funding and permissions. Existing version 1 ledgers must be backed up with producers stopped; do not migrate away outstanding charges or silently create an empty replacement. This session created test ledgers only.

## Unknown-charge reconciliation

1. Stop all producers using the ledger and preserve a backup. Confirm no request is still active.
2. Obtain provider billing/usage evidence identifying the uncertain attempt. A timeout, cancellation, HTTP error or missing token count alone is not proof of zero billing.
3. If attribution is uncertain, retain the full reservation and leave the call unresolved. Never reset on restart or midnight.
4. For an attributable reserved/unknown/overrun call, invoke the offline owner-only BudgetLedger.reconcile(call_id, charged_nusd, evidence_sha256). It records original/new charges, timestamp and the evidence digest; repeated reconciliation is rejected. Store the actual evidence privately outside Git. No capability/API exposes this operation to a model.
5. Review aggregate remaining allowance before restarting. A verified correction is not permission to raise spending caps. Reconcile all uncertainty before model selection.

Cancellation retains reservations; a hard crash leaves them reserved on disk. Settlement is once-only. The ledger still uses synchronous SQLite with a two-second lock timeout: responsiveness under loaded paid workloads remains unmeasured. Account-wide charges outside these instrumented adapters are outside this application's governor. Provider-side spending controls remain a separate requirement.
