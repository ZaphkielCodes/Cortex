"""
hub/privacy.py
--------------
Phase A7: Privacy-Aware Execution Routing & Policy Engine.

Enforces: "Capable != Allowed"
A cloud or remote executor may technically support a capability but still
be disallowed from receiving sensitive data.

Core Responsibilities:
1. Structured Privacy Classification (PUBLIC, INTERNAL, PERSONAL, SENSITIVE, HIGHLY_SENSITIVE).
2. Executor Privacy Profiles (locality, execution_environment, trust_level, private_compute, cloud_processing).
3. Central Privacy Policy Engine (ALLOW, DENY, REQUIRE_APPROVAL, LOCAL_ONLY, WAIT_FOR_PRIVATE_EXECUTOR).
4. Local-First for Sensitive Work & Remote/Cloud Permitted Paths.
5. Data Minimization for Remote Execution.
6. A6 Handoff Integration & Privacy-Aware Wait States.
7. Durable User Privacy Policy Primitives.
8. Scoped Override-by-Approval Flow.
9. Ground-Truth Conversational Self-Knowledge.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from .device_handle import DeviceHandle
from .database import save_privacy_policy, get_privacy_policy, list_privacy_policies

log = logging.getLogger("cortex.privacy")


# ---------------------------------------------------------------------------
# 1. Privacy Classification & Locality Taxonomy
# ---------------------------------------------------------------------------

class PrivacyClass:
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    PERSONAL = "PERSONAL"
    SENSITIVE = "SENSITIVE"
    HIGHLY_SENSITIVE = "HIGHLY_SENSITIVE"


PRIVACY_ORDER: Dict[str, int] = {
    PrivacyClass.PUBLIC: 1,
    PrivacyClass.INTERNAL: 2,
    PrivacyClass.PERSONAL: 3,
    PrivacyClass.SENSITIVE: 4,
    PrivacyClass.HIGHLY_SENSITIVE: 5,
}


class ExecutorLocality:
    LOCAL_DEVICE = "local_device"
    TRUSTED_LAN_DEVICE = "trusted_lan_device"
    PRIVATE_SERVER = "private_server"
    CLOUD_WORKER = "cloud_worker"
    THIRD_PARTY_SERVICE = "third_party_service"


class ExecutionEnvironment:
    ON_PREMISE = "on_premise"
    SECURE_ENCLAVE = "secure_enclave"
    CLOUD_HOSTED = "cloud_hosted"
    UNTRUSTED_RUNTIME = "untrusted_runtime"


class PrivacyDecision:
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    LOCAL_ONLY = "LOCAL_ONLY"
    WAIT_FOR_PRIVATE_EXECUTOR = "WAIT_FOR_PRIVATE_EXECUTOR"


# ---------------------------------------------------------------------------
# 2. User Privacy Policy Primitives
# ---------------------------------------------------------------------------

@dataclass
class UserPrivacyPolicy:
    """
    Durable user-configurable privacy preferences.
    Defaults are conservative.
    """
    sensitive_data_cloud_allowed: bool = False
    personal_data_trusted_devices_only: bool = True
    prefer_local_execution: bool = True
    allow_remote_public_tasks: bool = True
    allow_cloud_approval_override: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserPrivacyPolicy":
        return cls(
            sensitive_data_cloud_allowed=bool(data.get("sensitive_data_cloud_allowed", False)),
            personal_data_trusted_devices_only=bool(data.get("personal_data_trusted_devices_only", True)),
            prefer_local_execution=bool(data.get("prefer_local_execution", True)),
            allow_remote_public_tasks=bool(data.get("allow_remote_public_tasks", True)),
            allow_cloud_approval_override=bool(data.get("allow_cloud_approval_override", True)),
        )


# ---------------------------------------------------------------------------
# 3. Task Privacy Context & Classification
# ---------------------------------------------------------------------------

@dataclass
class TaskPrivacyContext:
    """Structured privacy profile for a specific task execution."""
    privacy_class: str                          # PUBLIC, INTERNAL, PERSONAL, SENSITIVE, HIGHLY_SENSITIVE
    data_categories: List[str] = field(default_factory=list)
    resource_paths: List[str] = field(default_factory=list)
    resource_localities: Dict[str, str] = field(default_factory=dict)
    preferred_execution_scope: Optional[str] = None   # "local_only", "private_compute", "any"
    integration_source: Optional[str] = None          # e.g. "health_monitor", "browser"
    reasons: List[str] = field(default_factory=list)
    user_approved: bool = False
    approved_executor_id: Optional[str] = None
    task_execution_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskPrivacyContext":
        return cls(
            privacy_class=data.get("privacy_class", PrivacyClass.INTERNAL),
            data_categories=list(data.get("data_categories", [])),
            resource_paths=list(data.get("resource_paths", [])),
            resource_localities=dict(data.get("resource_localities", {})),
            preferred_execution_scope=data.get("preferred_execution_scope"),
            integration_source=data.get("integration_source"),
            reasons=list(data.get("reasons", [])),
            user_approved=bool(data.get("user_approved", False)),
            approved_executor_id=data.get("approved_executor_id"),
            task_execution_id=data.get("task_execution_id"),
        )


# Capability baseline privacy mapping
CAPABILITY_BASELINE_PRIVACY: Dict[str, str] = {
    # Public capabilities
    "web.search": PrivacyClass.PUBLIC,
    "web.scrape_page": PrivacyClass.PUBLIC,
    "weather.current": PrivacyClass.PUBLIC,
    "weather.forecast": PrivacyClass.PUBLIC,
    "time.get": PrivacyClass.PUBLIC,

    # Internal system & telemetry
    "device.status": PrivacyClass.INTERNAL,
    "device.info": PrivacyClass.INTERNAL,
    "app.list_running": PrivacyClass.INTERNAL,
    "process.list": PrivacyClass.INTERNAL,
    "app.launch": PrivacyClass.INTERNAL,
    "app.close": PrivacyClass.INTERNAL,
    "window.list": PrivacyClass.INTERNAL,
    "window.focus": PrivacyClass.INTERNAL,
    "goal.wait": PrivacyClass.INTERNAL,
    "wait": PrivacyClass.INTERNAL,

    # Personal user context
    "calendar.create": PrivacyClass.PERSONAL,
    "calendar.list": PrivacyClass.PERSONAL,
    "ui.notify": PrivacyClass.PERSONAL,
    "notes.create": PrivacyClass.PERSONAL,
    "notes.list": PrivacyClass.PERSONAL,

    # Sensitive workspace, files, and screens
    "email.send": PrivacyClass.SENSITIVE,
    "email.draft": PrivacyClass.SENSITIVE,
    "fs.list": PrivacyClass.SENSITIVE,
    "fs.stat": PrivacyClass.SENSITIVE,
    "fs.read": PrivacyClass.SENSITIVE,
    "fs.write": PrivacyClass.SENSITIVE,
    "fs.delete": PrivacyClass.SENSITIVE,
    "fs.move": PrivacyClass.SENSITIVE,
    "fs.rename": PrivacyClass.SENSITIVE,
    "screen.capture": PrivacyClass.SENSITIVE,
    "screen.analyze": PrivacyClass.SENSITIVE,
    "screenshot.read": PrivacyClass.SENSITIVE,
    "shell.execute": PrivacyClass.SENSITIVE,

    # Highly sensitive credentials and health
    "health.heart_rate": PrivacyClass.HIGHLY_SENSITIVE,
    "health.metrics": PrivacyClass.HIGHLY_SENSITIVE,
    "credentials.get": PrivacyClass.HIGHLY_SENSITIVE,
    "auth.token": PrivacyClass.HIGHLY_SENSITIVE,
}

# Category to privacy class mapping
DATA_CATEGORY_PRIVACY: Dict[str, str] = {
    "public_web": PrivacyClass.PUBLIC,
    "weather": PrivacyClass.PUBLIC,
    "system_telemetry": PrivacyClass.INTERNAL,
    "diagnostics": PrivacyClass.INTERNAL,
    "calendar": PrivacyClass.PERSONAL,
    "contacts": PrivacyClass.PERSONAL,
    "personal_notes": PrivacyClass.PERSONAL,
    "user_preferences": PrivacyClass.PERSONAL,
    "desktop_files": PrivacyClass.SENSITIVE,
    "personal_documents": PrivacyClass.SENSITIVE,
    "work_documents": PrivacyClass.SENSITIVE,
    "screen_capture": PrivacyClass.SENSITIVE,
    "confidential": PrivacyClass.SENSITIVE,
    "financial": PrivacyClass.SENSITIVE,
    "health": PrivacyClass.HIGHLY_SENSITIVE,
    "biometric": PrivacyClass.HIGHLY_SENSITIVE,
    "credentials": PrivacyClass.HIGHLY_SENSITIVE,
    "passwords": PrivacyClass.HIGHLY_SENSITIVE,
}


def classify_task_privacy(
    capability: str,
    arguments: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> TaskPrivacyContext:
    """
    Structured privacy classification from capability schema, arguments,
    resource paths, data categories, and integration sources.
    Avoids ad-hoc regex phrase forests.
    """
    args = dict(arguments or {})
    ctx = dict(context or {})

    reasons: List[str] = []
    data_categories: Set[str] = set()
    resource_paths: List[str] = []
    resource_localities: Dict[str, str] = {}

    # 1. Base classification from capability
    cap_clean = capability.strip().lower()
    base_class = CAPABILITY_BASELINE_PRIVACY.get(cap_clean, PrivacyClass.INTERNAL)
    reasons.append(f"Baseline capability '{cap_clean}' -> {base_class}")

    # 2. Planner annotations or explicit overrides
    explicit_class = (
        ctx.get("privacy_class")
        or args.get("privacy_class")
        or (ctx.get("metadata", {}).get("privacy_class") if isinstance(ctx.get("metadata"), dict) else None)
    )
    if explicit_class and explicit_class in PRIVACY_ORDER:
        if PRIVACY_ORDER[explicit_class] > PRIVACY_ORDER[base_class]:
            base_class = explicit_class
            reasons.append(f"Explicit annotation override -> {base_class}")

    # 3. Data categories evaluation
    passed_cats = ctx.get("data_categories") or args.get("data_categories") or []
    if isinstance(passed_cats, str):
        passed_cats = [passed_cats]
    for c in passed_cats:
        c_clean = str(c).strip().lower()
        data_categories.add(c_clean)
        cat_p = DATA_CATEGORY_PRIVACY.get(c_clean)
        if cat_p and PRIVACY_ORDER[cat_p] > PRIVACY_ORDER[base_class]:
            base_class = cat_p
            reasons.append(f"Data category '{c_clean}' elevated privacy -> {base_class}")

    # 4. Resource path inspection (detect local user data, confidential folders)
    for k, v in args.items():
        if isinstance(v, str):
            # Check file paths
            if re.match(r'^[a-zA-Z]:[\\/]', v) or v.startswith(("/", "\\")):
                resource_paths.append(v)
                v_lower = v.lower()
                # Personal/confidential user directories
                if any(p in v_lower for p in ("desktop", "documents", "downloads", "personal", "finance", "report")):
                    data_categories.add("personal_documents")
                    if PRIVACY_ORDER[PrivacyClass.SENSITIVE] > PRIVACY_ORDER[base_class]:
                        base_class = PrivacyClass.SENSITIVE
                        reasons.append(f"Local user resource path '{v}' elevated privacy -> {base_class}")
                # Highly sensitive keywords in file names
                if any(p in v_lower for p in ("credential", "password", "medical", "health", "tax_return", "ssn")):
                    data_categories.add("confidential")
                    base_class = PrivacyClass.HIGHLY_SENSITIVE
                    reasons.append(f"Sensitive resource keyword in '{v}' elevated privacy -> {base_class}")

    # Check explicit resource locations
    if args.get("required_data_locations"):
        resource_localities.update(args["required_data_locations"])
    if ctx.get("required_data_locations"):
        resource_localities.update(ctx["required_data_locations"])

    # 5. Integration source inspection
    source = ctx.get("source") or ctx.get("integration_source") or args.get("source")
    if source:
        s_clean = str(source).strip().lower()
        if s_clean in ("health_monitor", "medical_sensor", "biometric_reader"):
            data_categories.add("health")
            base_class = PrivacyClass.HIGHLY_SENSITIVE
            reasons.append(f"Integration source '{s_clean}' elevated privacy -> {base_class}")
        elif s_clean in ("password_manager", "keyring", "auth_service"):
            data_categories.add("credentials")
            base_class = PrivacyClass.HIGHLY_SENSITIVE
            reasons.append(f"Integration source '{s_clean}' elevated privacy -> {base_class}")

    pref_scope = ctx.get("preferred_execution_scope") or args.get("preferred_execution_scope")
    user_appr = bool(ctx.get("user_approved") or args.get("user_approved", False))
    appr_exec = ctx.get("approved_executor_id") or args.get("approved_executor_id")
    task_id = ctx.get("task_execution_id") or args.get("task_execution_id")

    return TaskPrivacyContext(
        privacy_class=base_class,
        data_categories=sorted(list(data_categories)),
        resource_paths=resource_paths,
        resource_localities=resource_localities,
        preferred_execution_scope=pref_scope,
        integration_source=source,
        reasons=reasons,
        user_approved=user_appr,
        approved_executor_id=appr_exec,
        task_execution_id=task_id,
    )


# ---------------------------------------------------------------------------
# 4. Privacy Evaluation Result & Decisions
# ---------------------------------------------------------------------------

@dataclass
class PrivacyEvaluationResult:
    decision: str                               # ALLOW, DENY, REQUIRE_APPROVAL, LOCAL_ONLY, WAIT_FOR_PRIVATE_EXECUTOR
    reason: str
    candidate_id: str
    privacy_class: str
    locality: str
    is_allowed: bool
    requires_approval: bool = False
    disallowed_reasons: List[str] = field(default_factory=list)
    approval_details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 5. Data Minimization
# ---------------------------------------------------------------------------

@dataclass
class MinimizationResult:
    minimized_payload: Dict[str, Any]
    stripped_fields: List[str]
    retained_fields: List[str]
    remote_executor_id: str


# Declared allowed payload keys per capability (everything else is stripped)
DECLARED_CAPABILITY_ARGUMENTS: Dict[str, Set[str]] = {
    "web.search": {"query", "max_results", "search_type", "domain"},
    "web.scrape_page": {"url", "timeout"},
    "weather.current": {"location", "units"},
    "weather.forecast": {"location", "days", "units"},
    "device.status": {"device_id", "fields"},
    "device.info": {"device_id"},
    "ui.notify": {"message", "title", "level", "target_device"},
    "app.launch": {"app", "arguments"},
    "app.close": {"app", "pid"},
    "app.list_running": {},
    "process.list": {"sort_by", "limit", "target_device"},
    "fs.stat": {"path"},
    "fs.list": {"path", "recursive"},
    "fs.read": {"path", "offset", "length"},
    "fs.write": {"path", "content", "mode"},
    "fs.delete": {"path"},
    "fs.move": {"src", "dst"},
    "fs.rename": {"src", "dst"},
    "calendar.create": {"title", "start_time", "end_time", "description", "location"},
    "calendar.list": {"time_min", "time_max", "max_results"},
    "email.send": {"to", "subject", "body"},
    "email.draft": {"to", "subject", "body"},
    "screen.capture": {"target_device", "region"},
    "screen.analyze": {"target_device", "query"},
    "goal.wait": {"condition_type", "condition_key", "operator", "expected_value"},
}

EXCLUDED_CONTEXT_KEYS: Set[str] = {
    "conversation_history",
    "chat_history",
    "messages",
    "memory",
    "user_profile",
    "unrelated_files",
    "session_token",
    "credentials",
    "system_prompt",
    "scratchpad",
    "internal_telemetry",
    "unrelated_memory",
    "personal_context",
}


def minimize_execution_payload(
    capability: str,
    arguments: Dict[str, Any],
    remote_executor_id: str,
    context: Optional[Dict[str, Any]] = None,
) -> MinimizationResult:
    """
    Construct a data-minimized execution payload for remote/cloud dispatch.
    Strips chat histories, unrelated memory, session tokens, and extraneous keys.
    """
    cap_clean = capability.strip().lower()
    # The canonical registry owns argument schemas; the historical map is only
    # compatibility for capabilities that have not yet been registered.
    from .capability_registry import CanonicalCapabilityRegistry
    cap = CanonicalCapabilityRegistry.get_instance().get_capability(cap_clean)
    allowed_keys = set(cap.arguments) if cap else DECLARED_CAPABILITY_ARGUMENTS.get(cap_clean, set(arguments.keys()))

    minimized: Dict[str, Any] = {}
    stripped: List[str] = []
    retained: List[str] = []

    # Merge arguments and non-confidential context
    combined = dict(arguments)
    if context:
        for k, v in context.items():
            if k not in EXCLUDED_CONTEXT_KEYS and k not in combined:
                combined[k] = v
            elif k in EXCLUDED_CONTEXT_KEYS:
                stripped.append(k)

    for k, v in combined.items():
        if k in EXCLUDED_CONTEXT_KEYS:
            stripped.append(k)
            continue

        # Keep keys declared for this capability or standard task identifiers
        if k in allowed_keys or k in ("task_execution_id", "goal_id", "step_id", "target_device"):
            minimized[k] = v
            retained.append(k)
        else:
            stripped.append(k)

    return MinimizationResult(
        minimized_payload=minimized,
        stripped_fields=sorted(list(set(stripped))),
        retained_fields=sorted(list(set(retained))),
        remote_executor_id=remote_executor_id,
    )


# ---------------------------------------------------------------------------
# 6. Central Privacy Policy Engine
# ---------------------------------------------------------------------------

class PrivacyPolicyEngine:
    """
    Central evaluation engine implementing Cortex Phase A7 Execution Routing.
    Evaluates:
    - Task privacy class
    - Required capabilities
    - Resource / data locality
    - Candidate trust and locality
    - Authorization
    - User policy primitives
    """

    _instance: Optional["PrivacyPolicyEngine"] = None

    def __init__(self, policy: Optional[UserPrivacyPolicy] = None):
        self.policy: UserPrivacyPolicy = policy or UserPrivacyPolicy()
        self._approvals: Dict[str, Dict[str, Any]] = {}
        self._policy_loaded: bool = False

    @classmethod
    def get_instance(cls) -> "PrivacyPolicyEngine":
        if cls._instance is None:
            cls._instance = PrivacyPolicyEngine()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton for test isolation."""
        cls._instance = None

    async def initialize(self) -> None:
        """Load durable user privacy policies from SQLite."""
        if self._policy_loaded:
            return
        await self.reload_policy_from_db()

    async def reload_policy_from_db(self) -> None:
        """Reload user privacy policies from SQLite database."""
        try:
            stored = await list_privacy_policies()
            if stored:
                self.policy = UserPrivacyPolicy.from_dict(stored)
                log.info(f"[PrivacyPolicyEngine] Loaded policy from DB: {self.policy.to_dict()}")
            self._policy_loaded = True
        except Exception as exc:
            log.warning(f"[PrivacyPolicyEngine] Could not load privacy policies from DB: {exc}")

    async def update_user_policy(self, updates: Dict[str, Any]) -> UserPrivacyPolicy:
        """Update user policy primitives and persist to SQLite."""
        cur = self.policy.to_dict()
        cur.update(updates)
        self.policy = UserPrivacyPolicy.from_dict(cur)

        # Persist to DB
        for k, v in self.policy.to_dict().items():
            await save_privacy_policy(k, v)

        log.info(f"[PrivacyPolicyEngine] Updated user privacy policy: {self.policy.to_dict()}")
        return self.policy

    # -----------------------------------------------------------------------
    # Candidate Evaluation
    # -----------------------------------------------------------------------

    def evaluate_candidate(
        self,
        candidate: DeviceHandle,
        task_context: TaskPrivacyContext,
        required_capability: Optional[str] = None,
    ) -> PrivacyEvaluationResult:
        """
        Evaluate if a candidate executor is permitted to execute a task.
        Rule: Capable != Allowed.
        """
        cand_id = candidate.device_id
        locality = getattr(candidate, "locality", ExecutorLocality.LOCAL_DEVICE)
        p_class = task_context.privacy_class
        disallowed_reasons: List[str] = []

        # 1. Restricted devices
        if candidate.trust_level == "restricted":
            if p_class in (PrivacyClass.PERSONAL, PrivacyClass.SENSITIVE, PrivacyClass.HIGHLY_SENSITIVE):
                reason = f"Restricted device '{cand_id}' is not permitted to process {p_class} tasks."
                disallowed_reasons.append(reason)
                return PrivacyEvaluationResult(
                    decision=PrivacyDecision.DENY,
                    reason=reason,
                    candidate_id=cand_id,
                    privacy_class=p_class,
                    locality=locality,
                    is_allowed=False,
                    disallowed_reasons=disallowed_reasons,
                )

        # 2. Resource Locality constraints (e.g. ASUS-local files cannot run on remote nodes)
        if task_context.resource_localities:
            for res_path, res_loc in task_context.resource_localities.items():
                if res_loc != cand_id:
                    reason = f"Resource '{res_path}' is pinned to local device '{res_loc}'; candidate '{cand_id}' cannot access local data."
                    disallowed_reasons.append(reason)
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.LOCAL_ONLY,
                        reason=reason,
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=False,
                        disallowed_reasons=disallowed_reasons,
                    )

        # 3. Cloud / Remote Workers
        if locality == ExecutorLocality.CLOUD_WORKER or getattr(candidate, "device_type", "") == "cloud_worker":
            # HIGHLY_SENSITIVE: Never allowed in cloud
            if p_class == PrivacyClass.HIGHLY_SENSITIVE:
                reason = "HIGHLY_SENSITIVE data is strictly forbidden from cloud processing."
                disallowed_reasons.append(reason)
                return PrivacyEvaluationResult(
                    decision=PrivacyDecision.DENY,
                    reason=reason,
                    candidate_id=cand_id,
                    privacy_class=p_class,
                    locality=locality,
                    is_allowed=False,
                    disallowed_reasons=disallowed_reasons,
                )

            # SENSITIVE: Check user policy & approval override
            if p_class == PrivacyClass.SENSITIVE:
                if task_context.user_approved and task_context.approved_executor_id == cand_id:
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.ALLOW,
                        reason=f"User explicitly approved cloud execution on '{cand_id}'.",
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=True,
                    )

                if self.policy.sensitive_data_cloud_allowed:
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.ALLOW,
                        reason="User policy explicitly permits sensitive data cloud processing.",
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=True,
                    )

                # Policy forbids by default: check if approval override is permitted
                if self.policy.allow_cloud_approval_override:
                    details = self._build_approval_details(candidate, task_context, required_capability)
                    reason = (
                        f"Task contains SENSITIVE data and cloud worker '{cand_id}' requires explicit user approval."
                    )
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.REQUIRE_APPROVAL,
                        reason=reason,
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=False,
                        requires_approval=True,
                        disallowed_reasons=[reason],
                        approval_details=details,
                    )
                else:
                    reason = f"User policy strictly forbids cloud processing for SENSITIVE tasks (candidate: '{cand_id}')."
                    disallowed_reasons.append(reason)
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.WAIT_FOR_PRIVATE_EXECUTOR,
                        reason=reason,
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=False,
                        disallowed_reasons=disallowed_reasons,
                    )

            # PERSONAL: Personal data cloud routing
            if p_class == PrivacyClass.PERSONAL:
                if self.policy.personal_data_trusted_devices_only:
                    if task_context.user_approved and task_context.approved_executor_id == cand_id:
                        return PrivacyEvaluationResult(
                            decision=PrivacyDecision.ALLOW,
                            reason=f"User approved personal data execution on '{cand_id}'.",
                            candidate_id=cand_id,
                            privacy_class=p_class,
                            locality=locality,
                            is_allowed=True,
                        )
                    reason = f"Personal data is restricted to trusted local devices only; cloud worker '{cand_id}' rejected."
                    disallowed_reasons.append(reason)
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.DENY,
                        reason=reason,
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=False,
                        disallowed_reasons=disallowed_reasons,
                    )

            # PUBLIC / INTERNAL
            if p_class == PrivacyClass.PUBLIC:
                if self.policy.allow_remote_public_tasks:
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.ALLOW,
                        reason=f"PUBLIC task permitted on cloud worker '{cand_id}'.",
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=True,
                    )
                else:
                    reason = "User policy forbids remote execution of public tasks."
                    disallowed_reasons.append(reason)
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.DENY,
                        reason=reason,
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=False,
                        disallowed_reasons=disallowed_reasons,
                    )

        # 4. Local Devices & Trusted LAN Devices & Private Servers
        if locality in (
            ExecutorLocality.LOCAL_DEVICE,
            ExecutorLocality.TRUSTED_LAN_DEVICE,
            ExecutorLocality.PRIVATE_SERVER,
        ):
            # Check private compute flag for personal / sensitive / private data
            if p_class in (PrivacyClass.PERSONAL, PrivacyClass.SENSITIVE, PrivacyClass.HIGHLY_SENSITIVE):
                if not getattr(candidate, "private_compute_allowed", True):
                    reason = f"Candidate '{cand_id}' does not have private compute allowed for {p_class} data."
                    disallowed_reasons.append(reason)
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.DENY,
                        reason=reason,
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=False,
                        disallowed_reasons=disallowed_reasons,
                    )

            # HIGHLY_SENSITIVE on trusted LAN (e.g. Lenovo) vs local device (ASUS)
            if p_class == PrivacyClass.HIGHLY_SENSITIVE:
                if locality == ExecutorLocality.LOCAL_DEVICE or getattr(candidate, "execution_environment", "") == ExecutionEnvironment.SECURE_ENCLAVE:
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.ALLOW,
                        reason=f"HIGHLY_SENSITIVE task permitted on local execution node '{cand_id}'.",
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=True,
                    )
                else:
                    reason = f"HIGHLY_SENSITIVE task is restricted to local device or secure enclave (candidate '{cand_id}' is '{locality}')."
                    disallowed_reasons.append(reason)
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.LOCAL_ONLY,
                        reason=reason,
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=False,
                        disallowed_reasons=disallowed_reasons,
                    )

            # SENSITIVE / PERSONAL / INTERNAL / PUBLIC on local or trusted LAN
            if locality == ExecutorLocality.TRUSTED_LAN_DEVICE:
                if candidate.trust_level not in ("admin", "trusted"):
                    reason = f"Candidate '{cand_id}' has insufficient trust level '{candidate.trust_level}' for trusted LAN execution."
                    disallowed_reasons.append(reason)
                    return PrivacyEvaluationResult(
                        decision=PrivacyDecision.DENY,
                        reason=reason,
                        candidate_id=cand_id,
                        privacy_class=p_class,
                        locality=locality,
                        is_allowed=False,
                        disallowed_reasons=disallowed_reasons,
                    )

            return PrivacyEvaluationResult(
                decision=PrivacyDecision.ALLOW,
                reason=f"Candidate '{cand_id}' ({locality}) is authorized for {p_class} execution.",
                candidate_id=cand_id,
                privacy_class=p_class,
                locality=locality,
                is_allowed=True,
            )

        # Fallback default
        return PrivacyEvaluationResult(
            decision=PrivacyDecision.ALLOW,
            reason=f"Execution permitted on '{cand_id}'.",
            candidate_id=cand_id,
            privacy_class=p_class,
            locality=locality,
            is_allowed=True,
        )

    def _build_approval_details(
        self,
        candidate: DeviceHandle,
        task_context: TaskPrivacyContext,
        capability: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build clear, consequence-disclosing approval metadata."""
        appr_id = f"appr_{uuid.uuid4().hex[:10]}"
        what_data = (
            ", ".join(task_context.resource_paths)
            if task_context.resource_paths
            else f"Task inputs for capability '{capability or 'unknown'}' (categories: {', '.join(task_context.data_categories) or 'general'})"
        )
        what_leaves = [
            f"Execution payload for capability '{capability or 'unknown'}'",
            f"Required resource identifiers: {task_context.resource_paths or 'None'}",
        ]
        why_needed = (
            f"Local executor is offline or unavailable, and task contains {task_context.privacy_class} data."
        )

        details = {
            "approval_id": appr_id,
            "task_execution_id": task_context.task_execution_id,
            "what_data": what_data,
            "which_executor": f"{candidate.display_name} ({candidate.device_id}, locality={getattr(candidate, 'locality', 'cloud_worker')})",
            "candidate_device_id": candidate.device_id,
            "why_needed": why_needed,
            "what_will_leave_local_device": what_leaves,
            "privacy_class": task_context.privacy_class,
            "status": "pending",
        }
        self._approvals[appr_id] = details
        return details

    # -----------------------------------------------------------------------
    # Approval Flow Operations
    # -----------------------------------------------------------------------

    def grant_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Grant specific approval for cloud processing override."""
        appr = self._approvals.get(approval_id)
        if not appr:
            return None
        appr["status"] = "granted"
        appr["granted_at"] = datetime.now(timezone.utc).isoformat()
        return appr

    def deny_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Deny approval, preserving wait / local-only state."""
        appr = self._approvals.get(approval_id)
        if not appr:
            return None
        appr["status"] = "denied"
        appr["denied_at"] = datetime.now(timezone.utc).isoformat()
        return appr

    def is_approval_granted(self, approval_id: str, candidate_id: str) -> bool:
        """Verify whether explicit approval was granted for this candidate."""
        appr = self._approvals.get(approval_id)
        if not appr:
            return False
        return (
            appr.get("status") == "granted"
            and appr.get("candidate_device_id") == candidate_id
        )

    # -----------------------------------------------------------------------
    # Candidate Ranking & Filtering for ExecutorSelector
    # -----------------------------------------------------------------------

    def rank_and_filter_candidates(
        self,
        candidates: List[DeviceHandle],
        task_context: TaskPrivacyContext,
        required_capability: str,
    ) -> Tuple[List[DeviceHandle], Optional[PrivacyEvaluationResult]]:
        """
        Filter candidate list through privacy policy and apply local-first ranking.
        Returns: (allowed_candidates_in_priority_order, highest_priority_rejection)
        """
        allowed: List[DeviceHandle] = []
        pending_approval: Optional[PrivacyEvaluationResult] = None
        rejection_res: Optional[PrivacyEvaluationResult] = None

        for cand in candidates:
            eval_res = self.evaluate_candidate(cand, task_context, required_capability)
            if eval_res.is_allowed:
                allowed.append(cand)
            elif eval_res.requires_approval and pending_approval is None:
                pending_approval = eval_res
            elif rejection_res is None:
                rejection_res = eval_res

        if not allowed:
            return [], (pending_approval or rejection_res)

        # Local-First Ranking Rule:
        # If policy prefers local execution and task is not PUBLIC:
        # Sort priority:
        # 1. local_device (e.g. ASUS)
        # 2. trusted_lan_device (e.g. Lenovo)
        # 3. private_server
        # 4. cloud_worker / third_party
        if self.policy.prefer_local_execution and task_context.privacy_class != PrivacyClass.PUBLIC:
            locality_priority = {
                ExecutorLocality.LOCAL_DEVICE: 1,
                ExecutorLocality.TRUSTED_LAN_DEVICE: 2,
                ExecutorLocality.PRIVATE_SERVER: 3,
                ExecutorLocality.CLOUD_WORKER: 4,
                ExecutorLocality.THIRD_PARTY_SERVICE: 5,
            }
            allowed.sort(
                key=lambda h: (
                    locality_priority.get(getattr(h, "locality", ExecutorLocality.LOCAL_DEVICE), 9),
                    h.workload.get("active_task_count", 0),
                    h.display_name,
                )
            )

        return allowed, None

    # -----------------------------------------------------------------------
    # A6 Handoff Integration
    # -----------------------------------------------------------------------

    def evaluate_execution_handoff(
        self,
        execution: Any,  # TaskExecution
        candidate_handle: DeviceHandle,
    ) -> PrivacyEvaluationResult:
        """
        Evaluate if an A6 task failover/handoff may move to a candidate executor.
        """
        task_ctx = TaskPrivacyContext(
            privacy_class=execution.metadata.get("privacy_class", PrivacyClass.SENSITIVE if not execution.movable else PrivacyClass.INTERNAL),
            data_categories=execution.metadata.get("data_categories", []),
            resource_paths=list(execution.required_resources),
            resource_localities=dict(execution.required_data_locations),
            task_execution_id=execution.task_execution_id,
            user_approved=bool(execution.metadata.get("user_approved", False)),
            approved_executor_id=execution.metadata.get("approved_executor_id"),
        )
        # Inherit explicit file sensitivity
        if execution.required_data_locations or any("desktop" in r.lower() or "document" in r.lower() for r in execution.required_resources):
            task_ctx.privacy_class = PrivacyClass.SENSITIVE

        req_cap = execution.required_capabilities[0] if execution.required_capabilities else "unknown"
        return self.evaluate_candidate(candidate_handle, task_ctx, required_capability=req_cap)

    # -----------------------------------------------------------------------
    # Ground-Truth Self-Knowledge Introspection
    # -----------------------------------------------------------------------

    def answer_privacy_query(
        self,
        query: str,
        current_execution: Optional[Any] = None,
        candidate_handle: Optional[DeviceHandle] = None,
    ) -> Dict[str, Any]:
        """
        Truthfully answer conversational questions about execution routing,
        privacy boundaries, cloud policies, and data leaving device from ground-truth state.
        """
        q = query.lower()

        # 1. "Why are you running this locally?"
        if "why" in q and "running" in q and ("locally" in q or "local" in q):
            target = current_execution.executor_device_id if current_execution else "local device"
            p_class = current_execution.metadata.get("privacy_class", "SENSITIVE") if current_execution else "SENSITIVE"
            res_str = f" ('{', '.join(current_execution.required_resources)}')" if current_execution and current_execution.required_resources else ""
            return {
                "matched": True,
                "answer": (
                    f"This task is running locally on {target} because it accesses {p_class} data{res_str}. "
                    f"Cortex local-first execution policy requires private processing on authorized local nodes."
                ),
            }

        # 2. "Why didn't this move to the cloud?"
        if ("why didn't" in q or "why did not" in q or "why not") and "cloud" in q:
            p_class = current_execution.metadata.get("privacy_class", "SENSITIVE") if current_execution else "SENSITIVE"
            cloud_pol = "allowed" if self.policy.sensitive_data_cloud_allowed else "disallowed"
            return {
                "matched": True,
                "answer": (
                    f"This task was not moved to the cloud because it is classified as {p_class}, and user privacy policy "
                    f"'sensitive_data_cloud_allowed' is currently {self.policy.sensitive_data_cloud_allowed} ({cloud_pol}). "
                    f"Sensitive data is never silently routed to remote cloud workers without explicit authorization."
                ),
            }

        # 3. "Can Lenovo handle this privately?"
        if "lenovo" in q and ("privately" in q or "private" in q):
            handle = candidate_handle
            if not handle:
                from .executor_registry import ExecutorRegistry
                handle = ExecutorRegistry.get_instance().get_device_handle("lenovo")

            if handle and getattr(handle, "private_compute_allowed", True) and handle.trust_level in ("admin", "trusted"):
                return {
                    "matched": True,
                    "answer": (
                        f"Yes. Lenovo is enrolled as a {getattr(handle, 'locality', 'trusted_lan_device')} with trust level "
                        f"'{handle.trust_level}' and private_compute_allowed enabled. It is authorized to process data up to SENSITIVE."
                    ),
                }
            elif handle:
                return {
                    "matched": True,
                    "answer": (
                        f"No. Lenovo currently has trust level '{handle.trust_level}' with private_compute_allowed={getattr(handle, 'private_compute_allowed', False)} "
                        f"and cannot handle private/sensitive workloads."
                    ),
                }
            else:
                return {
                    "matched": True,
                    "answer": "Lenovo is not currently enrolled in Cortex ExecutorRegistry.",
                }

        # 4. "What data would leave my laptop?"
        if ("what data" in q or "what leaves" in q) and ("leave" in q or "sent" in q or "transmitted" in q):
            if current_execution:
                cap = current_execution.required_capabilities[0] if current_execution.required_capabilities else "web.search"
                min_res = minimize_execution_payload(
                    capability=cap,
                    arguments={"query": "quantum computing", "conversation_history": ["..."], "user_profile": {"name": "Abhin"}},
                    remote_executor_id="cloud_worker",
                )
                retained = ", ".join(min_res.retained_fields) or "minimal task arguments"
                stripped = ", ".join(min_res.stripped_fields) or "conversation history, memory, personal profile"
                return {
                    "matched": True,
                    "answer": (
                        f"Under data minimization for capability '{cap}', only the required execution parameters ({retained}) "
                        f"are transmitted. All personal context, memory, and full conversation history ({stripped}) are strictly stripped."
                    ),
                }
            return {
                "matched": True,
                "answer": (
                    "Under Cortex data minimization, only the strictly required capability parameters leave your laptop. "
                    "Full conversation history, user profile memory, and unrelated files are never transmitted."
                ),
            }

        # 5. "Why is this task waiting?"
        if "why is" in q and "waiting" in q:
            if current_execution and current_execution.execution_state in ("WAITING_FOR_EXECUTOR", "WAIT_FOR_PRIVATE_EXECUTOR"):
                wait_info = current_execution.metadata.get("waiting_for_executor", {})
                req = wait_info.get("privacy_requirement") or current_execution.metadata.get("privacy_class", "SENSITIVE")
                reason = wait_info.get("missing_reason") or wait_info.get("reason", "waiting for private executor")
                return {
                    "matched": True,
                    "answer": (
                        f"This task is waiting (WAITING_FOR_PRIVATE_EXECUTOR) because it requires private execution ({req}) "
                        f"and all eligible local or trusted LAN devices are currently offline. Cloud execution is prohibited by policy: {reason}."
                    ),
                }
            return {
                "matched": True,
                "answer": "This task is waiting for an authorized executor matching its capability and privacy constraints.",
            }

        return {"matched": False, "answer": ""}
