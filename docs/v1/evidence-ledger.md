# Cortex v1 evidence ledger

Snapshot: 2026-10-23. Status labels can coexist: IMPLEMENTED means code exists; AUTOMATED_TESTED means a bounded automated contract passed; LIVE_VALIDATED applies only to the stated experiment; BLOCKED means a required result did not complete; DEFERRED removes work from the initial critical path. No label alone establishes production readiness.

| Subsystem | Classification / implemented and automated evidence | Live evidence and limits | v1 role |
|---|---|---|---|
| Hub, enrolled device registry and transport | IMPLEMENTED; AUTOMATED_TESTED in A1–A9, auth and lifecycle suites | LIVE_VALIDATED ASUS startup, authenticated reconnect, 42 advertisements; second physical-device v1 task unverified | Required |
| AgentTurn and single-agent execution | IMPLEMENTED; AUTOMATED_TESTED agent_turn_runtime, reasoning_context, reference_resolution | LIVE_VALIDATED simple arithmetic, correction and app launch in usability review; broad reliable task completion BLOCKED | Required |
| Persistent memory and context compression | IMPLEMENTED; AUTOMATED_TESTED context_compression, reasoning/reference/recovery suites | Correction preserved context; exhaustive long-conversation grounding, stale context and isolation under live workloads unverified | Required, minimize context |
| Router, provider failover and inference contracts | IMPLEMENTED; AUTOMATED_TESTED direct_routing, a10_5_routing, failover_budget, provider_capacity | Real Groq responses; 400/413/429 and Gemini failures in review; no guaranteed capacity or measured default model; latest Gmail interpretation provider-unavailable | Required; BLOCKED reliability |
| Permissions, privacy, approvals, authentication | IMPLEMENTED; AUTOMATED_TESTED control_auth_boundary, privacy/private_reasoning and A5–A10 suites | LIVE_VALIDATED invalid credentials401, forged source403, unowned control403, executable workspace content422; Gmail consent enforced, Calendar hard denial. Cancelled execution can leave stale approval row | Required |
| Task lifecycle, cancellation/recovery | IMPLEMENTED; AUTOMATED_TESTED request_lifecycle, task_recovery_context, GUI controls | LIVE_VALIDATED active API cancellation, terminal cancellation409 after fix, 18 terminal rows survive restart; ambiguous interrupted-side-effect recovery not freshly proven | Required |
| Gmail/Calendar integrations | IMPLEMENTED; AUTOMATED_TESTED a11_integrations/refinement | LIVE_VALIDATED real email.search and calendar.list, default primary resolution. Gmail fresh approved retry still model_provider_unavailable; Calendar interpretation policy-denied. Combined daily task never accepted | Required; BLOCKED |
| Native PyQt/React workspace and IPC | IMPLEMENTED; AUTOMATED_TESTED a12_workspace, GUI/runtime suites | A12.1 recorded native lifecycle/cancellation pass; A13.1 native discovery pass. Later usability Workspace navigation interrupted, Overview and IPC connected only; do not erase earlier bounded evidence or infer new per-case UI passes | Required text presentation |
| Dynamic capability discovery | IMPLEMENTED; AUTOMATED_TESTED a13_discovery | LIVE_VALIDATED A13.1 native view, 42 online/offline/restored capabilities, 7 rejected forged advertisements without mutation, actual ui.wait. Not exhaustive handler validation | Required |
| Desktop/compound UI and files | IMPLEMENTED; AUTOMATED_TESTED A10/compound/window/general_ui | LIVE_VALIDATED launch and waits; scientific-mode task stopped at approval, file copy cancelled before dispatch. CPU retest lacks fresh telemetry receipt. PRE_A11_COMPOUND_VALIDATION OPEN | Stage 6, not daily-task dependency |
| A14 multi-agent orchestration | IMPLEMENTED; AUTOMATED_TESTED a14_orchestration and protocol contracts | Real four-worker graph admitted, all workers completed, independent overlap1.318527s, dependencies ordered. False conflicts on generic claim keys blocked synthesis; full live acceptance OPEN | DEFERRED; implementation frozen |
| Voice and speaker identity | IMPLEMENTED; AUTOMATED_TESTED voice delivery/speech reliability and historical phase suites | Review used mocked voice; IPC connection is not live speech evidence. Voice reconnect remains uncertain | Voice Stage 3; speaker identity DEFERRED |
| Android and device transfer | IMPLEMENTED scaffolding/protocols; AUTOMATED_TESTED peer_transfer suites | No v1 second-device/Android daily-task acceptance evidence; no live BLE/Wi-Fi Direct throughput proof | Android Stage 5; transfer DEFERRED |
| Always-on deployment, multi-user/family | Deployment/policy foundations exist; automated portions in historical phases | No always-on host or family onboarding/weekly-use evidence established | Stages 4/7/8 |
| Orb, 2D/3D workspace and extra services | Orb and extra integration definitions retained; selected API contracts tested | Generative workspace deferred; no general live acceptance claimed for extra services | DEFERRED |

## Evidence sources and availability
Repository reports: [usability review](../usability-review-2026-10-22.md), [frozen findings](../usability-frozen-findings-2026-10-22.md), [A11](../stabilization/a11-final-acceptance.md), [A12.1](../a12-1-native-acceptance.md), [A13.1](../a13-1-acceptance.md), [A14.4](../a14-4-acceptance.md). These are historical reports, not new live trials in Stage 0.

Local evidence root (not shipped because it can contain private runtime context): `C:/Users/abhin/Documents/Codex/2026-10-15/referenced-chatgpt-conversation-this-is-an/work/`. Relevant directories: `usability-review-20261022`, `gmail-resume`, `a13-1-acceptance`, `a12-1-acceptance`, `a11-closure-20261021`, `a14-4-acceptance`. Old report links/screenshots may be unavailable on another machine; source reports explicitly distinguish retained summaries from raw live evidence. Stage 0 sanitized regression/manifest/hash results are saved alongside this ledger.

Latest Gmail retry: request_06a3059c21d407e47917644d, email.search real success, Groq sharing approved HTTP200, final model_provider_unavailable. No headers/bodies requested in that retry. This is not a daily-summary pass. Raw provider status for that final failure has not been established here.

## Critical blockers for the new product
1. Inference request size, quota admission, schema failures and latency prevent dependable final answers. Prior HTTP413 rate_limit_exceeded is not conclusively a context-window error; retain uncertainty.
2. Read-only Google execution succeeds but recipient consent/policy and final interpretation do not yet yield a complete combined summary.
3. Freshness/grounding and truthful task-state presentation need measured complete-case checks, not only completion flags.
4. Approval expiry/stale rows and historical active-task counts confuse ordinary use.
5. Native broad task acceptance, second-device availability and live audio remain bounded or missing evidence.
A14 synthesis defect is retained technical debt, not a v1 blocker to reopen.

## Preserved history
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A12_FOUNDATION = COMPLETE
A13 = COMPLETE
A14_IMPLEMENTATION = FROZEN
A14_LIVE_ACCEPTANCE = OPEN
A12_2_GENERATIVE_WORKSPACE = DEFERRED

See [baseline audit](baseline-audit.md) for the current measured regression and security boundary. The prior 935 report was superseded by 947 in the usability review; neither is assumed to be today's result.
