# Stage 0 baseline audit — 2026-10-23

## Freeze boundary
Branch: `main`. Previous HEAD: `99d2173` (Complete A11 integration foundation and Google OAuth setup); prior commit `b8e447e` published reviewed runtime/desktop source. No tags existed before this freeze. Baseline identity is the commit targeted by annotated tag `cortex-v1-stage0-baseline`; obtain its exact SHA with `git rev-parse cortex-v1-stage0-baseline^{commit}`. The SHA is also recorded in the final handoff, avoiding a self-referential commit hash inside its own contents.

Initial tracked files: 337. Commit candidates before new v1 docs: 378. Initial dirty tracked source/tests: 20 files, 327 insertions and 75 deletions. Untracked A12–A14 workspace, discovery, inference/orchestration protocols, tests and acceptance reports are preserved in this baseline. No existing code was discarded, rolled back or rewritten. Stage 0 edits are documentation only (README and docs/v1); previously uncommitted implementation is captured as-is. A14 remains in code/tests but is removed from the active product critical path by the roadmap, not by changing runtime routing.

No push, history rewrite, provider purchase/configuration, new credential, OAuth scope, runtime live call or Stage 1 implementation in Stage 0.

## Regression evidence
Exactly one serial invocation of the saved consolidated 53-file manifest, using Python 3.14.7, Windows 11 build 26200, pytest and installed package versions in test-environment-packages.json. `CORTEX_HUB_DEVICE_ID=asus`, `CORTEX_MODEL_PROFILE_PATH=''`; existing conftest isolates tests/test_cortex.db and blocks external networking. No production database used. Test process ran in the current filesystem sandbox with repository write permission; the older full live-host filesystem permissions were not reproduced.

**945 passed / 2 failed / 0 skipped / 0 xfailed**, 242.72 seconds. 947 cases collected; this records the current result, not the older 935/947-pass claims. No assertion edits, skips or implementation fixes. Two dependency deprecations: google.genai _UnionGenericAlias and Starlette/AnyIO BlockingPortal alias.

Failures:
- tests/test_peer_transfer_orchestration.py::test_coordinator_uses_workers_and_real_tls_without_reading_source: result failure instead of success.
- tests/test_peer_transfer_wire.py::test_tls_wire_multichunk_resume_integrity_and_no_overwrite: finish rejected with PermissionError.

Receiver finalization uses os.link before deleting the partial file. A separate synthetic same-directory hard-link probe failed with PermissionError, WinError5/errno13 in this sandbox. This is evidence of an environment limitation consistent with the failures, not definitive proof of the receiver's suppressed underlying exception. Do not classify these as fixed, intentionally obsolete or established production regressions. Host-permitted confirmation remains validation debt. No repeated full run was performed, honoring the once-only baseline request.

All **11/11 frozen hashes match** the saved A11 foundation hash manifest, including AgentTurn, A10.5 routing/failover, compound UI/recovery, frozen benchmark and conftest. Exact results and hashes: regression-result.json. Test manifest is committed; raw XML/output remain local in work/v1-stage0 under the chat evidence directory identified in evidence-ledger.md. No private runtime logs or raw task contexts are committed.

Reproduction from repository root with existing dependencies and equivalent local filesystem/TLS permissions:

```powershell
$env:CORTEX_HUB_DEVICE_ID = 'asus'
$env:CORTEX_MODEL_PROFILE_PATH = ''
$stage0Tests = Get-Content docs/v1/suite-manifest.json -Raw | ConvertFrom-Json
python -m pytest @stage0Tests -q --junitxml="$env:TEMP/cortex-stage0-regression.xml"
```

Do not run overlapping suites against the shared test database. Root-level legacy/manual scripts are excluded deliberately; some can touch production data or providers. The manifest is the historical consolidated suite, not a claim that every repository script or frontend check was run. No frontend build or new native acceptance was performed in Stage 0.

## Security and artifact review
Audited tracked and untracked nonignored candidates with credential-pattern and suspicious-extension scans, plus exact comparison against current local .env/device-secret values held only in memory. No current credential matches, private databases/logs, key files or binaries in the candidate set. Eight pattern hits were inspected and are synthetic test values/redaction fixtures; see security-audit.json. Never print or stage actual credentials.

Confirmed ignore rules for .env, private data/logs/screenshots and the previously identified machine-specific secret-bearing launch/rekey scripts. Dependencies, build output, caches, scratch, local databases and personal configuration remain ignored on disk. No secrets were deleted or rotated. Ignored credential-bearing scripts should not be copied/published; their presence is preserved local debt. OS keyring secrets were not exported.

This is a bounded source publication audit, not a guarantee that arbitrary secrets are absent or a comprehensive forensic audit of prior Git history. Existing reports contain local paths and opaque test IDs; private provider/service bodies are not copied into v1 docs. Original history remains intact. Review the index before commit; never use force-add for excluded files.

## Gate interpretation
Stage 0's exit requirement is a safely committed snapshot and evidence ledger, not a newly green production-readiness claim. The two current environment-associated failures are explicit baseline debt. Stage 0 can pass once the reviewed local commit/tag exist; Stage 1 remains NOT_STARTED. No fresh live validation or revived A14 work is claimed. The historical 82% Goal Meter is retired.
