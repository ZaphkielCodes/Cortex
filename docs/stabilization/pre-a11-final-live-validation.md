# Pre-A11 final live validation — 2026-10-20

A11 was not started. The unchanged compound acceptance launcher ran exactly once. No prompt-specific repairs, app pre-positioning, recovery redesign, approval-expiry changes, or router ranking/budget changes were made.

Evidence directory:
`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\pre-a11-final-validation-20261020`

## PROVIDER READINESS

- Authenticated, non-inference model-list requests: Groq HTTP 200 (0.571 s), Gemini HTTP 200 (0.455 s). No generation requests were used for readiness.
- Production health-store evaluation showed no active cooldown for configured routes. Quota remaining was unknown; metadata success did not establish generation capacity.
- Groq and Gemini were eligible for the UI tasks subject to sharing approval. NVIDIA was excluded by its configured PUBLIC-only trial policy. That restriction was preserved.
- The documented acceptance harness used isolated task/health storage; production cooldowns were neither cleared nor overwritten. Persistence/reset behavior was also covered by offline regression.
- Readiness permitted one controlled run. Actual inference later demonstrated transient service failures and rate limits.

## HTTP 413 REPAIR VERIFIED

YES, OFFLINE. Removed the contradictory `413 + rate_limit_exceeded -> REQUEST_TOKEN_LIMIT` exception. HTTP 413 now takes precedence over provider error wording and remains `CONTEXT_TOO_LARGE`. Genuine HTTP 429 handling is unchanged.

Verified behavior:

- No safe identical-request retry is enabled for HTTP 413.
- A verified larger-context eligible model can receive the request through existing failover.
- Unknown-capacity alternatives are rejected after a context failure.
- Without a verified valid alternate, the task fails truthfully without replaying the oversized request.
- No model context sizes or quota limits were invented.

One prior test was migrated: its old expectation allowed a rate-coded HTTP 413 to bypass the verified-context guard. That expectation directly contradicted this validation brief. The replacement asserts verified larger capacity; four additional checks cover truthful failure and contradictory/missing provider codes. The unreachable `REQUEST_TOKEN_LIMIT` user message was removed.

No HTTP 413 occurred in this live run; this repair has offline proof only.

## FULL REGRESSION

**713 passed / 0 failed / 0 skipped / 0 xfailed**, 147.79 seconds. Two existing dependency deprecation warnings. All 709 baseline checks were accounted for, including the documented migrated assertion.

Focused recovery/routing/receipt checks: **128 passed / 0 failed**, 27.03 seconds.

The first focused invocation omitted the baseline's required `CORTEX_HUB_DEVICE_ID=asus` setting and had one device-context failure. Repeating with that setting passed without changing the affected assertion. Both diagnostic and final artifacts are retained.

The external-network test guard, acceptance script, benchmark runner, frozen prompts, recovery implementation, model selection, failover budgets, and health persistence were hash-checked unchanged. No production changes were made after the successful regression.

## layout_change

- Final result: **BLOCKED — APPROVAL_EXPIRED**, 107.172 seconds including harness review time.
- First unrecovered causal failure: `APPROVAL_EXPIRED`; cause `HUMAN_REVIEW_TIMING`.
- Calculator launched once and produced a bound native UI observation. No mode-changing input was executed. Scientific mode and preserved final display were not verified.
- The approval was surfaced promptly, but the reviewer incorrectly told the human it lasted 120 seconds. Actual unchanged `AgentTurn.APPROVAL_SECONDS` was **60 seconds**. The late approval was rejected with HTTP 400. This reporting error was corrected during the run; expiry was not weakened.
- The independent window inspector did not expose this Calculator window. No independent final screenshot or mode verification was obtained.
- Request ID: `request_b1bc3adcba205bf8e6f1a320`; AgentTurn ID: `turn_d6b0e084fc4340079cb9360b0fc4dad0`.

## canvas_dimensions

- Final result: **INCOMPLETE — MODEL_DECISION**, 106.125 seconds.
- First unrecovered causal failure: Groq GPT-OSS HTTP 400 `tool_use_failed`, classified `INVALID_OUTPUT` / `UNSUPPORTED_TOOL_USE`, with no eligible usable continuation. Cause `MODEL_VARIABILITY`, compounded by `EXTERNAL_PROVIDER_FAILURE`.
- Earlier interruptions were recovered: Gemini HTTP 503, a GPT-OSS HTTP 400, and Qwen HTTP 429. They are recorded separately from the final blocking failure.
- Paint launched once and produced a bound observation. The human sharing approval was recorded successfully, HTTP 200. No resize input executed.
- Independent read-only inspection matched the actual Paint HWND `4129858`, title `Untitled - Paint`, and read **1728 × 1080 px**. The required 640 × 480 state was not reached. A fresh blank 640 × 480 unsaved canvas was not verified.
- Request ID: `request_ed7fba810c72d7b4cb005f1a`; AgentTurn ID: `turn_f1deab457a494ad581c5e8b75ef4fbd9`.

## unsent_document

- Final result: **BLOCKED — APPROVAL_EXPIRED**, 184.317 seconds including harness review time.
- First unrecovered causal failure: expiry of the second Notepad observation approval; cause `HUMAN_REVIEW_TIMING`.
- Earlier Gemini HTTP 504 and Groq Qwen HTTP 429 were recovered. Notepad launched once; two observations completed. The first sharing approval was recorded successfully, HTTP 200. The second was surfaced but expired without a recorded human decision.
- After database confirmation of expiry, an empty local answer released the stale harness input. Its default-deny submission received HTTP 400 and changed no approval state. This is **not** counted as a human denial.
- The human approved the additional observation after this approval had expired and the single run had finished. That late authorization did not revive the expired task or trigger another run.
- Independent read-only inspection matched HWND `68510`, title `Untitled - Notepad`, `Untitled. Unmodified.`, and **0 characters**. No text input executed. Exact `amber` / `cobalt` two-line content was not present or verified.
- Request ID: `request_5d5f6663cfce0cc86f055c7a`; AgentTurn ID: `turn_cc3c8aa5f6f34a30afb939f13d02f2e2`.

## RECOVERY MECHANISMS OBSERVED

- Actual provider failover, provider cooldown waiting, subsequent successful model decisions, task/parent identity preservation, and native window observations were observed.
- Exactly one launch per case. Seven completed commands had distinct command IDs: three launches and four read-only observations. No UI mutation or side-effect replay was observed.
- No-progress recovery, target reacquisition, invalid-capability correction, argument-schema correction, text readback completion, and restart recovery were **not exercised live in this run**. Relevant mechanisms remain offline-tested; no additional live proof is claimed.
- Complete-case semantic passes: **0/3**. Two cases were approval-blocked, rather than demonstrated semantic computer-use failures. Paint's requested end state was demonstrably unmet after model/provider failure.

## APPROVAL RESULTS

`APPROVAL_GRANTED = 2`, `APPROVAL_EXPIRED = 2`, `APPROVAL_DENIED = 0`.

The expired approvals did not authorize sharing. Recorded approvals were scoped to Groq and their respective task observations. No approval was granted on the human's behalf; no expiry or privacy policy was changed.

## PROVIDER FAILURES

- Gemini: HTTP 503 service unavailable; HTTP 504 deadline exceeded (11.418 s on the Notepad attempt).
- Groq Qwen: genuine HTTP 429 rate limits, with bounded cooldown waiting.
- Groq GPT-OSS: HTTP 400 `tool_use_failed`; model-scoped invalid-output handling. A later successful decision does not make the final Paint model rejection a success.
- NVIDIA: ineligible by privacy policy, not an attempted or failed UI provider.
- Metadata availability did not predict a reliable multi-turn inference window. No additional readiness inference or second acceptance run was performed.

## GENERAL DEFECTS REMAINING

This run does **not establish a new general execution/recovery defect**. Approvals and provider/model errors prevented the requested semantic completion. It also does not prove the general orchestration is resolved.

A separate observability limitation was recorded: Paint's aggregate `llm_ttft` / `llm_total_latency` became negative because a failed later attempt retained the previous successful `model_complete` timestamp. This report uses the nonnegative per-provider attempt latencies instead. That diagnostic issue was not patched in this validation-only pass and is not presented as a computer-use failure.

No architecture redesign is justified by these blocked cases. Healthy-provider compound completion, including real UI mutation/readback and recovery where needed, remains validation debt. The Calculator approval-window misinformation was a reviewer error, not a runtime defect.

## PRE-A11 DECISION

```ini
A10_GENERAL_COMPOUND_ORCHESTRATION_RESOLVED = NO
PRE_A11_STABILIZATION_COMPLETE = NO
READY_FOR_A11 = NO
A11_STARTED = NO
```

These decisions mean **live acceptance remains unproven**; they do not relabel human-review timing or external provider failures as architecture failures. Existing historical A10.5 PUBLIC failover proof remains separate from this private-UI acceptance.

Detailed artifacts: `provider-readiness.json`, `focused-final.xml`, `regression-summary.json`, `regression.xml`, `change-scope.json`, `frozen-hashes.json`, `source-sha256.json`, `compound/results.json`, `compound/runtime.db`, `compound-review.json`, and `independent-semantic-inspection.json` in the evidence directory above. Raw local runtime evidence was not copied to a third-party report.
