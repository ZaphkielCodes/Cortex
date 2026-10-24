# Stage 1 first-session report — 2026-10-23

STAGE_1 = IN_PROGRESS
PAID_EVALUATION = NOT_STARTED
DEFAULT_MODEL = NOT_SELECTED
STAGE_2 = NOT_STARTED

Baseline: 880973853a1a984375682b3f46c4636dedbdc38e, cortex-v1-stage0-baseline. No commit/push in this session. No live model calls, billing purchases/settings changes, new credentials or OAuth scopes. Existing security/approvals remain unchanged. A14 was not developed.

Deliverables: stage1-evaluation.md and benchmarks/v1-single-agent-20.json define20 unique tasks and predeclared scoring/selection gates. stage1-providers.md records official price/spec references and separate technical/account eligibility. stage1-accounting.md documents opt-in persistent budget admission and its limits. stage1-frozen-compatibility.md identifies the existing multi-retry incompatibility; no frozen file changed.

Implementation: new hub/cost_accounting.py; minimal hook in hub/direct_provider.py and SDK guard in hub/tracing.py; 21 tests in tests/test_v1_cost_accounting.py. Direct usage assignment now happens before output shape validation so malformed billed output is not discarded. Budget ledger uses atomic reservations, conservative unknown-charge retention, expiry/unknown-rate rejection, restart/midnight handling and once-only settlement. Logs contain numeric usage/cost/status and generated call identity, not prompts/mail/credentials. It is not activated and is not yet sufficient for paid evaluation.

Validation:
- Initial focused run:47 passed,16 setup errors because sandbox temporary-directory creation failed. Switched to an explicit writable temporary directory; no test skipped or weakened.
- Next focused run:61 passed,2 failed because test admission ceilings were below the existing real payload size. Increased the synthetic reviewed ceiling; production admission remained strict.
- Subsequent focused run:63 passed; after adding activation/SDK/price-expiry checks, final focused68 passed in5.81s.
- Full consolidated run:966 passed,2 failed,0 skipped,0 xfailed,281.35s; two existing dependency warnings. All947 Stage0 cases retained;21 new checks pass.
- Failures remain tests/test_peer_transfer_orchestration.py::test_coordinator_uses_workers_and_real_tls_without_reading_source and tests/test_peer_transfer_wire.py::test_tls_wire_multichunk_resume_integrity_and_no_overwrite. Same finish failures as Stage0; sandbox hard-link limitation remains the supported hypothesis, not a new production fix claim.
- All11 frozen source hashes unchanged. Whitespace check passes. No fresh desktop, voice, integration or live provider acceptance claimed.

Machine-readable results: stage1-test-results.json. Local raw regression artifacts: C:/Users/abhin/Documents/Codex/2026-10-15/referenced-chatgpt-conversation-this-is-an/work/v1-stage1. Focused XML: C:/Users/abhin/Documents/Codex/stage1-focused-final.xml. Raw evidence stays outside the source tree.

Remaining Stage1 requirements before paid evaluation:
1. Review/implement the documented narrow frozen-boundary change enforcing at most one corrective proposal retry without unsafe fallback/replay. Current characterization explicitly shows this is not yet enforced.
2. Extend metering to eligible SDK/provider adapters, preserve rate/policy versions and per-task correlation, test whole-router budget denial/fallback end to end, and measure responsiveness under ledger contention. Unknown charges need operator reconciliation.
3. Verify actual provider/model availability, billing coverage (including hidden tokens), privacy terms, account funding method, prepaid limit and daily plus total experiment caps. No authorization to spend follows from this document.
4. Implement/review executable evaluation harness and synthetic fixtures from the20-task specification; freeze artifacts, prepare held-out variants and reviewer scoring. A specification is not executed model evaluation.
5. With explicit funding authorization only, run measured trials, reconcile costs and select a candidate only if all predeclared gates pass. Otherwise remain IN_PROGRESS. Do not start Stage2's real daily-summary runs yet.

Historical A11 conversational and pre-A11 compound validation remain open; A12/A13 foundations remain complete; A14 implementation frozen/live acceptance open; A12.2 deferred. The v1 reliability roadmap remains active and the old Goal Meter remains retired.
