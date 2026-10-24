# Cloud Codex handoff — 2026-10-24

Read stage1-session2.md, stage1-accounting.md, stage1-frozen-compatibility.md, stage1-evaluation.md and stage1-benchmark-runner.md first. Stage 0 passed at tag cortex-v1-stage0-baseline. Stage 1 remains IN_PROGRESS; Stage 2 is NOT_STARTED. No paid trials, billing changes or model selection were authorized or performed. Do not resume A14 or add architecture.

The owner requested publication of all stage changes to the existing GitHub repository for cloud continuation. Source, benchmark, documentation, manifests and sanitized results belong in Git. Runtime databases, ledger databases, logs, API/OAuth/device credentials and environment files do not. Existing local secrets were not uploaded; cloud work must not assume it has the Windows device, credentials or native application.

## Reproduce evidence

The consolidated manifest is docs/v1/stage1-session2-manifest.json; exact results/environment are in stage1-session2-results.json. Run its test paths serially in one pytest process, with CORTEX_HUB_DEVICE_ID=asus, CORTEX_MODEL_PROFILE_PATH empty and an explicit writable --basetemp. Do not overlap suites against tests/test_cortex.db. Python baseline is Windows Python 3.14.7 with package inventory in test-environment-packages.json; cloud OS differences must be reported, not hidden by skips. Existing test networking restrictions remain in tests/conftest.py. Do not run root legacy/manual scripts as if they were the safe consolidated suite.

For offline benchmark preparation use python scripts/evaluate_v1.py --output <fresh-writable-directory>. Do not pass --run or --worker during unauthorised evaluation. Preparation should produce 100 scheduled trials and zero completed trials. The scorer must not manufacture a pass for that empty evidence.

## Next work

Review positive synthetic fixture execution for all 20 cases, especially message selection/injection, follow-up context, approval denial and cancellation. The offline worker smoke reaches the production decision path but deliberately returns a provider failure; this is not paid-model or end-to-end model-quality acceptance. Review exact model SDK contracts and price/token ceilings, then obtain explicit provider/funding/daily/total-cap authorization before calls. No recurring billing or auto-reload. Preserve prior failures and all historical validation debt. Evaluate 100 cases/model and held-outs against the unchanged predeclared gates before selecting a winner.

A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A12_FOUNDATION = COMPLETE
A13 = COMPLETE
A14_IMPLEMENTATION = FROZEN
A14_LIVE_ACCEPTANCE = OPEN
A12_2_GENERATIVE_WORKSPACE = DEFERRED
STAGE_1 = IN_PROGRESS
STAGE_2 = NOT_STARTED
