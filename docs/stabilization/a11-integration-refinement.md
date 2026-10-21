# A11 integration validation and refinement

Date: October 21, 2026. This report distinguishes implementation, mocked tests, real Google reads, and conversational acceptance. It does not close the deferred Pre-A11 compound desktop acceptance.

## Outcome

| Area | Status | Evidence |
| --- | --- | --- |
| A11 registry, scope/policy gates and adapters | IMPLEMENTED / MOCK_TESTED | Existing 57 A11 checks plus 13 refinement checks |
| Gmail search and metadata read | LIVE_VALIDATED for structured execution | Real `email.search` returned one message; `email.read` returned its headers, with no body requested |
| Calendar upcoming retrieval | LIVE_VALIDATED for structured execution | Real `calendar.list` returned three upcoming events in a bounded seven-day window |
| Native ASUS connection | LIVE_VALIDATED | Native Windows agent authenticated to the disposable Hub |
| Browser cookie/control channel | LIVE_VALIDATED for transport | Real `/login` cookie authenticated `/ws-control`; canonical initial state arrived and connection remained open during the probe |
| Integration card rendering | IMPLEMENTED / MOCK_TESTED | Six JavaScript behavioral checks, including canonical multi-account data and safe text rendering; no physical browser screenshot review |
| Natural-language AgentTurn → integration → final answer | BLOCKED / NOT LIVE_VALIDATED | Earlier model responses asked for account clarification; final retest encountered Groq 429 and invalid model output before dispatch |
| Drive and Contacts live access | NOT RUN | Drive authorization deferred; no Contacts consent was requested |

**A11 end-to-end live acceptance: NOT COMPLETE.** The Google connections are working; the final conversational path has not met the acceptance criteria. Successful API probes are not substitutes for successful model routing and grounded final responses.

## Bugs found and repaired

1. **Legacy dashboard/API mismatch.** The browser dashboard expected `plugin_name` and one top-level `account_id`. The canonical API supplies `name`, `key`, and per-account `accounts[]`. Replaced the integration rendering path with a canonical multi-account view of authorization, scopes, capabilities, service health, and recent action outcomes. Missing accounts are displayed explicitly rather than as `undefined`.
2. **Browser session lost on WebSocket refresh.** The dashboard used `/ws-cli`, which expects a raw device-secret handshake. A refreshed browser retains its HttpOnly session cookie but no in-memory secret, so the old socket authentication failed repeatedly. Switched the browser to the existing cookie-authenticated `/ws-control` channel and its existing event/approval protocol. Native `/ws-cli` and agent authentication remain unchanged. Transient disconnects back off; authentication failures return to login. Stale sockets cannot update the current view. Approval rows wait for server reconciliation rather than assuming that sending a request means approval succeeded.
3. **Incorrect authorization link.** Old cards linked to `/auth`, a JSON endpoint. Cards now use the existing `/connect` redirect when reviewing read-only authorization. No Drive authorization link was added, and no new write scopes are requested.
4. **Account authorization failure leaked across accounts.** A 401 from one account set service health to `auth_required`, which hid another healthy account's capabilities. Discovery now uses each account's actual authorization state; service cooldowns still apply. Account-scoped 401 handling remains enforced.
5. **Incomplete OAuth identity error handling.** Identity lookup timeouts and malformed/non-object identity responses could escape the normalized OAuth contract. They now fail safely without persisting partial credentials or exposing provider content. A caller-marked `authorization_required` token cannot silently refresh behind that gate.
6. **Denied OAuth consent left unusable state.** Callbacks required a code even when Google returned an authorization error. The callback now consumes valid state once and returns a safe failure for missing-code/denied-consent responses without attempting exchange.
7. **Malformed Gmail read could look successful.** An empty successful HTTP response could become a message with a missing ID. Such a response is now `PROVIDER_UNAVAILABLE / INVALID_RESPONSE`, with no invented message.
8. **Account discovery lacked actionable defaults.** Live models asked for an account despite one connected account. Canonical capability descriptions and argument schemas now expose the resolved default/only account alias, or the eligible aliases and explicit-selection requirement. No email address, Google subject ID, or credential is added to that model-facing metadata. The final live routing retest remained blocked by inference failures, so this improvement is offline-verified rather than claimed as a completed live fix.

## Files modified in this task

Existing files:

- `hub/api_integrations.py`
- `hub/integrations/google_auth.py`
- `hub/integrations/google_api.py`
- `hub/integrations/ecosystem.py`
- `hub/static/app.js`
- `hub/static/index.html`

Added files:

- `hub/static/integrations.js`
- `hub/static/dashboard-connection.js`
- `tests/dashboard_integrations.test.cjs`
- `tests/test_a11_refinement.py`
- `scripts/validate_integrations_readonly.py`
- This report.

The repository already had extensive uncommitted work. This list identifies this task's scope rather than claiming ownership of the entire working-tree diff. No commit or push was performed.

## Tests and regression

- Focused A11 foundation + refinement: **70 passed / 0 failed**.
- Consolidated regression: **811 passed / 0 failed / 0 skipped / 0 xfailed**. All **798** previous cases retained; **13** refinement checks added.
- JavaScript dashboard and existing UI task-sync checks: **16 passed / 0 failed**. Six are the new dashboard checks. The six new JS checks also run through one pytest wrapper, so these counts should not be added as distinct pytest cases.
- Python compilation and JavaScript syntax checks passed.
- No baseline tests were removed, skipped, or relaxed.

New coverage includes OAuth identity failures, denied-consent state consumption, multi-account 401 isolation, reauthorization gates, malformed reads, timeouts, serialized refresh, authenticated integration HTTP responses, cookie login/logout, canonical account selection metadata, integration rendering, and reconnect behavior. Existing A11 checks continue covering PKCE/scopes, Contacts/adapters, policy/privacy, high-risk approvals, credential redaction, and AgentTurn integration execution with mocked providers.

The external-network guard in `tests/conftest.py` stayed enabled. Full regression was run with the access needed by the existing local TLS/filesystem tests, not with the network guard removed.

## Live method and results

The opt-in runner starts a disposable Hub and the actual native Windows agent as ASUS. Only Gmail/Calendar account metadata is copied from the production database; credentials remain in the existing OS keyring and are loaded by the normal integration implementation. A disposable enrollment authenticates the validation agent. Production enrollment and database execution state are not reused or modified.

The test database marks copied non-revoked accounts as validation-only to prevent the unrelated legacy Calendar startup prefetch. Canonical authorization still comes from secure tokens and granted scopes. Registry selection, permissions/privacy checks, and structured execution are real.

A diagnostic boundary permits only `email.search`, `email.read`, and `calendar.list`; other proposed capabilities are blocked. The runner separately submits natural-language requests to the normal `/api/control/message` path. It uses the real configured reasoning providers without scripted model responses, forced routing, new quota allowances, or automatic approvals. Diagnostic reads are labeled separately from model-selected actions.

### Gmail

Real authorized search of the last fourteen days returned one message. A subsequent real read retrieved only headers: ID, date, sender, recipient, subject. No message body or attachment was requested. Reports record field names, counts, provider, status, latency, and provenance presence, not header values or resource IDs. The final read probe took approximately **0.73 seconds** for search plus metadata retrieval.

### Calendar

Real authorized retrieval over the next seven days returned three events with start/end, status, summary, and ID. The report contains no event titles/details or identifiers. The final probe took approximately **0.32 seconds**.

### Conversational AgentTurn path

The earlier live run returned account clarifications for Gmail and Calendar rather than dispatching the requested integrations. After account metadata was refined, the final bounded retest observed Groq HTTP 429, a model-invalid-output/HTTP 400 attempt, and provider cooldown waits; its 70-second validation window expired without a completed request. A preceding run also observed Gemini HTTP 503. No model-selected API read or grounded final answer was proven by these runs. No desktop fallback executed.

The running disposable task was stopped when the bounded validation session ended; this is not claimed as durable task completion or recovery proof. No pending approval was accepted automatically. These are real inference/model-capacity and selection limitations, not evidence that Google authorization is missing.

## Security and permission validation

- No emails sent; no calendar events, contacts, or Drive files changed; no delete/share action; no new scope consent; no Maps/billable API probe.
- Drive authorization remains deferred. Provider quotas, inference routing/failover, approval TTL, and general execution/recovery code were not altered.
- Existing A7 privacy, enrollment/RBAC, kill switch, and exact one-use write approvals remain enforced and regression-tested.
- No service contents were supplied to a reasoning model during the successful diagnostic reads. The natural-language probes did not complete an integration dispatch.
- Token values remain in the secure store/process boundary. Test fixtures use synthetic canaries. Validation reports omit credentials, raw provider bodies, account email addresses, message/event values, and service resource identifiers. Child runtime output is suppressed rather than copied into normal validation logs.
- Integration rendering uses DOM text nodes, so provider-controlled identity text is not interpreted as HTML.
- Browser refresh no longer requires retaining a device secret in JavaScript or browser storage; the existing HttpOnly session cookie is used.
- Frozen-file hashes match the previous baseline for AgentTurn, provider routing/failover/budgets, compound orchestration/recovery, acceptance prompts/scripts, and the external-network test guard.
- No phrase-specific request router, app-specific planner recipe, or canned successful result was added.

## Evidence

Artifacts are in:

`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a11-refinement-20261021`

- `regression-summary.json`, `regression.xml`, `regression.txt`, `suite-manifest.json`
- `focused2.xml`
- `live-02/live-report.json`, `live-03/live-report.json`, `live-04/live-report.json`
- `frozen-audit.json` and source hashes

`live-01` records a validation-harness startup failure caused by incorrectly copied account configuration; the copy was corrected before the successful service probes. This was not counted as a Google authorization failure or a live pass.

The runner's raw disposable databases contain local runtime state and are not intended as publishable reports. Use the minimized JSON reports when sharing evidence.

## Remaining blockers and next task

1. Finish real AgentTurn selection and final-answer acceptance using healthy permitted inference capacity. Start with the same default-account read requests; inspect safe decision/schema diagnostics if account clarifications persist. Do not widen OAuth scopes or weaken approval/privacy policy to make the benchmark pass.
2. Validate the repaired browser cards visually and exercise a normal refresh in the user's regular runtime. Transport and rendering contracts passed; a physical frontend review is still outstanding.
3. Leave Drive deferred and Contacts live testing pending legitimate existing consent. The adapters are mocked-tested, not live-validated here.
4. Keep `PRE_A11_COMPOUND_VALIDATION = OPEN`. No A12–A14 work or compound acceptance changes were performed.

**Recommended next task:** A11 conversational live-acceptance closure under healthy provider capacity, followed by the independent deferred compound acceptance. Do not label the entire A11 ecosystem live-complete or release-ready yet.

## Cortex Goal Meter

**82% — unchanged, planning estimate.** Real Google reads and the browser fixes reduce A11 validation debt, but the end-to-end AgentTurn criterion and earlier physical validation remain unproven. More green mocked checks alone do not justify increasing the whole-project estimate.

## Protocol references

The implementation retains Google's [OAuth authorization-code and refresh flow](https://developers.google.com/identity/protocols/oauth2/web-server) and Gmail's [metadata-header retrieval contract](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get). These protocol references support the implementation method, not the reported test outcomes.
