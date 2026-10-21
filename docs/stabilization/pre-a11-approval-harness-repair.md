# Pre-A11 human-in-the-loop acceptance harness repair

2026-10-20. A11 was not started. No live provider readiness calls, inference calls, or desktop acceptance cases were run in this repair pass.

## APPROVAL HARNESS REPAIR

The manual acceptance path no longer blocks indefinitely inside `input()`. A daemon console reader supplies explicit human decisions while the asynchronous reviewer continues checking authoritative approval status. Ordinary case progression pauses at that boundary.

The prominent approval banner identifies the case, task, provider/model recipient, data class, reason, creation timestamp, expiry timestamp, and remaining time. No observation text or user prompt was added to approval logs. Approval polling and ordinary harness polling use a short local interval; no additional model call is made to display an approval.

`y` approves the displayed request, `n` denies it, and `c` cancels the task. An empty line grants nothing. End-of-input cancels rather than autoapproving. Expiry is recorded by the runtime and does not trigger a synthetic denial or a late approval submission. Queued stale answers are not reused for another approval.

Evidence distinguishes:

- `APPROVAL_GRANTED`
- `APPROVAL_DENIED`
- `APPROVAL_EXPIRED`
- `APPROVAL_CANCELLED`

Each review records `surfaced_at`, `created_at`, `expires_at`, `human_decision_at`, and `remaining_seconds_when_decided`. Runtime cancellation is exposed as observational metadata; existing authorization/database resolution semantics are preserved.

## CASE-BY-CASE MODE

The PowerShell launcher requires an explicit `-Case` for a live run. It passes exactly one frozen case ID to the existing runner. A run finishes and saves its evidence before another command is started.

Default evidence paths are unique and include the case:

`C:\Users\abhin\Codes\Cortex\scratch\compound-acceptance-<timestamp>-<unique-id>\<case-id>`

A caller-supplied `-OutputDirectory` is also separated by case. Existing case evidence is never overwritten. `-ValidateOnly -Case <id>` validates preparation without starting providers or apps.

After a case, independently review its semantic end state before starting the next one. HTTP 200, successful launch, an execution receipt, or a model's completion assertion never supplies a semantic pass. Saved `passed` remains unknown pending review.

## APPROVAL COUNTDOWN SOURCE

An observational runtime record is published immediately beside the existing approval wait. It contains the actual active window's `created_at` and `expires_at`, derived from the same runtime timeout setting used by that wait.

Canonical control state exposes those fields. An authenticated, originating-device-only status endpoint returns pending/terminal status, recipient metadata, and current server time without observation contents. The reviewer computes remaining time from runtime `expires_at` and `server_now`; missing metadata stays unknown instead of inventing a deadline.

The existing runtime approval TTL, privacy checks, source-device checks, recipient/model binding, resolution path, and expiration behavior are unchanged. The acceptance launcher always selects manual sharing review; no preapproval or automatic sharing approval was introduced.

## LATENCY METRIC FIX

Direct and Gemini reasoning invocations now have separate attempt timing records. Each owns its start, first-token observation, completion, end, and success/failure/cancellation status. A later or failed invocation cannot inherit the previous invocation's completion timestamp, and a late invocation cannot overwrite a newer invocation's model timestamps.

- Starting a model call clears stale first-token/completion values.
- Failed calls retain their own end time without inventing a completion.
- Aggregate model latency sums nonnegative observed attempt durations.
- Invalid backwards legacy timestamps become unknown rather than negative durations.
- Non-streaming calls do not fabricate TTFT from completion. Missing TTFT remains null.
- Streaming first-token time is recorded only when actual text is observed.
- Metrics aggregation ignores unknown samples, retaining existing percentile behavior.

These are instrumentation changes. Provider payloads, candidate ranking, quota/cooldown policy, failover budgets, execution, and compound recovery decisions were not redesigned.

## PROVIDER FAILURE CLASSIFICATION

The runner retains distinct diagnostic labels for `HTTP_429`, `HTTP_400_TOOL_USE_FAILED`, `HTTP_503`, `HTTP_504`, `TIMEOUT`, `COOLDOWN`, and `CONTEXT_TOO_LARGE`, with provider/model, HTTP status, category, kind, and per-attempt latency where available.

Final outcomes distinguish a provider/model interruption, approval denial/expiry/cancellation, a semantic task failure, and semantic review still required. An earlier recovered provider error does not hide a later semantic failure. Cooldown/context rejection before an inference attempt is still reported from the final structured failure.

## NEW TESTS

28 new offline tests cover:

- Prompt surfacing before waiting for human input; runtime expiry metadata and countdown.
- Approval before expiry resumes the same task without replaying the observation.
- Expired approval cannot be revived; denied approval stays denied; explicit cancellation and EOF.
- Stale queued answers cannot approve the next request.
- Origin-scoped metadata access with no private observation contents.
- Individual-case selection and isolated evidence; invalid case rejected before runtime startup.
- Original manifest byte hash unchanged; model/HTTP success is not semantic acceptance.
- All required provider error distinctions; recovered-error versus final-cause reporting.
- Failed transport timing, missing TTFT, multi-provider aggregate monotonicity, late-attempt ownership, legacy negative timestamps, and non-streaming Gemini behavior.

Real approval-boundary tests use shortened test-instance windows. The production TTL setting is not changed. Transports are mocked and the existing external-network test guard remains intact.

## FULL REGRESSION

**741 passed / 0 failed / 0 skipped / 0 xfailed** in 150.49 seconds. All **713** prior checks are retained; **28** checks were added. Two existing dependency deprecation warnings remain.

The first focused run found a test synchronization issue while runtime metadata was being published and a missing temporary-directory parent. A later stale-input test needed to retain the completed task handle rather than look it up after runtime cleanup. Those test-driver issues were repaired without weakening the tested approval behavior. Final new-test run: **28 passed**. The full final suite passed after the source changes.

All three launcher commands were also exercised in `-ValidateOnly` mode. No live case or model generation was started.

Evidence:
`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\approval-harness-repair-20261020`

Key artifacts: `suite-manifest.json`, `new-tests.xml`, `regression.xml`, `regression-summary.json`, `frozen-hashes.json`, and `source-sha256.json`.

## COMMAND — CALCULATOR

Run from `C:\Users\abhin\Codes\Cortex` while present to answer local approvals:

```powershell
.\scripts\run_compound_acceptance.ps1 -Case layout_change
```

Pass only when scientific layout is independently verified and the original displayed number is preserved. Review the evidence before starting Paint.

## COMMAND — PAINT

```powershell
.\scripts\run_compound_acceptance.ps1 -Case canvas_dimensions
```

Pass only when the correct Paint window contains a fresh blank, unsaved **640 × 480** canvas and both dimensions are independently verified. Review the evidence before starting Notepad.

## COMMAND — NOTEPAD

```powershell
.\scripts\run_compound_acceptance.ps1 -Case unsent_document
```

Pass only when the correct unsaved document contains exactly `amber` then `cobalt` on separate lines, with independent readback after entry.

## STATUS

```ini
PROMPTS_UNCHANGED = YES
APPROVAL_TTL_UNCHANGED = YES
A10_A10_5_EXECUTION_LOGIC_UNCHANGED = YES
READY_FOR_HUMAN_LIVE_VALIDATION = YES
PRE_A11_STABILIZATION_COMPLETE = NO
READY_FOR_A11 = NO
A11_STARTED = NO
```

Hash checks preserve the frozen prompt manifest, canonical router, model selection, health persistence, failover budgets/classification, compound state/recovery implementation, and external-network test guard. AgentTurn changes add approval-observability metadata only; control API changes expose that metadata with source-device access checks. Live semantic acceptance remains unproven until the human runs and reviews the three cases individually. Provider-only interruptions must remain separate from general execution defects.
