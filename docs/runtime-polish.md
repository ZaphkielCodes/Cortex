# Runtime routing and timeout repair

The normal path is authenticated request -> RequestLifecycle -> AgentHarness -> safety controls -> AgentTurn -> model decision -> canonical capability validation -> permission/privacy and executor checks -> execution -> model interpretation -> saved final response.

## Removed assumptions

Ordinary request handling no longer uses the old harness situational/people regex tree, SemanticInterpreter phrase tree, GoalPlanner template plans, prefix stripping, app-specific execution recipes, or the mock provider's keyword action plans. CORTEX_LEGACY_COMPATIBILITY cannot reactivate them. The old computer-use target resolver no longer infers hotkeys or resumes from words such as "back" and "continue". The compatibility response composer preserves executor messages and provenance rather than inventing a capability-specific answer. AgentTurn owns natural result interpretation.

Safety stop and approval controls, capability schemas, protocol identifiers, provider host restrictions, numeric resource limits and conservative risk checks remain explicit. They are necessary contracts, not substitutes for model reasoning. Other historical modules/scripts and simulated fixtures remain in the repository; this change does not claim every literal or legacy helper has been removed.

Device names no longer grant capabilities, online status or administrative roles. Enrollment and advertised capabilities establish authority. Configure CORTEX_HUB_DEVICE_ID for a hub alias; native execution on the hub additionally requires CORTEX_ENABLE_HUB_DESKTOP=1 and a non-cloud locality. Remote world state cannot use hub foreground/telemetry as if it came from that remote device.

## Slow requests

Previously, context preparation could block before acknowledgment, the socket reader could wait on its own executor result, and synchronous desktop calls could block heartbeat/result handling. There was also a redundant executor wait after AgentTurn had already completed execution.

RequestLifecycle starts its acknowledgment clock before database/context/model work. The default acknowledgment budget is three seconds, capped at five; the desktop HTTP transport budget is eight seconds. Slow work receives a parent request task ID and continues under an owned background task. Preparation has a separate bounded deadline. AgentTurn execution remains a child task with its existing leases and approval checks. Final results are persisted and correlated to the same parent request ID. GUI polling can recover a saved final result if live notification fails. Cancellation and shutdown persist a truthful terminal state; interrupted work is not replayed automatically.

The hub keeps reading voice/executor WebSocket messages while requests prepare and run. The Windows agent dispatches commands through a serialized worker and retains command IDs/results across socket reconnects within the process. Duplicate command/request IDs with different inputs are rejected. This is not durable exactly-once execution across process crashes or unlimited replay history.

A real disconnect, stalled native OS call or unavailable provider can still prevent completion. Such cases remain errors or unverified outcomes. The fix avoids treating ordinary slow execution as a lost hub response and warns against blindly retrying an action. It does not manufacture success or promise that networks never time out.

## Validation (2026-10-18)

- Focused offline runtime suite: 240 passed. Includes slow preparation, final correlation, real hub socket receipt during blocked preparation, cancellation/shutdown, command deduplication, privacy approvals, executor continuity and general UI contracts. Providers and physical devices are simulated; these are not live model-quality tests.
- JavaScript task synchronization: 10 passed. Active desktop frontend build passed.
- Native TLS peer transfer checks: 5 passed outside the filesystem sandbox, whose hard-link restriction prevented the initial run.
- Broader historical A5-A10/architecture/observability/fallback selection: 105 passed, 26 failed. Failures remain visible: old tests require phrase-only interpretation, synchronous template plans, automatic ASUS privilege, omitted executor identity or model-free answers. They need migration to scripted model decisions and explicit enrollment/executor contracts. No tests were skipped to conceal them. This selection was run before the final generic composer and timeout-wording changes, which have separate focused coverage.

No live API requests, paid inference, application restart, deployment, commit or push was performed. Keys can be added later. Mock mode now explicitly reports that a reasoning provider is not configured and performs no guessed action. A11 is outside this change.

## Typed check after configuring a provider and restarting

Submit each request separately and wait for its final result:

1. "What app am I using rn?"
2. "What's my CPU usage?"
3. "What's the weather in [your city]?"
4. "Open my Downloads folder."
5. "Open Calculator and switch to scientific mode."
6. "Explain virtual memory simply."
7. "Make that explanation shorter."
8. "Teleport my laptop to Mars."

Check truthful results, context continuity and clear unsupported-request handling. For slow work, confirm Activity transitions from in progress to a final answer under the same task ID. Approval-dependent actions must still wait for confirmation. Do not expect model reasoning to work before a valid provider is configured.
