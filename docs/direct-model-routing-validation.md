# Direct routing validation — October 18, 2026

Applied to the existing working checkout without committing or resetting its
other changes. OpenRouter inference/router implementations, environment
settings, deployment configuration and service-specific test references were
removed. The legacy credential-redaction pattern in tracing is intentionally
retained so old keys cannot leak into logs.

## Passing focused checks

194 passed, one google-genai/Python 3.14 deprecation warning, in 10.17 seconds.
The run covered direct HTTP transport, quota/cooldown failover, recipient pinning,
history and evidence preservation, cancellation, malformed/incomplete output,
credential-safe errors, configuration, real AgentTurn wiring with mocked HTTP,
CPU execution/result interpretation, screenshot transport, private local routing,
approvals, voice task delivery, GUI controls, reasoning/reference context, task
recovery, executor continuity and architecture repair. Existing tests also probe
current app, weather, Downloads, Calculator scientific mode, conversation,
corrections and unsupported requests using scripted model/device boundaries.

The offline `test_llm_failover.py` probe also passed: a mocked Groq 429 cooled the
Qwen route down and selected GPT-OSS without executing any computer action.

JUnit results are saved in this chat's workspace under
`work/direct-routing/focused-results.xml`.

## Broader regression results

A separate 213-check run produced **195 passed and 18 failed**. This is not a
green full regression suite. Failures are in older runtime expectations:

- A5: six physical-style natural-language routing probes and one self-knowledge test.
- A6, A7 and A8: one self-knowledge test in each suite.
- Phase 8R: a test forbidding model calls for time/date and a voice-latency test
  supplying a legacy non-AgentDecision response.
- Live context grounding: six probes relying on the legacy/mock path.

The mock provider explicitly rejects AgentTurn requests; several older tests
still assume deterministic natural-language routing. Those expectations were not
restored or skipped to make the report green. Component/invariant checks in the
A5–A10 suites were included in that run. A complete pre-change baseline was not
captured, so these failures are reported without claiming a measured zero-regression
comparison.

## Boundaries

No paid fallback, provider signup, billing change, service restart, A11 work,
commit or push was performed. Tests used isolated test state; the existing test
database fixture was restored from its pre-run backup. The production database
SHA-256 was unchanged.

Live provider authentication, current endpoint availability, real-model answer
quality and physical desktop acceptance remain unverified until keys are entered
and live probes are run. Local quota estimates are not a guarantee of provider
allowances or account billing status.
