# Production natural-language hardcoding audit

Scanned Python sources under hub/ and agent/. Found 119 candidate sites; raw line-level results are in hardcoding-candidates.json. Static matching identifies candidates, not proof of runtime reachability.

| Component | Entry point | Finding |
|---|---|---|
| `hub/agent_harness.py` | `sanitize_for_speech` | Allowed: internal-error redaction; no request interpretation. |
| `hub/main.py` | `sanitize_for_speech` | Delegates to the shared generic sanitizer; removed weather/screenshot-specific error branches. |
| `hub/pipeline/deterministic_router.py` | `DeterministicSafetyRouter.route` | Allowed: pending approval and kill-switch state controls. |
| `hub/approval_resolver.py` | `resolve_approval_intent` | Allowed: explicit approval vocabulary within a pending confirmation. |
| `hub/system_registry.py` | `SystemRegistry.explain_capability` | Fixed: exact canonical IDs only; no weather/Gmail/physical-fabrication query matching. |
| `hub/system_registry.py` | `SystemRegistry.answer_query` | REMAINING compatibility NL tree: identity, model, capabilities and connected devices. No production callers found; ordinary AgentTurn does not invoke it. |
| `hub/target_resolver.py` | `TargetResolver.resolve / ground_reference` | REMAINING compatibility NL tree: action verbs, app names, files, counts, collection cleanup, pronouns, foreground/previous-app and device suffixes. Used by the legacy TargetGrounder adapter; not ordinary AgentTurn. |
| `hub/goals.py` | `GoalManager.handle_natural_language` | REMAINING compatibility NL tree: create/list/run/pause/resume/cancel goals and autonomy controls. No production callers found. |
| `hub/task_ownership.py` | `TaskOwnershipManager.answer_self_knowledge_query` | REMAINING compatibility NL tree: ownership, executor, failover reasons, waiting and reconnect questions. Replaced in the primary model path by structured system.runtime_state. |
| `hub/file_transfer.py` | `FileTransferManager.answer_self_knowledge_query` | REMAINING compatibility NL tree: file location, transfer progress, transport, resume and receipt questions. Primary model path now has structured transfer facts. |
| `hub/routines.py` | `RoutineManager.handle_natural_language and activity categorization` | REMAINING NL matching for habits, working times/free time and semantic activity labels. Request handler has no production callers; activity categorization remains active. |
| `hub/workload.py` | `WorkloadManager.handle_natural_language and deadline/effort parsers` | REMAINING NL matching for work entry/status/priority/deadlines and natural-language date/effort fields. Request handler has no production callers; structured-field parsers remain callable. |
| `hub/social.py` | `SocialContextManager / intent and sentiment helpers` | REMAINING ACTIVE context heuristics: phrases classify social intent, likes/dislikes and relevant people facts. ContextManager still invokes this component. |
| `hub/session_context.py` | `SessionContextManager._update_session` | REMAINING ACTIVE context heuristic: regex calendar-topic inference and person-reference fallback. They update context rather than directly dispatch actions. |
| `hub/collection_classifier.py` | `CollectionClassifier._extract_goal_categories / item classifier` | REMAINING ACTIVE goal-worker heuristics: category and cleanup/removal intent inferred from wording and item names. GoalRunner can invoke the classifier for collection operations; this blocks an all-hardcoding-removed claim. |
| `hub/people.py` | `PeopleManager.resolve_person` | REMAINING entity-reference heuristics: aliases/pronouns matched against known people. These resolve data references; no arbitrary desktop capability dispatch. |
| `hub/privacy.py` | `classify_task_privacy` | Allowed security: conservative capability/data/path risk classification. It cannot grant execution permission. |
| `hub/computer_use_controller.py` | `evaluate_action_risk` | Allowed safety: high-impact terms only strengthen approval requirements. Raw-target action inference has been removed. |
| `agent/voice/controller.py` | `is_sleep_command / session controls` | Allowed exact system control and voice state-machine vocabulary. Audio-confidence rejection is not application routing. |
| `agent/app_control.py and hub/environment.py` | `app identifiers / known folders` | Allowed exact OS control identifiers and known-folder paths; these consume structured arguments rather than interpret complete user requests. |

The primary request pipeline has no Notepad/Settings/weather/CPU/Downloads/Calculator phrase branch. Structured result validation for missing weather/telemetry values is a truthfulness check, not NL routing. App aliases/known-folder identifiers remain exact system control paths.

Major language heuristics remain in context and background collection paths. Therefore this audit does NOT certify that all production NL hardcoding is gone. These are explicit stabilization blockers, not hidden exceptions.
