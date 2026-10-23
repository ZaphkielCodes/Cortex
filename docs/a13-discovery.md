# A13 capability discovery — interrupted acceptance checkpoint

Current status: **A13 COMPLETE = YES**, following [A13.1 native/live acceptance](a13-1-acceptance.md), with 850 regression tests passing. A11 remains BLOCKED, pre-A11 compound validation remains OPEN, and A14 has not started. The interrupted checkpoint below is retained as historical evidence, not the current closure decision.

## Original interrupted checkpoint

October 22, 2026. At this checkpoint A13 was incomplete. Computer Use was stopped by the user with physical Escape while opening Workspace. No further desktop input was sent. The acceptance-owned Hub, ASUS Agent and native application were stopped cleanly.

## Implementation

The implementation extends the canonical capability catalog and A5 DeviceHandles. It does not create another executor registry. The agent derives support from registered native handlers plus existing session handlers; there are no ASUS/Lenovo phrase or device-profile lists.

`shared/discovery.py` defines strict protocol-v1 advertisements: bound device identity, executor kind, generation, bounded lifetime, capability/version, typed input/output schemas, canonical permission/risk/privacy metadata and evidence category. Its portable schema is `shared/discovery/v1.schema.json`. Peer-supplied successful-verification timestamps, duplicate IDs, remote schema references and unknown fields are rejected. Last verification is recorded by the Hub only after a correlated success receipt; it is receipt evidence, not independent hardware attestation.

`hub/discovery.py` reconciles advertisements atomically against trusted canonical definitions, storing bounded evidence on existing DeviceHandles. Presence, availability, authorization and immediate executability are separate fields. Schema/version mismatches remain non-executable; changed capability sets require a new generation. Expiry, current authenticated session, schema/policy changes, trust and revoked per-device permissions are checked at dispatch. Existing planning, approvals, privacy and OAuth policies remain authoritative. Legacy name-list compatibility is logged and marked protocol 0; legacy peers must refresh advertisements before their lease expires.

`GET /api/discovery` is authenticated and read-only. Worker views are restricted to their own device, and integration/account metadata is admin-only. Integration candidates derive from existing A11 adapters, stored account state, scopes and health/cooldown. Accounts use opaque display references. Drive discovery is explicitly deferred, and no OAuth scopes are requested. Integration execution retains A11's own immediate account/scope/privacy checks.

The A12 workspace projects discovery into existing typed tables and a read-only expandable disclosure. It adds no executable UI action. The protocol remains version 1 with an additive `capability_discovery` provenance category; the bundled client validator and exported schema are updated together.

Existing transfer/voice session handlers now have canonical discovery metadata, marked `discovery_only` so they do not become new model-facing tools. A10 handler implementations and frozen AgentTurn/model routing/compound orchestration were not rewritten.

## Files changed

Added: `shared/discovery.py`, `shared/discovery/v1.schema.json`, `agent/discovery.py`, `hub/discovery.py`, `hub/api_discovery.py`, `tests/test_a13_discovery.py`, this document.

Modified: `agent/agent.py`, capability-detection code in `agent/executor.py`, `hub/models.py`, `hub/main.py`, `hub/capability_registry.py`, `hub/device_handle.py`, `hub/permissions.py`, `hub/control_layer.py`, `hub/workspace.py`, `shared/workspace/v1.schema.json`, `agent/ui/workspace-state.js`, `agent/ui/DynamicWorkspace.jsx`. Frontend assets were rebuilt. No commit/push was performed.

## Verified results at interruption

| Area | Result |
| --- | --- |
| Final focused A13 deterministic checks | **23 passed / 0 failed** |
| Earlier combined A13/A12 focused gate, before the last receipt test | 31 passed / 0 failed |
| Frontend production build | PASS, 26 modules; existing large-bundle warning |
| Real Hub + ASUS advertisement | PASS: 42 device capabilities discovered, 34 immediately executable and 8 requiring further policy conditions |
| Real permitted execution | PASS: `ui.wait`, one second, existing authenticated CLI → ASUS executor → success |
| Success correlation | Hub recorded `ui.wait` last verification after the matching command receipt |
| Native application launch | PASS: actual PyQt `Cortex Control Center` observed |
| Native discovery table inspection | BLOCKED: user stopped Computer Use before Workspace opened |
| Real ASUS disconnect/reconnect and restored discovery | NOT RUN |
| Forged-advertisement live probe | NOT RUN; deterministic rejection coverage only |
| Android discovery | NOT LIVE-TESTED |
| Final complete regression | In progress at handoff; no final pass count claimed |

Real execution ID: `ce7c6a4b7a3d4f97924e3553fef38488`. No inference calls, paid voice or external account writes were used. Real discovered names, eligibility reasons and verification state are preserved in `lifecycle.json`; no capability beyond that evidence is claimed live.

Two test processes initially overlapped on the repository's shared test database. Their conflicting results were discarded, the overlap was stopped, and the focused gate was rerun serially. The final consolidated run uses the 823-case baseline plus 23 new A13 cases; its actual result must be read when it finishes, rather than assuming 846 passes. An earlier sandboxed TLS failure passed when rerun with the existing required filesystem access. No historical tests were weakened.

## Completion and next steps

IMPLEMENTED: discovery protocol, reconciliation, dispatch checks, read API and workspace projection.

MOCK_TESTED: identity/schema rejection, generation/version changes, expiry, reconnect, permissions, ownership, OAuth/multi-account limits, provider recovery, cross-device isolation and correlated receipts.

LIVE_VALIDATED: authenticated ASUS support advertisement and one permitted capability execution; native app launch only.

BLOCKED/UNVERIFIED: native discovery rendering/transitions, real disconnected/reconnected ASUS eligibility, live forged-advertisement rejection, and final consolidated regression outcome.

Resume by checking the saved regression result, fixing genuine failures, then running the bounded native discovery lifecycle and security acceptance. Do not close A13 from unit tests alone.

```text
A13_COMPLETE = NO
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A14 = NOT_STARTED
CORTEX_GOAL_METER = 82% (unchanged planning estimate)
```

Artifacts: `C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\a13-discovery`. Key files: `runtime.json`, `execution.json`, `lifecycle.json`, `suite-manifest.json`, and the final regression artifacts when available. Local diagnostic logs may contain existing runtime context and are not public sharing artifacts.
