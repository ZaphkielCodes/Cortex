# Stage 1 executable benchmark and scoring freeze

STAGE_1 = IN_PROGRESS. No paid trials or winning model. The predeclared gates in stage1-evaluation.md remain unchanged.

## Offline preparation

From the repository run `python scripts/evaluate_v1.py --output <new-directory>`. This verifies benchmarks/v1/freeze.json and writes a reproducible seeded schedule of 20 tasks x five repetitions. It makes no model calls. Existing output directories are rejected. The immutable schedule, definitions, fixtures, held-outs, scorer and rubric are hashed before evaluation. Never refresh the lock after seeing candidate results to excuse a change; version the benchmark and rerun comparisons if a genuine fixture defect is found.

## Future authorized execution

Only after explicit funding authorization, current model/price verification and reviewed version 2 budget configuration, the same command may include `--run --provider <groq-or-gemini> --model <verified-model-id> --budget-config <absolute-private-config> --ack-paid-evaluation`. These flags are safeguards, not billing authorization by themselves. This session does not authorize running them.

Each trial uses a child process, fresh synthetic SQLite database and synthetic credential vault. It invokes production RequestPipeline -> AgentTurn -> named real provider -> normal capability/schema/permission checks. Google HTTP transports and device executors are synthetic boundaries; model decisions and answers are not supplied by the runner. Canonical device advertisements are reconciled by A13. External writes and orchestration are outside the fixture allowlist. Missing scopes, denied copy approval, offline devices and private requests retain their normal restrictions. Optional `--synthetic-sharing` resolves only model.share approvals for synthetic fixture content with the named provider; the private case remains denied. All execution approvals are denied. This flag must never be used with real owner data.

The selected provider is fixed for attributable model-quality measurement; no cross-model fallback is enabled by this runner. Existing bounded adapter transport retries remain. Outage/fallback accounting is separately tested with deterministic transports. A worker timeout stops the batch and retains uncertain budget reservations; it does not replay the request. Results from missing/unexecuted cases remain missing, not passes.

## Evidence and review

Each trial writes evidence.json: case and repetition, provider/model, initial/final states, proposals/schema outcomes, prompt/schema hashes, synthetic boundary receipts, approvals, response latency and durable per-attempt costs. No owner mailbox or device files enter the fixtures. The fixtures represent one bounded snapshot, not a complete Google or OS emulator. Receipt queries and arguments must be reviewed; returning a synthetic record does not prove query correctness.

`python scripts/score_v1.py <run-directory>` produces scores.json, per-case metrics, unconditional denominator 100 and a Wilson 95% interval. Supply each trial's review.json with the eight rubric keys, each containing pass=true/false and a concrete evidence reference. A justified not_applicable/reason is permitted only for applicable optional dimensions; intent, authorization and truthfulness cannot be waived. Missing review is never a pass; duplicate case/repetition identities are rejected. Review ambiguous intent/grounding independently as specified in the evaluation document. The scorer deliberately never selects a winner automatically.

Schema counts, observed tools, errors, latency and ledger costs are machine observations. Correct intent, semantic argument equivalence, grounded facts and naturalness require evidence-backed adjudication. Reviewers must distinguish synthetic boundary success from real Google/Windows acceptance, and must not count an outage as success. Linked cases 02/06/16 run their prerequisites within the same trial; their latency/cost includes prerequisite work.

## Held-out separation and current validation limits

benchmarks/v1/held-out.json is stored separately; neither its prompts nor behavioral oracles are passed into main-suite model context. Only the active task prompt and permitted synthetic runtime facts are supplied. The main 20 definitions are unchanged. Held-out execution/adjudication is a separate pre-selection step; the runner currently schedules the main 100 cases only.

Offline smoke validation proves the isolated worker reaches the real decision pipeline with a failing test provider and records truthful failure; it is not a real-provider benchmark or proof that all 20 fixture paths complete. Positive end-to-end fixture coverage, provider-specific model contract trials, complete 100-case evidence, independent scoring and held-out validation remain required before choosing a model. Cancellation case 18 deliberately exposes the normal runtime's ability or inability to resolve a pending task; the harness does not add a phrase handler or manufacture cancellation. A11 live acceptance and earlier compound validation remain open.
