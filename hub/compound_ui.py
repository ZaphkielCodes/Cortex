"""Durable, evidence-gated mechanics for model-driven compound tasks.

No request parsing, application recipes, hidden reasoning, or executor authority.
Policy, leases and native freshness fencing remain on the execution path.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import time
import weakref
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: Literal["ui", "capability"] = "ui"
    capability: str | None = None
    selector: dict[str, str] = Field(default_factory=dict)
    path: list[str] = Field(default_factory=list, max_length=4)
    operator: Literal["equals", "contains", "exists", "absent"] = "equals"
    value: str | int | float | bool | None = None

    @model_validator(mode="after")
    def bounded_claim(self):
        if set(self.selector) - {"element_id", "role", "label", "automation_id"}:
            raise ValueError("Unsupported evidence selector")
        if self.source == "ui" and not self.selector:
            raise ValueError("UI completion needs an observed control selector")
        if self.source == "capability" and self.operator == "absent":
            raise ValueError("Absence requires a complete UI observation")
        if self.source == "capability" and (not self.capability or not self.path or self.path[0] != "data"):
            raise ValueError("Capability completion needs structured data, not dispatch status or prose")
        if self.operator in {"equals", "contains"} and (not self.path or self.value is None):
            raise ValueError("Comparison needs a field and expected value")
        if self.operator == "contains" and (not isinstance(self.value, str) or not self.value):
            raise ValueError("Contains needs nonempty text")
        return self


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def evaluate(conditions, observation, evidence, device):
    """Evaluate literal predicates locally. Model prose is never a verifier."""
    outcomes = []
    for raw in conditions:
        condition = EvidenceCondition.model_validate(raw) if isinstance(raw, dict) else raw
        value, found, ambiguous = None, False, False
        if condition.source == "ui":
            candidates = (observation or {}).get("elements", []) if (observation or {}).get("device_id") == device else []
            for key, expected in condition.selector.items():
                candidates = [e for e in candidates if (e.get("metadata", {}).get("automation_id")
                    if key == "automation_id" else e.get(key)) == expected]
            found = bool(candidates)
            ambiguous = len(candidates) != 1
            value = candidates[0] if len(candidates) == 1 else None
        else:
            matches = [step for step in evidence if step.get("capability") == condition.capability
                and step.get("executor_device") == device and step.get("result", {}).get("status") == "success"]
            found = bool(matches)
            value = matches[-1]["result"] if matches else None
        for key in condition.path:
            if not isinstance(value, dict) or key not in value:
                found, value = False, None
                break
            value = value[key]
        if condition.operator == "exists":
            passed = found and not ambiguous
        elif condition.operator == "absent":
            # Absence requires a genuine, nonempty observation, never a lost window.
            passed = bool(observation and observation.get("device_id") == device and observation.get("window_id") and
                          observation.get("provenance", {}).get("elements_complete")) and not found
        elif ambiguous:
            passed = False
        elif condition.operator == "equals":
            passed = found and type(value) is type(condition.value) and value == condition.value
        else:
            passed = found and isinstance(value, str) and condition.value in value
        outcomes.append({"verified": bool(passed), "source": condition.source, "ambiguous": ambiguous,
                         "criterion": len(outcomes)})
    return {"verified": bool(outcomes) and all(row["verified"] for row in outcomes), "criteria": outcomes}


STAGES = {"OBSERVING", "REASONING", "GROUNDING", "WAITING_FOR_APPROVAL", "EXECUTING", "VERIFYING",
          "COMPLETED", "FAILED", "CANCELLED", "WAITING_FOR_EXECUTOR", "WAITING_FOR_PROVIDER"}
ALLOWED = {
    "OBSERVING": {"OBSERVING", "REASONING", "VERIFYING", "WAITING_FOR_APPROVAL"},
    "REASONING": {"REASONING", "OBSERVING", "GROUNDING", "VERIFYING", "WAITING_FOR_APPROVAL", "COMPLETED"},
    "GROUNDING": {"OBSERVING", "REASONING", "WAITING_FOR_APPROVAL", "EXECUTING", "COMPLETED"},
    "WAITING_FOR_APPROVAL": {"GROUNDING", "EXECUTING", "REASONING", "VERIFYING", "OBSERVING"},
    "EXECUTING": {"VERIFYING", "OBSERVING", "REASONING"},
    "VERIFYING": {"OBSERVING", "REASONING", "GROUNDING", "COMPLETED", "WAITING_FOR_APPROVAL"},
    "WAITING_FOR_EXECUTOR": {"OBSERVING", "REASONING", "GROUNDING"},
    "WAITING_FOR_PROVIDER": {"OBSERVING", "REASONING", "VERIFYING", "GROUNDING"},
    "FAILED": {"OBSERVING"}, "COMPLETED": set(), "CANCELLED": set(),
}


@dataclass
class CompoundState:
    task_id: str
    goal: str
    source_device: str
    parent_task_id: str | None = None
    success_condition: list = field(default_factory=list)
    current_stage: str = "REASONING"
    current_observation_id: str | None = None
    previous_observation_id: str | None = None
    chosen_action: str | None = None
    target_selector: dict = field(default_factory=dict)
    expected_effect: list = field(default_factory=list)
    execution_receipt: dict = field(default_factory=dict)
    execution_receipts: list = field(default_factory=list)
    verification_result: dict = field(default_factory=dict)
    retry_count: int = 0
    blocked_reason: str | None = None
    task_status: str = "RUNNING"
    active: bool = False
    ui_actions: int = 0
    action_history: list = field(default_factory=list)
    operational_trace: list = field(default_factory=list)
    trace_sequence: int = 0
    turn: int = 0
    fresh_required: bool = False
    # Private local checkpoint; excluded from operational trace and model_state.
    pending_text_verification: dict = field(default_factory=dict)
    recovery_counts: dict = field(default_factory=dict)
    recovery_evidence: list = field(default_factory=list)
    expected_targets: dict = field(default_factory=dict)
    semantic_observations: dict = field(default_factory=dict)
    relaunch_permits: dict = field(default_factory=dict)


class CompoundRuntime:
    MAX_REPLANS = 3
    active = weakref.WeakValueDictionary()

    def __init__(self, state):
        self.state = state
        self._stage_started = time.monotonic()
        self.active[state.task_id] = self

    async def save(self):
        from .database import get_task, update_task
        stored = await get_task(self.state.task_id)
        if not stored:
            return
        saved = stored.get("context") or {}
        saved = json.loads(saved) if isinstance(saved, str) else dict(saved)
        await update_task(self.state.task_id, {"context": dict(saved, compound_ui=asdict(self.state))})

    async def transition(self, stage, *, decision=None, result=None, reason=None, details=None):
        if stage not in STAGES:
            raise ValueError("Unknown compound stage")
        old = self.state.current_stage
        if stage == "COMPLETED" and self.state.active and not self.state.verification_result.get("verified"):
            raise ValueError("Compound completion requires verified semantic evidence")
        if stage not in {"FAILED", "CANCELLED", "WAITING_FOR_PROVIDER", "WAITING_FOR_EXECUTOR"} and stage != old and stage not in ALLOWED[old]:
            raise ValueError(f"Invalid compound transition {old} to {stage}")
        now = time.monotonic()
        self.state.trace_sequence += 1
        event = {"sequence": self.state.trace_sequence, "turn": self.state.turn,
            "stage": old, "next_state": stage, "observation_id": self.state.current_observation_id,
            "decision": decision, "capability": self.state.chosen_action,
            "target": dict(self.state.target_selector), "result": result,
            "verification": self.state.verification_result or None,
            "latency_ms": round((now-self._stage_started)*1000, 3),
            "task_id": self.state.task_id, "parent_task_id": self.state.parent_task_id,
            "at": datetime.now(timezone.utc).isoformat(),"reason":reason,"details":details}
        self.state.operational_trace = (self.state.operational_trace+[event])[-128:]
        self.state.current_stage = stage
        self.state.blocked_reason = reason
        self.state.task_status = stage if stage in {"FAILED", "CANCELLED", "COMPLETED", "WAITING_FOR_EXECUTOR", "WAITING_FOR_PROVIDER"} else "RUNNING"
        self._stage_started = now
        await self.save()
        if stage != old:
            from .api_control import emit_activity
            emit_activity("Computer task: " + stage.lower().replace("_", " "), category="computer_use",
                device_id=self.state.source_device, task_id=self.state.parent_task_id or self.state.task_id,
                status="completed" if stage == "COMPLETED" else "error" if stage == "FAILED" else "active")

    def conditions(self, claims):
        return [claim.model_dump() if isinstance(claim, EvidenceCondition) else EvidenceCondition.model_validate(claim).model_dump() for claim in claims]

    def declare(self, claims):
        normalized = self.conditions(claims)
        if self.state.success_condition and normalized and normalized != self.state.success_condition:
            raise ValueError("Success condition cannot change after declaration")
        if normalized:
            self.state.success_condition = normalized
        if not self.state.success_condition:
            raise ValueError("Declare structured success_condition before compound side effects")
        self.state.active = True

    async def reject(self, reason):
        self.state.retry_count += 1
        await self.transition("OBSERVING", result="replan", reason=reason)
        return self.state.retry_count <= self.MAX_REPLANS

    async def recover(self, category, *, capability=None, details=None):
        from .compound_recovery import RECOVERY_LIMITS, RecoveryExhausted
        count = self.state.recovery_counts.get(category, 0) + 1
        self.state.recovery_counts[category] = count
        evidence = {"category": category, "attempt": count, "capability": capability,
            "execution_receipt": dict(self.state.execution_receipt), "expected_effect": self.state.expected_effect,
            "observed_effect": details or {}, "input_replayed": False}
        self.state.recovery_evidence = (self.state.recovery_evidence + [evidence])[-12:]
        await self.transition("OBSERVING", result="recoverable_failure", reason=category)
        if count > RECOVERY_LIMITS[category]:
            raise RecoveryExhausted(category)
        return evidence

    def action_identity(self, capability, arguments, observation, device):
        def stable_target(selector):
            element = next((e for e in (observation or {}).get("elements", []) if e.get("element_id") == selector), {})
            identity = element.get("metadata", {}).get("automation_id")
            return (identity or [element.get("role"), element.get("label")]) if element else selector
        selector = arguments.get("element_id") or arguments.get("target") or arguments.get("selector")
        target = stable_target(selector)
        excluded = {"observation_id", "element_id", "target", "selector", "target_device", "is_credential"}
        if capability == "ui.drag":
            target = [stable_target(arguments.get("from_target")), stable_target(arguments.get("to_target"))]
            excluded.update({"from_target", "to_target"})
        args = {k:v for k,v in arguments.items() if k not in excluded}
        if capability in {"ui.click","ui.right_click"}:
            args["button"] = "right" if capability == "ui.right_click" else str(args.get("button","left")).casefold()
            capability = "ui.click"
        if capability == "ui.type":
            args.setdefault("clear_existing",False)
        from .compound_recovery import observation_identity, process_key
        identity = observation_identity(observation or {})
        window = [identity.get("pid"), process_key(identity.get("process_name"))] if identity.get("pid") else identity.get("window_id")
        return digest([capability, device, window if capability.startswith("ui.") else None, target, args])

    def duplicate(self, capability, arguments, observation, device):
        from .capability_registry import CanonicalCapabilityRegistry
        cap = CanonicalCapabilityRegistry.get_instance().get_capability(capability)
        if not cap or cap.side_effect_class in {None, "none"} or capability == "window.focus" and getattr(self,"_reacquiring",False):
            return False
        # Older checkpoints have only opaque fingerprints. A fresh observation
        # cannot prove that replaying their last input is safe under a new identity.
        legacy = any(row.get("recovered") and not row.get("capability") for row in self.state.action_history)
        receipt = self.state.execution_receipt
        if legacy and receipt.get("capability") == capability and receipt.get("side_effects_possible") and not receipt.get("retry_safe"):
            return True
        if capability == "app.launch" and self.state.relaunch_permits.get(device) == arguments.get("app"):
            return False
        fingerprint = self.action_identity(capability, arguments, observation, device)
        return any(row["fingerprint"] == fingerprint and not row.get("retry_safe") for row in self.state.action_history)

    async def before_dispatch(self, capability, arguments, observation, device, ui_action):
        if capability == "ui.observe":
            # A readback must not erase the pending input receipt it verifies.
            await self.transition("OBSERVING", decision="CALL_CAPABILITY")
            return
        fingerprint = self.action_identity(capability, arguments, observation, device)
        from .capability_registry import CanonicalCapabilityRegistry
        cap = CanonicalCapabilityRegistry.get_instance().get_capability(capability)
        mutating = bool(ui_action or (cap and cap.side_effect_class not in {None, "none"}))
        if mutating and self.duplicate(capability, arguments, observation, device):
            raise ValueError("Equivalent unverified action cannot be repeated")
        if capability == "app.launch":
            self.state.relaunch_permits.pop(device, None)
        self.state.chosen_action = capability
        self.state.target_selector = {"fingerprint": fingerprint, "device_id": device,
            "window_id": (observation or {}).get("window_id"),
            "element_id": (arguments.get("element_id") or arguments.get("target"))
                if str(arguments.get("element_id") or arguments.get("target", "")).startswith("el_") else None}
        self.state.execution_receipt = {"attempt_id": digest([self.state.task_id, self.state.turn, fingerprint]),
            "task_id": self.state.task_id, "parent_task_id": self.state.parent_task_id,
            "device_id": device, "capability": capability, "ran": "unverified",
            "side_effects_possible": mutating, "fingerprint": fingerprint,
            "retry_safe": False, "verification_required": ui_action, "status": "in_flight"}
        self.state.execution_receipts = (self.state.execution_receipts+[dict(self.state.execution_receipt)])[-40:]
        if ui_action:
            self.state.ui_actions += 1
        if mutating:
            self.state.action_history.append({"fingerprint": fingerprint, "capability": capability, "verified": False, "retry_safe": False})
        await self.transition("EXECUTING")

    async def receipt(self, raw):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        attempted = data.get("input_attempted", raw.get("input_attempted"))
        attempted = True if data.get("input_attempted") is True or raw.get("input_attempted") is True else attempted
        preinput = raw.get("error") in {"stale_observation", "target_not_found", "ambiguous_target", "target_not_editable",
            "focus_unverified", "observation_required", "window_not_found"}
        no_input = attempted is not True and (attempted is False or preinput or raw.get("status") in {
            "denied", "needs_approval", "unsupported", "clarification_needed"})
        self.state.execution_receipt.update(command_id=raw.get("command_id"), status=raw.get("status"),
            ran=True if raw.get("status") == "success" else False if no_input else "unverified",
            side_effects_possible=not no_input and self.state.execution_receipt.get("side_effects_possible", False),
            retry_safe=no_input, verification_required=not no_input and self.state.execution_receipt.get("verification_required", False))
        if no_input and self.state.action_history and self.state.action_history[-1]["fingerprint"] == self.state.execution_receipt.get("fingerprint"):
            # Re-grounding is allowed when the executor proves no input occurred.
            self.state.action_history[-1]["verified"] = True
            self.state.action_history[-1]["retry_safe"] = True
        elif raw.get("status") == "success" and not self.state.execution_receipt.get("verification_required"):
            if self.state.action_history and self.state.action_history[-1]["fingerprint"] == self.state.execution_receipt.get("fingerprint"):
                self.state.action_history[-1]["verified"] = True
        if self.state.execution_receipts:
            self.state.execution_receipts[-1] = dict(self.state.execution_receipt)
        await self.transition("VERIFYING", result=raw.get("status"))

    async def observed(self, observation, raw=None):
        if (self.state.execution_receipt.get("verification_required") and
                observation.get("observation_id") == self.state.current_observation_id):
            self.state.fresh_required = True
            await self.recover("VERIFICATION_FAILED",capability="ui.observe",details={"post_action_observation_not_fresh":True})
            return False
        self.state.previous_observation_id = self.state.current_observation_id
        self.state.current_observation_id = observation.get("observation_id")
        from .compound_recovery import semantic_ui_hash
        device = observation.get("device_id")
        prior = self.state.semantic_observations.get(device)
        current = semantic_ui_hash(observation)
        self.state.semantic_observations[device] = current
        self._ui_unchanged = prior is not None and prior == current
        self.state.fresh_required = False
        self.state.execution_receipts = (self.state.execution_receipts + [{
            "task_id":self.state.task_id,"parent_task_id":self.state.parent_task_id,
            "device_id":observation.get("device_id"),"capability":"ui.observe","ran":True,
            "status":"success","side_effects_possible":False,"retry_safe":True,
            "verification_required":False,"command_id":(raw or {}).get("command_id")}])[-40:]
        await self.transition("VERIFYING", result="observation_received")
        return True

    async def verified(self, outcome):
        self.state.verification_result = outcome
        if outcome.get("verified"):
            for action in self.state.action_history:
                if action["fingerprint"] == self.state.execution_receipt.get("fingerprint"):
                    action["verified"] = True
            self.state.execution_receipt["verification_required"] = False
            for receipt in self.state.execution_receipts:
                if receipt.get("attempt_id") == self.state.execution_receipt.get("attempt_id"):
                    receipt.update(self.state.execution_receipt)
        else:
            category = "NO_PROGRESS" if getattr(self,"_ui_unchanged",False) else "VERIFICATION_FAILED"
            await self.recover(category, capability=self.state.chosen_action,
                details={"expected_effect_verified":False,"meaningful_ui_change":not getattr(self,"_ui_unchanged",False)})
        await self.transition("REASONING", result="verified" if outcome.get("verified") else "replan")

    def completion(self, observation, evidence, device):
        if not self.state.active:
            return {"verified": True, "criteria": []}
        if self.state.fresh_required or self.state.execution_receipt.get("verification_required"):
            return {"verified": False, "reason": "fresh_verification_required"}
        if self.state.ui_actions and not any(c.get("source") == "ui" for c in self.state.success_condition):
            return {"verified": False, "reason": "ui_goal_needs_ui_evidence"}
        return evaluate(self.state.success_condition, observation, evidence, device)

    def model_state(self):
        return {"current_stage": self.state.current_stage, "success_condition": self.state.success_condition,
                "current_observation_id": self.state.current_observation_id,
                "verification_result": self.state.verification_result, "retry_count": self.state.retry_count,
                "blocked_reason": self.state.blocked_reason, "fresh_required": self.state.fresh_required,
                "recovery_counts":self.state.recovery_counts,"recovery_evidence":self.state.recovery_evidence[-3:],
                "expected_targets":self.state.expected_targets,
                "executed_actions":self.state.action_history[-20:]}


async def provider_wait(task_id, waiting):
    """Local operational hook; no callbacks or private state go into provider payloads."""
    runtime = CompoundRuntime.active.get(task_id)
    if not runtime:
        return
    if waiting:
        runtime._provider_return_stage = runtime.state.current_stage
        await runtime.transition("WAITING_FOR_PROVIDER", reason="provider_cooldown")
    else:
        await runtime.transition(getattr(runtime, "_provider_return_stage", "REASONING"), result="provider_retry_ready")
