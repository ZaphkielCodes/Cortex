# Repository publication audit — 2026-10-17

The local inventory covered 7,265 regular files and 1,080 directory entries (432,575,169 bytes). Regular files were hashed; 257 authored/scratch text files were decoded and Python files were parsed. Dependency junctions were recorded without following them. This is an inventory and static reference audit, not an assertion that every runtime path was exercised.

There were 1,709 exact duplicate groups, with 4,974 extra copies totaling 226,665,082 bytes. No group contained two authored source files. Repetition is concentrated in dependency stores, installed dependencies, caches, and generated builds; it does not justify deleting either frontend source tree.

The desktop loader references both frontend builds, and an A9 verification script references the nested one. Both source projects and their original orb assets are retained. Both lockfile formats in the nested project are retained because package-manager usage has not been consolidated.

Excluded local files below remain on disk. The listed stale scripts have no runtime imports; generic word matches such as `validate` or a test function named `test_db_safety` are not dependencies on those files. The two broken runners are not pytest collection entrypoints.

| File | Reason |
| --- | --- |
| `check_dev.py` | One-off database inspection script with import-time production access. |
| `refactor_db.py` | One-time source migration script; no inbound runtime reference. |
| `refactor_db2.py` | One-time source migration script; no inbound runtime reference. |
| `scratch_fix.py` | One-time bulk test rewrite script; no inbound runtime reference. |
| `validate.py` | Old live validation script deletes the production database. |
| `validate_final.py` | Old live validation script deletes the production database. |
| `physical_test.py` | Old manual live harness; starts services and automatically approves actions. |
| `test_db_safety.py` | Manual root-level diagnostic; current tests have database isolation fixtures. |
| `test_llm_failover.py` | Old manual paid-provider probe, not an offline regression test. |
| `test_vosk_frames.py` | Import-time speech-model configuration experiment. |
| `tests/run_phase7a_tests.py` | Unreferenced manual runner with a syntax error; current pytest tests retained. |
| `tests/run_phase7b_tests.py` | Unreferenced manual runner with a syntax error; current pytest tests retained. |
| `scripts/rekey_asus.py` | Machine-specific credential migration containing an embedded device secret. |
| `scripts/run_lenovo_agent.py` | Machine-specific launcher containing an embedded device secret. |
| `scripts/run_lenovo_agent.bat` | Machine-specific launcher containing an embedded device secret. |
| `agent/ui_settings.json` | Generated personal window preferences; code supplies defaults. |
| `agent/screenshots/screenshot_20261015_200605_571.png` | Private runtime screenshot. |

Also excluded: `.env`, runtime data/logs/screenshots, credential files and deployment bundles, Vosk model files, Node/Python dependency caches, test caches, generated builds, scratch experiments, and generated scenario reports. Source scenarios and current tests remain included.

Publication preserves the runtime implementation, A5–A10 behavior, and current provider configuration defaults. No legacy capability modules were removed just because the preferred path is model-driven. No physical automation or paid-provider validation was run as part of this audit.

Validation of this publication snapshot: all 243 focused migration/A5–A10 regression tests passed with mock providers and an isolated test database (two dependency deprecation warnings). The active React frontend production build passed (bundle-size warning). An earlier sandbox test attempt had 227 passes and 16 temporary-directory setup errors; the complete rerun outside that sandbox passed. The full historical suite was not run or claimed green.
