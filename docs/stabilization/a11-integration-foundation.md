# A11 integration ecosystem foundation

Date: 2026-10-20. Scope: integration foundation only. This is an implementation and offline validation report, not a claim of live Google-service acceptance or product release readiness.

## A11 FOUNDATION IMPLEMENTATION SUMMARY

Implemented seven structured Google adapters and thirteen canonical capabilities. They participate in the existing capability discovery, permissions, approvals, privacy checks, execution dispatcher, and AgentTurn result interpretation. Added account-aware OAuth, bounded results, separate service health, and a minimal functional Control Center panel.

No service-name natural-language router was added. AgentTurn, inference routing/failover, compound recovery, acceptance prompts, approval TTL, and the external-network test guard were preserved.

## INTEGRATION ARCHITECTURE

`hub/integrations/definitions.py` defines IntegrationCapability, AuthState, ScopeRequirement, ProviderConnection, strict argument models, effects, and service metadata. `google_api.py` supplies the IntegrationProvider interface and structured Google adapters. `google_auth.py` handles scoped consent and secure credentials. `ecosystem.py` owns IntegrationRegistry, account selection, authorization, execution policy, normalization, health, and provenance.

The path is user request → existing AgentTurn → canonical capability → existing control/policy gates → integration registry → structured adapter → normalized receipt → existing AgentTurn interpretation. There is no separate planner/executor/UI capability inventory.

## CANONICAL REGISTRY

IntegrationRegistry is the source for capability IDs, argument/result schemas, provider IDs, OAuth requirements, effects, privacy class, availability, auth state, and observed rate-limit metadata. The existing CapabilityRegistry and SystemRegistry project this information; Control Center consumes SystemRegistry. Unknown service health remains unknown until an actual request establishes it.

## PROVIDERS ADDED

| Provider | Adapter |
| --- | --- |
| Google Gmail | Gmail REST v1 |
| Google Calendar | Calendar REST v3 |
| Google Drive | Drive REST v3 |
| Google People / Contacts | People REST v1 |
| Google Places | Places text search |
| Google Geocoding | Geocoding v4 |
| Google Routes | Routes computeRoutes |

## CAPABILITIES ADDED

| Capability | Required grant | Effect |
| --- | --- | --- |
| email.search | gmail.readonly | READ |
| email.read | gmail.readonly | READ |
| email.send | gmail.send | SEND |
| calendar.list | calendar.events.readonly | READ |
| calendar.create | calendar.events | WRITE |
| calendar.update | calendar.events | WRITE |
| contacts.search | contacts.readonly | READ |
| drive.search | drive.metadata.readonly | READ |
| drive.read | drive.readonly | READ |
| drive.upload | drive.file | WRITE |
| maps.geocode | Enabled Maps project/key | READ |
| maps.place_search | Enabled Maps project/key | READ |
| routes.compute | Enabled Maps project/key | READ |

OAuth scope names above use Google's `https://www.googleapis.com/auth/` prefix. Maps requires both a configured key and explicit `CORTEX_GOOGLE_MAPS_ENABLED=1`; configuration alone is not proof that APIs or billing are enabled. DELETE, SHARE, and PERMISSION_CHANGE are modeled effects, with no corresponding action exposed in this foundation.

## OAUTH/AUTH MODEL

Reused the existing PKCE helper, Google client configuration, and OS-keyring CredentialStore. Added scoped OAuth2 authorization, explicit consent, expiring single-use callback state, verified Google account identity, access-token expiry, serialized refresh and refresh-token rotation, and revocation handling. An absent or invalid expiry cannot invent valid authorization. API 401 requires reauthorization; it does not falsely establish revocation.

Tokens stay in the existing local secure store. Database metadata records account identity, defaults, and verification state, not tokens. Auth-management endpoints require an authenticated administrator. No generic HTTP write endpoint bypasses canonical execution.

For later setup, configure the blank Google settings documented in `.env.example`, then request an authenticated `/integrations/gmail/auth?account_id=school&capabilities=email.search`. Follow its consent URL manually using the configured callback. Request send/write scopes only when those capabilities are explicitly wanted. The actual `.env` was not changed; no login or consent was performed during this task.

## SCOPE MODEL

Each capability declares its minimal grants. Connected, insufficient scope, expired, revoked, unconfigured, and provider unavailable are distinct states. Already-granted broader scopes can satisfy documented narrower grants; the implementation does not request broad access automatically. Scopes are checked again after refresh, before a service request.

## MULTI-ACCOUNT MODEL

Accounts have validated local aliases, provider identity, per-account scopes/state, and persisted defaults. Calls can select `account_id` explicitly. Ambiguous selection requires clarification. A default account lacking scope does not hide a capability another account can use. Write approvals bind both account alias and Google subject identity, so a default or identity change invalidates an earlier approval.

## PRIVACY/POLICY INTEGRATION

Capability privacy class feeds A7 classification and existing reasoning-provider eligibility. Sensitive results are minimized before continuation; disallowed model recipients do not receive them. Integration execution also evaluates the actual hub host locality rather than treating hub ownership as a privacy exemption. Enrollment, RBAC, kill switch, local/private-only restrictions, and write approval apply before external dispatch.

During regression, three unchanged A7 tests revealed that registering an unavailable hub-owned email capability could override an advertised worker's existing privacy/liveness selection. Fixed ExecutorSelector to avoid that shortcut for unavailable integrations, and added a direct cloud-hub privacy rejection test. No baseline assertions were weakened or migrated for A11.

## DIRECT-API VS UI SELECTION

Canonical metadata marks available structured capabilities with higher preference and records UI-fallback eligibility when unavailable. The catalog exposes this preference, and the registry provides capability-based selection. The model still chooses which capability satisfies the goal. No Gmail/Calendar prompt matching or app-specific click recipes were added. Metadata preference is offline-tested; correct live model selection remains to be validated.

## RESULT NORMALIZATION

Adapters expose whitelisted fields through a shared status/provider/capability/data/provenance/retrieved_at envelope. Collections, nesting, response bytes, and text are bounded. Coverage explicitly reports first-page-only projection. HTML-only email does not become a fabricated empty plain-text body. Drive uploads are new plain-text files; binary upload and Google document export are outside this foundation.

Successful writes require a provider resource ID. A dispatched action without a verifiable receipt is unverified, may have side effects, and is not safe for automatic replay.

## ERROR NORMALIZATION

AUTH_REQUIRED, SCOPE_MISSING, PERMISSION_DENIED, RATE_LIMITED, NOT_FOUND, INVALID_ARGUMENT, PROVIDER_UNAVAILABLE, NETWORK_ERROR, and CONFLICT are normalized without exposing raw provider bodies. Safe HTTP status/diagnostic metadata is retained. Integration cooldown and health are separate from inference-provider health. No provider-specific retry hack or write replay was added.

## PROVENANCE

Reads include provider, selected account, capability, retrieval time, and resource identifiers when returned. Model-facing results exclude unrelated response metadata and credential fields. Recent-action UI records contain capability/status/error metadata, not payload bodies.

## AGENTTURN INTEGRATION

The existing dispatcher generically delegates registered integration capabilities. Existing AgentTurn code was not modified. Offline probes exercise discovery, read execution, interpretation, privacy rejection, and a complete write approval round trip: no request before approval, one request after approval, and a final interpreted result. Mock transports and scripted models are used; these are not live email sends.

## CONTROL CENTER INTEGRATION

Added a minimal Settings panel displaying actual service health, accounts, auth state, granted scopes, capability consent requirements, and recent actions. Its default-account control persists through the authenticated endpoint. Existing approval controls remain in place. Frontend production build passed; the existing ten UI task-sync checks passed. Physical UI review and live connection-state acceptance were not performed. Running production processes were not restarted.

## HARDCODING AUDIT

The new integration path contains no natural-language regex trees, service-name prompt routing, or Notepad/Settings/Calculator/weather/Downloads scenario handlers. Capability-ID adapter branches describe external protocols, not user phrasing. Fixed schemas, scope implications, security validation, protocol endpoints, and policy messages are deterministic. Success content comes from normalized receipts and existing AgentTurn interpretation. This is an audit of the changed foundation path, not a claim that every historical fallback elsewhere in Cortex has been removed.

## SECURITY AUDIT

Credentials are stored through the existing keyring, never exposed as model tool arguments. Strict schemas reject extra fields. OAuth callback state expires and is consumed once; identity is checked before an alias can be rebound. Google calls use fixed origins, header credentials, bounded bodies, timeouts, and no redirect following. Service request URLs and OAuth callback queries are filtered from normal request logs. Normalized projections redact credential fields and current secret values; raw token/error bodies are not logged.

Every exposed write is tier 3 and requires the existing approval mechanism. Grants bind source, capability, account identity, and arguments, and are consumed once atomically. Changed/denied/expired approvals cannot dispatch; concurrent requests cannot consume a grant twice. No live write, delete, share, or permission change occurred.

## NEW TESTS

57 deterministic checks in `tests/test_a11_integrations.py`: all seven providers and thirteen adapters; auth and missing scopes; account selection; strict schemas; normalized reads/errors/provenance; API preference/UI eligibility; privacy and kill switch; credential canaries; OAuth consent/state/identity; refresh/revocation; actual-scope recheck; write approvals, changed bindings, concurrent consumption, and unverified receipts; complete AgentTurn read and attended write paths. All external transports are mocked and the external-network guard remains enabled.

## FULL REGRESSION

Final result: **798 passed / 0 failed / 0 skipped / 0 xfailed**. All 741 baseline cases retained; 57 A11 checks added. The consolidated manifest covers A1–A10, architecture/core intelligence, GUI/runtime controls, background/recovery, context compression, provider routing/failover, approval harness, and A11.

Evidence: `work/a11-foundation-20261020/regression-summary.json`, `regression.xml`, `regression.txt`, and `suite-manifest.json` in the chat workspace. The initial A7 failures and subsequent repair runs are preserved separately. Frozen-file hashes confirm no changes to AgentTurn, inference router/failover, compound orchestration/recovery, compound acceptance scripts/prompts, or `tests/conftest.py` during this task.

## LIVE VALIDATION

`LIVE_VALIDATION = AUTH_PENDING`

Read-only local inspection found configured OAuth client settings and a legacy Calendar credential that is expired with no recorded granted-scope metadata. The production database content hash was unchanged by inspection. Maps key and explicit Maps opt-in are absent. No service requests, token refreshes, inference calls, or account changes were performed. This does not establish live Google API behavior.

## PRE_A11_COMPOUND_VALIDATION STATUS

`PRE_A11_COMPOUND_VALIDATION = OPEN`

Earlier live Calculator/Paint cases were provider/model interrupted; clean attended Notepad validation remains pending. Their prompts, acceptance criteria, recovery behavior, and approval TTL were not changed. A11 foundation completion does not close this independent debt.

## A11 FOUNDATION COMPLETE YES/NO

**YES** — implementation and offline acceptance for this foundation are complete; live authentication remains pending as permitted by the task.

## READY FOR NEXT A11 SUBPHASE YES/NO

**YES** — scoped account authorization and read-only live integration acceptance can follow. The product is **not release-ready** on the strength of this offline gate.

## REMAINING VALIDATION DEBT

- Fresh scoped OAuth consent; verify real account identity, refresh/revocation, and multi-account behavior.
- Enable intended Google APIs and, if wanted, configure Maps project/key/billing with explicit opt-in.
- Read-only live acceptance through actual user input → provider → normalized result → grounded answer.
- Separately authorized live writes, with attended approvals and receipt verification.
- Physical Control Center validation after restarting with the new code.
- Independent Pre-A11 compound semantic acceptance under healthy provider capacity.
- Future ecosystem work: pagination, binary/file export support, additional providers, and larger action coverage. These are not implemented or claimed here.

## Protocol references

Implementation uses Google's [native-app OAuth/PKCE guidance](https://developers.google.com/identity/protocols/oauth2/native-app), [Gmail list API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list), [Calendar event API](https://developers.google.com/workspace/calendar/api/v3/reference/events/list), [Drive file API](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list), [People contact search](https://developers.google.com/people/api/rest/v1/people/searchContacts), [Places text search](https://developers.google.com/maps/documentation/places/web-service/text-search), [Geocoding v4](https://developers.google.com/maps/documentation/geocoding/geocoding), and [Routes compute directions](https://developers.google.com/maps/documentation/routes/compute_route_directions).
