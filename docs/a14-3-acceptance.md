# A14.3 provider capacity diagnosis and recovery — 2026-10-22

## Decision

```ini
REAL_STRUCTURED_INFERENCE_PATH = VERIFIED
MODEL_SELECTED_AGENT_ORCHESTRATE = YES
VALID_LIVE_TASKGRAPHS = 0
LIVE_MULTI_AGENT_ACCEPTANCE = FAILED_BEFORE_PLANNER_INFERENCE
A14_COMPLETE = NO
A14 = IN_PROGRESS
```

A viable real Groq inference path was established, and the one bounded live request
selected orchestration. It failed locally before the planner inference. The
reproduced catalog-size blocker was repaired after that attempt; its repair has
deterministic proof but has not been retested live. No second live acceptance or
synthetic graph was used to claim completion.

## Historical HTTP 413: evidence and uncertainty

The retained A14.2 record contains HTTP 413, `rate_limit_exceeded` and the frozen
router's CONTEXT_TOO_LARGE classification. It does **not** retain that response's
numeric Limit/Used/Requested fields or rate headers. Its exact restriction cannot
be established from that status/code pair alone. A14.3 did not reproduce 413.

Current observed Groq headers identify **8,000 TPM** and **1,000 RPD** for these
credentials. The model context windows are roughly 131K. Rate admission and model
context are different limits; neither the historical code nor these current
headers proves which restriction caused the original 413. There is no evidence
that the approximately 6–8K estimated input exhausted a 131K model context, but
serialized size and provider admission estimates can differ from Cortex's byte
estimate. TPD and remaining daily token capacity are unverified.

Groq documents [413 as request-body size](https://console.groq.com/docs/errors),
and [TPM/rate headers and 429 separately](https://console.groq.com/docs/rate-limits).
Consequently, the frozen all-413-to-context rule is a conservative routing policy,
not a complete diagnosis. [Separate frozen-routing review](a14-3-frozen-routing-review.md)
records the conditional proposal and missing evidence. No frozen code was changed.

The direct transport now preserves bounded numeric error metadata for 413 as well
as other statuses: HTTP status, allowlisted provider code, quota scopes,
Limit/Used/Requested numbers and validated numeric/duration headers. It does not
retain error messages, organization IDs, generated text, prompts or credentials.
Invalid tool generations expose only byte count and recognized envelope fields.
The original status and classification remain unchanged.

## Verified capacity and compatibility matrix

Metadata-only requests returned HTTP 200 for Groq's model list, NVIDIA's model list,
and Gemini's specific model. No inference tokens were needed for those reads.
The previous local cooldowns had expired; they were not cleared or bypassed.
Capacity was previously unknown because no profile file was configured.

| Configured model | Verified context/profile | Cortex contract | A14.3 live evidence |
|---|---|---|---|
| Groq `qwen/qwen3.8-27b` | API 131,072; conservative profile 131,042 from published page | Native function arguments, local AgentDecision/graph validation | HTTP 200; first proposal lacked a capability and was rejected before execution |
| Groq `openai/gpt-oss-120b` | 131,072; max completion 65,536 | Same native contract | First diagnostic 400 `tool_use_failed`; clarified-contract diagnostic 200; live repair selected `agent.orchestrate` |
| Gemini `gemini-3.8-flash` | 1,048,576 input / 65,536 output | JSON MIME plus text schema, local validation | Metadata read 200; no generation attempt here; historical 503 remains relevant |
| NVIDIA `deepseek-ai/deepseek-v4.1-flash` | Model card combined 1,048,576; exact ID listed by live endpoint | JSON text plus local validation | Metadata read 200; no generation attempt here; PUBLIC-only trial restriction preserved |
| NVIDIA `z-ai/glm-5.3` | Model card 1,048,576; exact ID listed by live endpoint | JSON text plus local validation | Metadata read 200; no generation attempt here; PUBLIC-only trial restriction preserved |

Sources: [Groq Qwen](https://console.groq.com/docs/model/qwen/qwen3.8-27b),
[Groq GPT-OSS](https://console.groq.com/docs/model/openai/gpt-oss-120b),
[Gemini](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash),
[NVIDIA DeepSeek](https://docs.api.nvidia.com/nim/reference/nvidia-deepseek-v4_1-flash),
[NVIDIA GLM](https://build.nvidia.com/z-ai/glm-5-3/modelcard).
Endpoint-specific lower restrictions or trial limits can still apply. Published
tool/JSON support is not a guarantee of correct proposals; local strict validation
remains mandatory. No strict-output mode or new model/provider was enabled.

`shared/inference/model-profiles.verified.json` preserves every existing quality,
latency, modality and suitability value, changing context_window only. The local
ignored `.env` now points `CORTEX_MODEL_PROFILE_PATH` at that file using the existing
loader. `model-capacity-evidence.json` records sources and the Qwen API/page
discrepancy. These are documented capacities, not quota or eligibility overrides.
Actual privacy, health, tools and dispatch restrictions still apply. The normal
INTERNAL request did not use NVIDIA trial models.

## Full payload measurements and minimal repairs

The new content-free metrics measure the exact bytes serialized by httpx, including
schemas, tool definitions and escaping. Component measurements overlap; they must
not be added together. Estimates are explicitly byte-based, not token counts.

| Measurement | Successful diagnostic | Initial live decision | Live repair decision |
|---|---:|---:|---:|
| Complete wire request bytes | 24,009 | 28,889 | 31,784 |
| System bytes | 18,345 | 20,022 | 22,644 |
| Capability catalog bytes | 10,168 | 10,177 | 10,177 |
| State bytes | 722* | 2,079 | 4,701 |
| Serialized tool definitions | 4,168 | 4,168 | 4,168 |
| Native schema bytes | 3,987 | 3,987 | 3,987 |
| Wire minus raw message text | 5,426 | 5,688 | 5,961 |
| Output allowance | 1,024 tokens | 1,024 tokens | 1,024 tokens |
| Full-wire byte estimate | 6,003 | 7,223 | 7,946 |

The successful diagnostic's provider usage was **4,717 prompt tokens / 248
completion tokens / 4,965 total**, including 164 reported reasoning-token *count*
only; no hidden reasoning was collected. Exact usage for the two live decisions
was not persisted, so estimates are not represented as provider token counts.

*The diagnostic preceded the final instruction-placement repair: its state suffix
measurement included 311 instruction bytes. The live request used the repaired
placement, and STATE was a complete JSON document. Tests cover both native and
text-only transports' trailing JSON boundaries.

Repairs at A14-owned inference boundaries:

- Native function schema annotations are compacted without changing validation.
- An exact duplicate worker schema is removed from Groq's system text only when
  the same schema is supplied in native function parameters. Text-only transports
  retain their schema text. Unknown/different schemas are not removed.
- A14 native output instructions explicitly request one transport function call,
  rather than a text JSON object outside the required function. This changes no
  model decision or selected Cortex capability. A real valid response and later
  real orchestration selection followed the change; model nondeterminism means
  this is not proof that every previous 400 had that single cause.
- Delegated workers' model catalogs contain only their already-declared tool
  scope. Argument constraints, executor identities, failed results, unavailable
  states and original safety text are preserved; dispatch retains full checks.

## The newly reached planning bottleneck

The real graph task failed with a generic local ValueError before any planner
request. A separate read-only reconstruction used the authenticated discovery
snapshot, existing permission checks and the production catalog, with no inference
or execution. Its **90 eligible entries** yielded **26,643 bytes**, above A14's
**24,576-byte** bound. Compact JSON alone yielded **24,856 bytes**, still too large.
This reproduces a definite planning-input blocker consistent with the live trace.
The old generic error did not retain an exact typed reason, so the reconstruction
is distinguished from an inline recorded exception code.

The repaired planner catalog losslessly interns argument definitions and target
sets in self-described rows. The reconstructed payload is now **13,607 bytes**.
Round-trip equality and preservation of the full public request were checked.
No capability, constraint, role, user requirement or verification criterion was
silently truncated. The 24-KiB bound was not increased. Oversize contexts now get
an explicit CONTEXT_BUDGET_EXCEEDED error and do not waste the model replan on a
local serialization failure.

## Bounded real probes and native evidence

Exactly **four inference requests** occurred: two controlled public diagnostics
and two model decisions within **one** normal live acceptance request. The
diagnostics disabled continuation for that request to avoid consuming alternate
providers. No provider was forced beyond eligibility, no quota state was reset,
and no repeated acceptance was issued. Model outputs were not injected.

1. GPT-OSS diagnostic: HTTP 400 `tool_use_failed`, 1.437 seconds overall.
2. Clarified-contract GPT-OSS diagnostic: real valid RESPOND, 1.474 seconds overall.
3. Normal acceptance Qwen proposal: HTTP 200, CALL_CAPABILITY without capability;
   rejected before action by the existing frozen engine.
4. Existing bounded repair selected GPT-OSS: HTTP 200, real
   CALL_CAPABILITY `agent.orchestrate`, then real hub dispatch.

```text
REQUEST_TASK_ID = request_094a28e35a2754be3aab6e7b
AGENT_TURN_ID = turn_635c6737d7d542918fa35d1c87cc35b6
GRAPH_TASK_ID = graph_172a8cfee7a5f8ef304df738
MODEL_SELECTED_ORCHESTRATION = YES
GRAPH_TASK_CREATED = YES
VALID_TASKGRAPH_ACCEPTED = NO
PLANNER_INFERENCE_REQUESTS = 0
WORKER_ROWS = 0
REAL_ASUS_WAIT_RECEIPTS = 0
FINAL_STATUS = FAILED
```

The owned graph row is an attempted orchestration, not an accepted TaskGraph.
The failed hub orchestration receipt is not a successful physical ASUS action.
The post-live catalog-packing repair has not been retested live under the one-run
bound. Concurrency, dependency ordering, ASUS execution, synthesis, verification
and conservative graph restart proof remain unverified live.

Computer Use inspected actual Python-owned native window **591880**. Screenshot-
backed navigation opened Workspace. It showed the exact current request FAILED,
the unified multi-agent failure, **0/0 subtasks**, verification not established,
empty worker lifecycle and the failed orchestration receipt. This verifies actual
native graph-*failure* projection. It does not verify populated worker graph
rendering or multi-agent success. Voice was mocked; text/provider/task path was
real. All owned Hub/Agent/desktop processes were stopped and cleanup confirmed.

## Automated checks and files

Final focused provider/router/A14/recovery run: **183 passed / 0 failed / 0 skipped**,
**19.66 seconds**. Earlier focused totals overlap and are not added. A development
run had 182 passes and one failure because a minimal catalog fixture omitted
optional description/argument fields; the packer was made compatible without
weakening the existing test.

Final full regression: **935 passed / 0 failed / 0 skipped / 0 xfailed**, **187.22 seconds**. All **919 baseline checks** retained, **16 added**, **0 missing**.

The full suite includes every baseline file plus `tests/test_provider_capacity.py`.
Tests run serially with a dedicated temporary directory. Their model-profile env
is explicitly empty to isolate controlled unknown-capacity fixtures from local
production `.env`; the new profile-loader test explicitly loads the verified file.

Files changed/added in this phase:

- `hub/direct_provider.py`: A14-gated native contract/schema deduplication,
  content-free wire metrics and safe error evidence; no status rewriting.
- `hub/inference_metrics.py`: new serializer measurements and sanitized headers.
- `hub/orchestration_contract.py`: mark the A14 inference boundary.
- `hub/orchestration_workers.py`: scoped catalogs and compact bounded inputs.
- `hub/orchestration_inference.py`: lossless planning index, safe instruction
  placement, explicit bounded-context error.
- `hub/orchestration.py`: indexed planner catalog and truthful context failure.
- `shared/inference/model-profiles.verified.json` and
  `shared/inference/model-capacity-evidence.json`: sourced capacity configuration.
- Local `.env`: existing profile path only; no credentials printed or replaced.
- `tests/test_provider_capacity.py`, this report, architecture documentation and
  the separate frozen-routing review.

All **11 frozen hashes match**. Provider selection, retry limits, privacy,
approval scopes, execution authority, quota enforcement and A10/A10.5 files remain
unchanged. No paid services, new credentials, external account writes or OAuth
scope changes were made.

Evidence root:
`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a14-3-acceptance`

Key artifacts: provider inventory; capacity probes before/after native contract;
wire measurements; planning forensics; live request/result/routing records;
native evidence; cleanup; focused XML; final regression XML/summary; frozen audit.

```ini
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A12_FOUNDATION = COMPLETE
A13 = COMPLETE
A12_2_GENERATIVE_WORKSPACE = DEFERRED
A14 = IN_PROGRESS
A10_A10_5 = FROZEN
```
