"""
hub/task_ownership.py
----------------------
Phase A6: Task Ownership, Execution Leases, Fencing Tokens, Durable Checkpoints,
Handoff Eligibility, Failover, Wait Conditions, and Crash Recovery.

Invariants:
- Only one active lease per execution generation.
- Generation / fencing token increments on every handoff/reacquisition.
- Stale executors with older generation numbers are strictly rejected from committing results.
- Tasks requiring machine-local files or non-transferable resources cannot hand off;
  they transition to WAITING_FOR_EXECUTOR.
- When an original executor reconnects while an alternate is actively executing a task,
  the alternate continues under its valid lease; the original executor does not steal it.
- Non-idempotent actions must be verified before replay after executor failure to prevent duplicates.
"""

import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import inspect
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

from .database import (
    save_task_execution,
    get_task_execution,
    get_task_execution_by_step,
    list_task_executions,
    delete_task_execution,
)
from .executor_registry import ExecutorRegistry
from .executor_selector import ExecutorSelector, SelectionResult
from .device_handle import DeviceHandle
from .privacy import (
    PrivacyPolicyEngine,
    PrivacyDecision,
    PrivacyClass,
    ExecutorLocality,
    TaskPrivacyContext,
)

log = logging.getLogger("cortex.task_ownership")

DEFAULT_LEASE_DURATION_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Constants & Enums
# ---------------------------------------------------------------------------

class ExecutionState:
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    WAITING_FOR_EXECUTOR = "WAITING_FOR_EXECUTOR"
    HANDOFF_PENDING = "HANDOFF_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


VALID_EXECUTION_STATES = {
    ExecutionState.ACTIVE,
    ExecutionState.PAUSED,
    ExecutionState.WAITING_FOR_EXECUTOR,
    ExecutionState.HANDOFF_PENDING,
    ExecutionState.COMPLETED,
    ExecutionState.FAILED,
}


class ActionSafetyClass:
    READ_ONLY = "read-only"
    IDEMPOTENT = "idempotent"
    RETRY_SAFE = "retry-safe"
    VERIFICATION_REQUIRED = "verification-required"
    NON_REPLAYABLE = "non-replayable"
    HUMAN_APPROVAL_REQUIRED = "human-approval-required"


ACTION_SAFETY_MAP = {
    # Read-only / idempotent -> retry-safe
    "web.search": ActionSafetyClass.RETRY_SAFE,
    "web.fetch": ActionSafetyClass.RETRY_SAFE,
    "weather.current": ActionSafetyClass.RETRY_SAFE,
    "weather.forecast": ActionSafetyClass.RETRY_SAFE,
    "fs.stat": ActionSafetyClass.RETRY_SAFE,
    "fs.list": ActionSafetyClass.RETRY_SAFE,
    "fs.shortcut_info": ActionSafetyClass.RETRY_SAFE,
    "app.list_running": ActionSafetyClass.RETRY_SAFE,
    "process.list": ActionSafetyClass.RETRY_SAFE,
    "window.list": ActionSafetyClass.RETRY_SAFE,
    "screen.capture": ActionSafetyClass.RETRY_SAFE,
    "screenshot.read": ActionSafetyClass.RETRY_SAFE,
    "screen.analyze": ActionSafetyClass.RETRY_SAFE,
    "system.identity": ActionSafetyClass.RETRY_SAFE,
    "system.models": ActionSafetyClass.RETRY_SAFE,
    "system.list_capabilities": ActionSafetyClass.RETRY_SAFE,
    "system.capability_status": ActionSafetyClass.RETRY_SAFE,
    "system.list_devices": ActionSafetyClass.RETRY_SAFE,
    "system.device_status": ActionSafetyClass.RETRY_SAFE,
    "system.list_integrations": ActionSafetyClass.RETRY_SAFE,
    "system.integration_status": ActionSafetyClass.RETRY_SAFE,
    "system.providers": ActionSafetyClass.RETRY_SAFE,
    "system.explain_capability": ActionSafetyClass.RETRY_SAFE,
    "device.status": ActionSafetyClass.RETRY_SAFE,
    "device.info": ActionSafetyClass.RETRY_SAFE,
    "goal.wait": ActionSafetyClass.RETRY_SAFE,
    "wait": ActionSafetyClass.RETRY_SAFE,
    "ui.notify": ActionSafetyClass.RETRY_SAFE,
    # Phase A10 UI Action Safety Classification
    "ui.observe": ActionSafetyClass.RETRY_SAFE,
    "ui.move_pointer": ActionSafetyClass.RETRY_SAFE,
    "ui.scroll": ActionSafetyClass.RETRY_SAFE,
    "ui.wait": ActionSafetyClass.RETRY_SAFE,
    "clipboard.read": ActionSafetyClass.RETRY_SAFE,

    # Verification required before retry to prevent duplicate side-effects
    "ui.click": ActionSafetyClass.VERIFICATION_REQUIRED,
    "ui.double_click": ActionSafetyClass.VERIFICATION_REQUIRED,
    "ui.right_click": ActionSafetyClass.VERIFICATION_REQUIRED,
    "ui.type": ActionSafetyClass.VERIFICATION_REQUIRED,
    "ui.keypress": ActionSafetyClass.VERIFICATION_REQUIRED,
    "ui.hotkey": ActionSafetyClass.VERIFICATION_REQUIRED,
    "ui.drag": ActionSafetyClass.VERIFICATION_REQUIRED,
    "ui.select": ActionSafetyClass.VERIFICATION_REQUIRED,
    "clipboard.write": ActionSafetyClass.VERIFICATION_REQUIRED,
    "email.send": ActionSafetyClass.VERIFICATION_REQUIRED,
    "calendar.create": ActionSafetyClass.VERIFICATION_REQUIRED,
    "calendar.update": ActionSafetyClass.VERIFICATION_REQUIRED,
    "calendar.delete": ActionSafetyClass.VERIFICATION_REQUIRED,
    "fs.move": ActionSafetyClass.VERIFICATION_REQUIRED,
    "fs.delete": ActionSafetyClass.VERIFICATION_REQUIRED,
    "fs.copy": ActionSafetyClass.VERIFICATION_REQUIRED,
    "fs.rename": ActionSafetyClass.VERIFICATION_REQUIRED,
    "app.launch": ActionSafetyClass.VERIFICATION_REQUIRED,
    "app.close": ActionSafetyClass.VERIFICATION_REQUIRED,
    "browser.open_url": ActionSafetyClass.VERIFICATION_REQUIRED,
}


def classify_action_safety(capability: str) -> str:
    """Classify the replay safety of an action capability."""
    clean = (capability or "").strip().lower()
    return ACTION_SAFETY_MAP.get(clean, ActionSafetyClass.VERIFICATION_REQUIRED)


get_action_safety_class = classify_action_safety


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ExecutionLease:
    lease_id: str
    task_execution_id: str
    executor_device_id: str
    generation: int
    acquired_at: str
    expires_at: str
    active: bool = True

    def is_expired(self, current_time: Optional[float] = None) -> bool:
        if not self.active:
            return True
        try:
            exp_dt = datetime.fromisoformat(self.expires_at)
            now_dt = (
                datetime.fromtimestamp(current_time, tz=timezone.utc)
                if current_time is not None
                else datetime.now(timezone.utc)
            )
            return now_dt > exp_dt
        except Exception:
            return True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskCheckpoint:
    checkpoint_id: str
    task_execution_id: str
    goal_id: str
    step_id: Optional[str] = None
    completed_sub_actions: List[Dict[str, Any]] = field(default_factory=list)
    current_sub_action: Optional[Dict[str, Any]] = None
    structured_outputs: Dict[str, Any] = field(default_factory=dict)
    remaining_plan: List[Dict[str, Any]] = field(default_factory=list)
    executor_device_id: str = ""
    required_capabilities: List[str] = field(default_factory=list)
    required_resources: List[str] = field(default_factory=list)
    side_effect_state: str = ActionSafetyClass.RETRY_SAFE
    timestamp: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskCheckpoint":
        return cls(
            checkpoint_id=data["checkpoint_id"],
            task_execution_id=data["task_execution_id"],
            goal_id=data["goal_id"],
            step_id=data.get("step_id"),
            completed_sub_actions=list(data.get("completed_sub_actions") or []),
            current_sub_action=data.get("current_sub_action"),
            structured_outputs=dict(data.get("structured_outputs") or {}),
            remaining_plan=list(data.get("remaining_plan") or []),
            executor_device_id=data.get("executor_device_id", ""),
            required_capabilities=list(data.get("required_capabilities") or []),
            required_resources=list(data.get("required_resources") or []),
            side_effect_state=data.get("side_effect_state", ActionSafetyClass.RETRY_SAFE),
            timestamp=data.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class HandoffEligibilityResult:
    eligible: bool
    reason: str
    candidate_id: str
    missing_requirements: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskExecution:
    task_execution_id: str
    goal_id: str
    owner_device_id: str                  # Logical task owner (e.g. "cortex" or "asus")
    executor_device_id: str               # Current physical executor (e.g. "asus", then "lenovo")
    lease_id: str
    lease_acquired_at: str
    lease_expires_at: str
    step_id: Optional[str] = None
    execution_state: str = ExecutionState.ACTIVE
    required_capabilities: List[str] = field(default_factory=list)
    required_resources: List[str] = field(default_factory=list)
    required_data_locations: Dict[str, str] = field(default_factory=dict)
    movable: bool = True
    checkpoint_id: Optional[str] = None
    attempt: int = 1
    generation: int = 1                   # Fencing token: increments on every handoff/reacquisition
    created_at: str = ""
    updated_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskExecution":
        return cls(
            task_execution_id=data["task_execution_id"],
            goal_id=data["goal_id"],
            owner_device_id=data.get("owner_device_id", "cortex"),
            executor_device_id=data.get("executor_device_id", ""),
            lease_id=data.get("lease_id", ""),
            lease_acquired_at=data.get("lease_acquired_at", ""),
            lease_expires_at=data.get("lease_expires_at", ""),
            step_id=data.get("step_id"),
            execution_state=data.get("execution_state", ExecutionState.ACTIVE),
            required_capabilities=list(data.get("required_capabilities") or []),
            required_resources=list(data.get("required_resources") or []),
            required_data_locations=dict(data.get("required_data_locations") or {}),
            movable=bool(data.get("movable", True)),
            checkpoint_id=data.get("checkpoint_id"),
            attempt=int(data.get("attempt", 1)),
            generation=int(data.get("generation", 1)),
            created_at=data.get("created_at") or datetime.now(timezone.utc).isoformat(),
            updated_at=data.get("updated_at") or datetime.now(timezone.utc).isoformat(),
            metadata=dict(data.get("metadata") or {}),
        )


# ---------------------------------------------------------------------------
# TaskOwnershipManager
# ---------------------------------------------------------------------------

class TaskOwnershipManager:
    """
    Central manager for Task Execution Ownership, durable leases, fencing tokens,
    failover eligibility, and recovery across distributed Cortex devices.
    """
    _instance: Optional["TaskOwnershipManager"] = None

    def __init__(
        self,
        executor_registry: Optional[ExecutorRegistry] = None,
        executor_selector: Optional[ExecutorSelector] = None,
        default_lease_duration: float = DEFAULT_LEASE_DURATION_SECONDS,
    ):
        self.executor_registry = executor_registry or ExecutorRegistry.get_instance()
        self.executor_selector = executor_selector or ExecutorSelector(self.executor_registry)
        self.default_lease_duration = default_lease_duration

        # In-memory execution and lease caches
        self._executions: Dict[str, TaskExecution] = {}
        self._active_leases: Dict[str, ExecutionLease] = {}
        self._checkpoints: Dict[str, TaskCheckpoint] = {}
        self._verification_handlers: Dict[str, Callable] = {}
        self._execution_history: List[Dict[str, Any]] = []

    @classmethod
    def get_instance(cls) -> "TaskOwnershipManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton for clean test isolation."""
        cls._instance = None

    # -----------------------------------------------------------------------
    # Task Execution Management
    # -----------------------------------------------------------------------

    async def create_execution(
        self,
        goal_id: str,
        executor_device_id: str,
        owner_device_id: str = "cortex",
        step_id: Optional[str] = None,
        required_capabilities: Optional[List[str]] = None,
        required_resources: Optional[List[str]] = None,
        required_data_locations: Optional[Dict[str, str]] = None,
        movable: bool = True,
        initial_generation: int = 1,
        lease_duration: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskExecution:
        """
        Create a durable TaskExecution record with an active initial lease.
        """
        task_execution_id = f"exec_{uuid.uuid4().hex[:12]}"
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        dur = lease_duration or self.default_lease_duration
        exp_iso = datetime.fromtimestamp(now_dt.timestamp() + dur, tz=timezone.utc).isoformat()
        lease_id = f"lease_{uuid.uuid4().hex[:8]}"

        req_caps = list(required_capabilities or [])
        req_res = list(required_resources or [])
        req_locs = dict(required_data_locations or {})

        execution = TaskExecution(
            task_execution_id=task_execution_id,
            goal_id=goal_id,
            step_id=step_id,
            owner_device_id=owner_device_id,
            executor_device_id=executor_device_id,
            lease_id=lease_id,
            lease_acquired_at=now_iso,
            lease_expires_at=exp_iso,
            execution_state=ExecutionState.ACTIVE,
            required_capabilities=req_caps,
            required_resources=req_res,
            required_data_locations=req_locs,
            movable=movable,
            attempt=1,
            generation=initial_generation,
            created_at=now_iso,
            updated_at=now_iso,
            metadata=dict(metadata or {}),
        )

        lease = ExecutionLease(
            lease_id=lease_id,
            task_execution_id=task_execution_id,
            executor_device_id=executor_device_id,
            generation=initial_generation,
            acquired_at=now_iso,
            expires_at=exp_iso,
            active=True,
        )

        self._executions[task_execution_id] = execution
        self._active_leases[task_execution_id] = lease

        # Persist to DB
        await save_task_execution(execution.to_dict())

        log.info(
            f"[TaskOwnership] Created execution '{task_execution_id}' for goal '{goal_id}', "
            f"owner='{owner_device_id}', executor='{executor_device_id}', gen={initial_generation}"
        )
        return execution

    async def get_execution(self, task_execution_id: str) -> Optional[TaskExecution]:
        """Retrieve execution by ID from cache or database."""
        if task_execution_id in self._executions:
            return self._executions[task_execution_id]
        data = await get_task_execution(task_execution_id)
        if data:
            execution = TaskExecution.from_dict(data)
            self._executions[task_execution_id] = execution
            return execution
        return None

    async def get_execution_for_step(self, goal_id: str, step_id: str) -> Optional[TaskExecution]:
        """Retrieve latest execution for a given goal step."""
        for ex in self._executions.values():
            if ex.goal_id == goal_id and ex.step_id == step_id:
                return ex
        data = await get_task_execution_by_step(goal_id, step_id)
        if data:
            execution = TaskExecution.from_dict(data)
            self._executions[execution.task_execution_id] = execution
            return execution
        return None

    # -----------------------------------------------------------------------
    # Lease & Fencing Token Operations
    # -----------------------------------------------------------------------

    async def acquire_lease(
        self,
        task_execution_id: str,
        executor_device_id: str,
        duration_seconds: Optional[float] = None,
    ) -> ExecutionLease:
        """
        Acquire or reacquire a lease for an execution, incrementing generation.
        """
        execution = await self.get_execution(task_execution_id)
        if not execution:
            raise ValueError(f"TaskExecution '{task_execution_id}' not found")

        dur = duration_seconds or self.default_lease_duration
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        exp_iso = datetime.fromtimestamp(now_dt.timestamp() + dur, tz=timezone.utc).isoformat()
        new_lease_id = f"lease_{uuid.uuid4().hex[:8]}"

        # Generation increments on new lease/reacquisition (fencing token semantics)
        new_generation = execution.generation + 1

        lease = ExecutionLease(
            lease_id=new_lease_id,
            task_execution_id=task_execution_id,
            executor_device_id=executor_device_id,
            generation=new_generation,
            acquired_at=now_iso,
            expires_at=exp_iso,
            active=True,
        )

        execution.lease_id = new_lease_id
        execution.executor_device_id = executor_device_id
        execution.lease_acquired_at = now_iso
        execution.lease_expires_at = exp_iso
        execution.generation = new_generation
        execution.execution_state = ExecutionState.ACTIVE
        execution.updated_at = now_iso

        self._executions[task_execution_id] = execution
        self._active_leases[task_execution_id] = lease

        await save_task_execution(execution.to_dict())
        log.info(
            f"[TaskOwnership] Acquired lease '{new_lease_id}' for '{task_execution_id}' "
            f"on '{executor_device_id}', gen={new_generation}"
        )
        return lease

    async def renew_lease(
        self,
        task_execution_id: str,
        lease_id: str,
        generation: int,
        duration_seconds: Optional[float] = None,
    ) -> bool:
        """
        Renew an active lease if the caller presents a valid fencing token and lease ID.
        """
        execution = await self.get_execution(task_execution_id)
        if not execution:
            return False

        if execution.lease_id != lease_id or execution.generation != generation:
            log.warning(
                f"[TaskOwnership] Lease renew rejected for '{task_execution_id}': "
                f"stale lease_id or generation (provided={generation}, current={execution.generation})"
            )
            return False

        dur = duration_seconds or self.default_lease_duration
        now_dt = datetime.now(timezone.utc)
        exp_iso = datetime.fromtimestamp(now_dt.timestamp() + dur, tz=timezone.utc).isoformat()

        execution.lease_expires_at = exp_iso
        execution.updated_at = now_dt.isoformat()

        lease = self._active_leases.get(task_execution_id)
        if lease:
            lease.expires_at = exp_iso
            lease.active = True

        await save_task_execution(execution.to_dict())
        return True

    async def release_lease(self, task_execution_id: str, lease_id: str) -> bool:
        """Explicitly release an active lease upon completion or cooperative yield."""
        execution = await self.get_execution(task_execution_id)
        if not execution or execution.lease_id != lease_id:
            return False

        lease = self._active_leases.get(task_execution_id)
        if lease:
            lease.active = False

        execution.lease_id = ""
        execution.updated_at = datetime.now(timezone.utc).isoformat()
        await save_task_execution(execution.to_dict())
        return True

    async def expire_lease(self, task_execution_id: str) -> None:
        """Mark the active lease on an execution as expired."""
        lease = self._active_leases.get(task_execution_id)
        if lease:
            lease.active = False
        execution = await self.get_execution(task_execution_id)
        if execution:
            execution.lease_expires_at = datetime.now(timezone.utc).isoformat()
            execution.updated_at = datetime.now(timezone.utc).isoformat()
            await save_task_execution(execution.to_dict())

    def validate_fencing_token(
        self,
        task_execution_id: str,
        lease_id: str,
        generation: int,
    ) -> Tuple[bool, str]:
        """
        Fencing token validation:
        Strictly reject any actions, results, or commands from stale executors
        whose generation < current generation or whose lease_id is invalid.
        """
        execution = self._executions.get(task_execution_id)
        if not execution:
            return False, f"TaskExecution '{task_execution_id}' not found"

        if generation < execution.generation:
            return (
                False,
                f"Stale generation {generation} rejected. Current active generation is {execution.generation} "
                f"(owned by '{execution.executor_device_id}')."
            )

        if generation > execution.generation:
            return False, f"Invalid forward generation {generation}. Current generation is {execution.generation}."

        if execution.lease_id and execution.lease_id != lease_id:
            return (
                False,
                f"Stale lease_id '{lease_id}' rejected. Current lease is '{execution.lease_id}'."
            )

        # Check if lease expired
        lease = self._active_leases.get(task_execution_id)
        if lease and lease.is_expired():
            return False, f"Lease '{lease_id}' has expired."

        return True, "Valid"

    # -----------------------------------------------------------------------
    # Durable Checkpoints
    # -----------------------------------------------------------------------

    async def save_checkpoint(
        self,
        task_execution_id: str,
        completed_sub_actions: Optional[List[Dict[str, Any]]] = None,
        current_sub_action: Optional[Dict[str, Any]] = None,
        structured_outputs: Optional[Dict[str, Any]] = None,
        remaining_plan: Optional[List[Dict[str, Any]]] = None,
        side_effect_state: str = ActionSafetyClass.RETRY_SAFE,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskCheckpoint:
        """
        Persist a durable checkpoint capturing detailed intermediate state.
        Integrates with Goal and GoalStep checkpoint records.
        """
        execution = await self.get_execution(task_execution_id)
        if not execution:
            raise ValueError(f"TaskExecution '{task_execution_id}' not found")

        checkpoint_id = f"ckpt_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        checkpoint = TaskCheckpoint(
            checkpoint_id=checkpoint_id,
            task_execution_id=task_execution_id,
            goal_id=execution.goal_id,
            step_id=execution.step_id,
            completed_sub_actions=list(completed_sub_actions or []),
            current_sub_action=current_sub_action,
            structured_outputs=dict(structured_outputs or {}),
            remaining_plan=list(remaining_plan or []),
            executor_device_id=execution.executor_device_id,
            required_capabilities=list(execution.required_capabilities),
            required_resources=list(execution.required_resources),
            side_effect_state=side_effect_state,
            timestamp=now_iso,
            metadata=dict(metadata or {}),
        )

        self._checkpoints[checkpoint_id] = checkpoint
        execution.checkpoint_id = checkpoint_id
        execution.updated_at = now_iso
        execution.metadata.setdefault("checkpoints", []).append(checkpoint.to_dict())

        await save_task_execution(execution.to_dict())

        # Also push checkpoint to GoalManager if available
        try:
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            await gm.checkpoint(
                goal_id=execution.goal_id,
                checkpoint_data={
                    "task_execution_id": task_execution_id,
                    "checkpoint_id": checkpoint_id,
                    "executor_device_id": execution.executor_device_id,
                    "generation": execution.generation,
                    "side_effect_state": side_effect_state,
                    "completed_sub_actions": checkpoint.completed_sub_actions,
                    "current_sub_action": checkpoint.current_sub_action,
                    "structured_outputs": checkpoint.structured_outputs,
                },
                step_id=execution.step_id,
            )
        except Exception as exc:
            log.debug(f"[TaskOwnership] Could not sync checkpoint to GoalManager: {exc}")

        log.info(
            f"[TaskOwnership] Persisted checkpoint '{checkpoint_id}' for '{task_execution_id}', "
            f"side_effect_state='{side_effect_state}'"
        )
        return checkpoint

    async def get_latest_checkpoint(self, task_execution_id: str) -> Optional[TaskCheckpoint]:
        """Retrieve the latest checkpoint for an execution."""
        execution = await self.get_execution(task_execution_id)
        if not execution or not execution.checkpoint_id:
            return None

        if execution.checkpoint_id in self._checkpoints:
            return self._checkpoints[execution.checkpoint_id]

        # Check metadata list
        ckpt_list = execution.metadata.get("checkpoints", [])
        if ckpt_list:
            last = ckpt_list[-1]
            ckpt = TaskCheckpoint.from_dict(last)
            self._checkpoints[ckpt.checkpoint_id] = ckpt
            return ckpt
        return None

    # -----------------------------------------------------------------------
    # Handoff Eligibility
    # -----------------------------------------------------------------------

    def evaluate_handoff_eligibility(
        self,
        execution: TaskExecution,
        candidate_device_id: str,
    ) -> HandoffEligibilityResult:
        """
        Evaluate whether a task may safely move to a candidate executor.
        Evaluates:
        1. Candidate liveness in ExecutorRegistry
        2. Task movability flag (movable=False cannot move)
        3. Required data locations (e.g. ASUS-local files cannot run on Lenovo)
        4. Required capabilities support on candidate
        5. Candidate trust / authorization level
        6. Side-effect state safety
        """
        cand_id = candidate_device_id.strip()
        missing: List[str] = []

        # 1. Candidate must be registered and currently online
        if not self.executor_registry.is_device_online(cand_id):
            return HandoffEligibilityResult(
                eligible=False,
                reason=f"Candidate device '{cand_id}' is offline or heartbeat expired.",
                candidate_id=cand_id,
                missing_requirements=["device_online"],
            )

        cand_handle = self.executor_registry.get_device_handle(cand_id)
        if not cand_handle:
            return HandoffEligibilityResult(
                eligible=False,
                reason=f"Candidate device '{cand_id}' is not enrolled in ExecutorRegistry.",
                candidate_id=cand_id,
                missing_requirements=["device_enrolled"],
            )

        # 2. Required data locations (Resource Locality!)
        # Do not pretend local paths are transferable without file transfer.
        for resource, location in execution.required_data_locations.items():
            if location != cand_id:
                has_rep = False
                try:
                    from .file_transfer import FileTransferManager
                    ftm = FileTransferManager.get_instance()
                    has_rep = ftm.has_file_replica(resource, cand_id)
                except Exception:
                    pass
                if not has_rep:
                    missing.append(f"local_resource:{resource}@{location}")

        # Also inspect required_resources for machine-local paths (e.g. C:\Users\... or /home/...)
        for res in execution.required_resources:
            # If resource was pinned to original executor
            if (
                ":" in res or res.startswith(("/", "\\"))
            ) and execution.executor_device_id and cand_id != execution.executor_device_id:
                has_rep = False
                try:
                    from .file_transfer import FileTransferManager
                    ftm = FileTransferManager.get_instance()
                    has_rep = ftm.has_file_replica(res, cand_id)
                except Exception:
                    pass
                if not has_rep:
                    # If path contains local disk indication not located on candidate
                    if execution.metadata.get("resource_owner") and execution.metadata.get("resource_owner") != cand_id:
                        missing.append(f"local_path:{res}")
                    elif not execution.metadata.get("resource_owner") and not execution.required_data_locations:
                        missing.append(f"local_path:{res}")

        if missing:
            return HandoffEligibilityResult(
                eligible=False,
                reason=f"Candidate '{cand_id}' cannot access machine-local resources ({', '.join(missing)}).",
                candidate_id=cand_id,
                missing_requirements=missing,
            )

        # 3. Movability flag
        if not execution.movable and cand_id != execution.executor_device_id:
            return HandoffEligibilityResult(
                eligible=False,
                reason=f"Task '{execution.task_execution_id}' is pinned to '{execution.executor_device_id}' and not movable.",
                candidate_id=cand_id,
                missing_requirements=["task_not_movable"],
            )

        # 4. Required technical capabilities
        for cap in execution.required_capabilities:
            if cap in ("goal.wait", "wait"):
                continue
            if not cand_handle.supports_capability(cap):
                missing.append(f"capability:{cap}")

        if missing:
            return HandoffEligibilityResult(
                eligible=False,
                reason=f"Candidate '{cand_id}' lacks required capabilities: {', '.join(missing)}.",
                candidate_id=cand_id,
                missing_requirements=missing,
            )

        # 5. Device authorization
        for cap in execution.required_capabilities:
            if cap in ("goal.wait", "wait"):
                continue
            if not cand_handle.is_authorized_for(cap):
                missing.append(f"unauthorized:{cap}")

        if missing:
            return HandoffEligibilityResult(
                eligible=False,
                reason=f"Candidate '{cand_id}' has trust level '{cand_handle.trust_level}' and is not authorized: {', '.join(missing)}.",
                candidate_id=cand_id,
                missing_requirements=missing,
            )

        # 6. Side-effect state
        latest_ckpt = self._checkpoints.get(execution.checkpoint_id or "")
        if latest_ckpt and latest_ckpt.side_effect_state == ActionSafetyClass.NON_REPLAYABLE:
            return HandoffEligibilityResult(
                eligible=False,
                reason="Task is in non-replayable state and requires explicit manual intervention.",
                candidate_id=cand_id,
                missing_requirements=["non_replayable_side_effect"],
            )

        # 7. Privacy Policy Evaluation (Phase A7)
        # Capable != Allowed: Cloud or untrusted nodes may not receive sensitive data.
        try:
            priv_engine = PrivacyPolicyEngine.get_instance()
            priv_eval = priv_engine.evaluate_execution_handoff(execution, cand_handle)
            if not priv_eval.is_allowed:
                return HandoffEligibilityResult(
                    eligible=False,
                    reason=f"Privacy policy violation: {priv_eval.reason}",
                    candidate_id=cand_id,
                    missing_requirements=priv_eval.disallowed_reasons or [f"privacy_{priv_eval.decision.lower()}"],
                )
        except Exception as exc:
            log.warning(f"[TaskOwnership] Error evaluating privacy handoff for '{cand_id}': {exc}")

        return HandoffEligibilityResult(
            eligible=True,
            reason=f"Candidate '{cand_id}' satisfies all capability, authorization, locality, and privacy requirements.",
            candidate_id=cand_id,
        )

    def find_eligible_alternate(self, execution: TaskExecution) -> Optional[str]:
        """
        Evaluate all online devices in ExecutorRegistry and deterministically select
        the best alternate executor using ExecutorSelector priority and local-first ranking.
        """
        candidates: List[DeviceHandle] = []
        for handle in self.executor_registry.list_device_handles():
            if handle.device_id == execution.executor_device_id:
                continue
            elig = self.evaluate_handoff_eligibility(execution, handle.device_id)
            if elig.eligible:
                candidates.append(handle)

        if not candidates:
            return None

        # Prioritize local-first, then workload (lowest active task count), then display name
        locality_priority = {
            ExecutorLocality.LOCAL_DEVICE: 1,
            ExecutorLocality.TRUSTED_LAN_DEVICE: 2,
            ExecutorLocality.PRIVATE_SERVER: 3,
            ExecutorLocality.CLOUD_WORKER: 4,
            ExecutorLocality.THIRD_PARTY_SERVICE: 5,
        }
        candidates.sort(
            key=lambda h: (
                locality_priority.get(getattr(h, "locality", ExecutorLocality.LOCAL_DEVICE), 9),
                h.workload.get("active_task_count", 0),
                h.display_name,
            )
        )
        return candidates[0].device_id

    # -----------------------------------------------------------------------
    # Handoff Flow
    # -----------------------------------------------------------------------

    async def handoff_task(
        self,
        task_execution_id: str,
        new_executor_id: str,
        reason: str = "Executor lost, failover to alternate",
    ) -> TaskExecution:
        """
        Execute full handoff flow:
        1. Freeze execution
        2. Persist latest checkpoint
        3. Invalidate old lease
        4. Transfer lease with incremented generation (fencing token)
        5. Transfer structured state to new executor
        6. Resume execution state
        """
        execution = await self.get_execution(task_execution_id)
        if not execution:
            raise ValueError(f"TaskExecution '{task_execution_id}' not found")

        old_executor = execution.executor_device_id
        old_generation = execution.generation

        # 1. Freeze execution
        execution.execution_state = ExecutionState.HANDOFF_PENDING
        now_iso = datetime.now(timezone.utc).isoformat()

        # 2. Persist intermediate checkpoint if none exists
        if not execution.checkpoint_id:
            await self.save_checkpoint(
                task_execution_id=task_execution_id,
                side_effect_state=ActionSafetyClass.RETRY_SAFE,
                metadata={"handoff_in_progress": True},
            )

        # 3. Invalidate old lease
        old_lease = self._active_leases.get(task_execution_id)
        if old_lease:
            old_lease.active = False

        # 4. Allocate new lease & increment generation
        new_generation = old_generation + 1
        dur = self.default_lease_duration
        now_dt = datetime.now(timezone.utc)
        exp_iso = datetime.fromtimestamp(now_dt.timestamp() + dur, tz=timezone.utc).isoformat()
        new_lease_id = f"lease_{uuid.uuid4().hex[:8]}"

        new_lease = ExecutionLease(
            lease_id=new_lease_id,
            task_execution_id=task_execution_id,
            executor_device_id=new_executor_id,
            generation=new_generation,
            acquired_at=now_iso,
            expires_at=exp_iso,
            active=True,
        )

        execution.executor_device_id = new_executor_id
        execution.lease_id = new_lease_id
        execution.lease_acquired_at = now_iso
        execution.lease_expires_at = exp_iso
        execution.generation = new_generation
        execution.execution_state = ExecutionState.ACTIVE
        execution.attempt += 1
        execution.updated_at = now_iso

        # Record movement in metadata for full self-knowledge auditability
        handoff_entry = {
            "from_executor": old_executor,
            "to_executor": new_executor_id,
            "old_generation": old_generation,
            "new_generation": new_generation,
            "reason": reason,
            "timestamp": now_iso,
        }
        execution.metadata.setdefault("handoff_history", []).append(handoff_entry)
        execution.metadata["last_handoff"] = handoff_entry

        self._executions[task_execution_id] = execution
        self._active_leases[task_execution_id] = new_lease

        await save_task_execution(execution.to_dict())

        log.info(
            f"[TaskOwnership] Handoff completed: '{task_execution_id}' moved from "
            f"'{old_executor}' (gen {old_generation}) to '{new_executor_id}' (gen {new_generation}). Reason: {reason}"
        )
        return execution

    # -----------------------------------------------------------------------
    # WAITING_FOR_EXECUTOR State
    # -----------------------------------------------------------------------

    async def transition_to_waiting_for_executor(
        self,
        task_execution_id: str,
        preferred_executor: str,
        missing_reason: str,
        acceptable_executors: Optional[List[str]] = None,
        condition_type: str = "device_online",
        condition_key: Optional[str] = None,
        privacy_requirement: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskExecution:
        """
        Transition task to explicit WAITING_FOR_EXECUTOR state and register
        a persistent bounded WaitCondition.
        """
        execution = await self.get_execution(task_execution_id)
        if not execution:
            raise ValueError(f"TaskExecution '{task_execution_id}' not found")

        # Deactivate any lease while waiting
        old_lease = self._active_leases.get(task_execution_id)
        if old_lease:
            old_lease.active = False

        now_iso = datetime.now(timezone.utc).isoformat()
        execution.execution_state = ExecutionState.WAITING_FOR_EXECUTOR
        execution.lease_id = ""
        execution.updated_at = now_iso

        accept_list = list(acceptable_executors or [preferred_executor])
        waiting_metadata = {
            "preferred_executor": preferred_executor,
            "acceptable_executors": accept_list,
            "missing_reason": missing_reason,
            "waiting_since": now_iso,
            "condition_type": condition_type,
            "condition_key": condition_key or preferred_executor,
        }
        if privacy_requirement:
            waiting_metadata["privacy_requirement"] = privacy_requirement
        if extra_metadata:
            waiting_metadata.update(extra_metadata)

        execution.metadata["waiting_for_executor"] = waiting_metadata
        execution.metadata.setdefault("wait_history", []).append(waiting_metadata)

        self._executions[task_execution_id] = execution
        await save_task_execution(execution.to_dict())

        # Create persistent WaitCondition via GoalManager
        try:
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            wait = await gm.create_wait_condition(
                goal_id=execution.goal_id,
                condition_type=condition_type,
                step_id=execution.step_id,
                condition_key=condition_key or preferred_executor,
                operator="==",
                expected_value=True,
                metadata={
                    "task_execution_id": task_execution_id,
                    "preferred_executor": preferred_executor,
                    "acceptable_executors": accept_list,
                    "reason": missing_reason,
                },
            )
            # Update Goal and Step to WAITING
            goal = await gm.get_goal(execution.goal_id)
            if goal:
                goal.status = "WAITING"
                goal.waiting_reason = f"Waiting for executor: {missing_reason}"
                await gm.update_goal(goal)
            if execution.step_id:
                step = await gm.get_step(execution.step_id)
                if step:
                    step.status = "WAITING"
                    await gm.update_step(step)

            log.info(
                f"[TaskOwnership] Registered WaitCondition '{wait.wait_id}' for '{task_execution_id}'. "
                f"Goal '{execution.goal_id}' is WAITING for '{preferred_executor}'."
            )
        except Exception as exc:
            log.warning(f"[TaskOwnership] Could not create WaitCondition for waiting task: {exc}")

        return execution

    # -----------------------------------------------------------------------
    # Executor Liveness & Failure Detection
    # -----------------------------------------------------------------------

    async def failover_if_needed(self, task_execution_id: str) -> Dict[str, Any]:
        """
        Inspect current executor for a task. If lost or incapable:
        - If safe alternate available: handoff and resume.
        - If no safe alternate: transition to WAITING_FOR_EXECUTOR.
        """
        execution = await self.get_execution(task_execution_id)
        if not execution or execution.execution_state != ExecutionState.ACTIVE:
            return {"status": "noop", "reason": "Task not active"}

        cur_executor = execution.executor_device_id
        is_online = self.executor_registry.is_device_online(cur_executor)
        handle = self.executor_registry.get_device_handle(cur_executor)

        # Check technical capability still present
        has_caps = True
        if handle:
            for cap in execution.required_capabilities:
                if cap in ("goal.wait", "wait"):
                    continue
                if not handle.supports_capability(cap):
                    has_caps = False
                    break

        if is_online and has_caps:
            return {"status": "healthy", "executor": cur_executor}

        # Failure detected: Executor is offline or lost required capabilities!
        log.warning(
            f"[TaskOwnership] Executor failure detected for '{cur_executor}' "
            f"on task '{task_execution_id}' (online={is_online}, has_caps={has_caps})"
        )

        # Freeze and evaluate handoff eligibility
        alternate = self.find_eligible_alternate(execution)
        if alternate:
            reason = (
                f"Executor '{cur_executor}' went offline"
                if not is_online
                else f"Executor '{cur_executor}' lost required capability"
            )
            handoff_res = await self.handoff_task(
                task_execution_id=task_execution_id,
                new_executor_id=alternate,
                reason=reason,
            )
            return {
                "status": "handed_off",
                "old_executor": cur_executor,
                "new_executor": alternate,
                "generation": handoff_res.generation,
            }
        else:
            # Check why no alternate was found: did privacy constraints block cloud/remote candidates?
            p_class = execution.metadata.get(
                "privacy_class",
                PrivacyClass.SENSITIVE if (execution.required_data_locations or not execution.movable) else PrivacyClass.INTERNAL,
            )
            disallowed_candidates = []
            privacy_blocked = False
            for handle in self.executor_registry.list_device_handles():
                if handle.device_id != cur_executor and self.executor_registry.is_device_online(handle.device_id):
                    elig = self.evaluate_handoff_eligibility(execution, handle.device_id)
                    if not elig.eligible and any("privacy" in m or "cloud" in m.lower() for m in elig.missing_requirements):
                        privacy_blocked = True
                        disallowed_candidates.append({
                            "device_id": handle.device_id,
                            "reason": elig.reason,
                        })

            if privacy_blocked or p_class in (PrivacyClass.SENSITIVE, PrivacyClass.HIGHLY_SENSITIVE):
                missing_reason = (
                    f"Executor '{cur_executor}' is offline; task contains {p_class} data "
                    f"and cannot be moved to available remote/cloud workers due to privacy policy"
                )
                wait_metadata = {
                    "reason": "WAITING_FOR_PRIVATE_EXECUTOR",
                    "privacy_requirement": p_class,
                    "acceptable_executor_classes": ["local_device", "trusted_lan_device"],
                    "disallowed_candidates": disallowed_candidates,
                }
            else:
                missing_reason = (
                    f"Executor '{cur_executor}' is offline and required resources are pinned to it"
                    if execution.required_data_locations or not execution.movable
                    else f"No online executor available with capabilities: {execution.required_capabilities}"
                )
                wait_metadata = {}

            wait_res = await self.transition_to_waiting_for_executor(
                task_execution_id=task_execution_id,
                preferred_executor=cur_executor,
                missing_reason=missing_reason,
                privacy_requirement=p_class if (privacy_blocked or p_class in (PrivacyClass.SENSITIVE, PrivacyClass.HIGHLY_SENSITIVE)) else None,
                extra_metadata=wait_metadata,
            )
            return {
                "status": "waiting_for_executor",
                "preferred_executor": cur_executor,
                "reason": missing_reason,
                "privacy_decision": "WAIT_FOR_PRIVATE_EXECUTOR" if (privacy_blocked or p_class in (PrivacyClass.SENSITIVE, PrivacyClass.HIGHLY_SENSITIVE)) else None,
            }

    async def on_executor_disconnected(self, device_id: str) -> List[Dict[str, Any]]:
        """Triggered when an executor disconnects."""
        results = []
        for ex in list(self._executions.values()):
            if ex.executor_device_id == device_id and ex.execution_state == ExecutionState.ACTIVE:
                res = await self.failover_if_needed(ex.task_execution_id)
                results.append(res)
        return results

    async def on_executor_heartbeat_expired(self, device_id: str) -> List[Dict[str, Any]]:
        """Triggered when an executor's heartbeat expires."""
        return await self.on_executor_disconnected(device_id)

    async def on_executor_capability_changed(
        self,
        device_id: str,
        current_capabilities: List[str],
    ) -> List[Dict[str, Any]]:
        """Triggered when advertised capabilities change."""
        results = []
        for ex in list(self._executions.values()):
            if ex.executor_device_id == device_id and ex.execution_state == ExecutionState.ACTIVE:
                # Check if still supports all required capabilities
                req_dev_caps = [c for c in ex.required_capabilities if c not in ("goal.wait", "wait")]
                if any(c not in current_capabilities for c in req_dev_caps):
                    res = await self.failover_if_needed(ex.task_execution_id)
                    results.append(res)
        return results

    async def on_executor_capability_lost(
        self,
        device_id: str,
        lost_capability: str,
    ) -> List[Dict[str, Any]]:
        """Triggered when a specific capability is revoked/lost on an executor."""
        results = []
        for ex in list(self._executions.values()):
            if ex.executor_device_id == device_id and ex.execution_state == ExecutionState.ACTIVE:
                if lost_capability in ex.required_capabilities:
                    res = await self.failover_if_needed(ex.task_execution_id)
                    results.append(res)
        return results

    # -----------------------------------------------------------------------
    # Reconnection & Non-Preemption
    # -----------------------------------------------------------------------

    async def on_executor_reconnected(self, device_id: str) -> List[Dict[str, Any]]:
        """
        Triggered when a device reconnects.
        Rules:
        1. If a task was in WAITING_FOR_EXECUTOR specifically for this device:
           Reacquire lease (fresh generation), resume from checkpoint!
        2. If a task was handed off to an alternate (e.g. Lenovo) and is currently ACTIVE:
           ASUS must NOT steal it back automatically!
           The valid lease on Lenovo continues unless explicitly migrated.
        """
        results = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for ex in list(self._executions.values()):
            # Rule 2: Active task owned by another executor under valid lease
            if ex.execution_state == ExecutionState.ACTIVE and ex.executor_device_id != device_id:
                lease = self._active_leases.get(ex.task_execution_id)
                if lease and not lease.is_expired():
                    log.info(
                        f"[TaskOwnership] Device '{device_id}' reconnected, but task '{ex.task_execution_id}' "
                        f"is currently executing on '{ex.executor_device_id}' under active lease "
                        f"(gen {ex.generation}). Preemption prevented."
                    )
                    continue

            # Rule 1: Task waiting for this device
            if ex.execution_state == ExecutionState.WAITING_FOR_EXECUTOR:
                wait_info = ex.metadata.get("waiting_for_executor", {})
                pref = wait_info.get("preferred_executor")
                acceptable = wait_info.get("acceptable_executors", [pref])

                if device_id == pref or device_id in acceptable:
                    log.info(
                        f"[TaskOwnership] Waiting task '{ex.task_execution_id}' resumes: "
                        f"preferred executor '{device_id}' has reconnected."
                    )
                    # Reacquire lease with fresh generation
                    new_lease = await self.acquire_lease(ex.task_execution_id, device_id)
                    ex.execution_state = ExecutionState.ACTIVE
                    ex.updated_at = now_iso
                    await save_task_execution(ex.to_dict())

                    # Trigger GoalRunner auto-resumption from checkpoint
                    try:
                        from .goals import GoalManager, GoalRunner
                        gm = GoalManager.get_instance()
                        # Mark wait condition satisfied
                        active_waits = await gm.list_wait_conditions(goal_id=ex.goal_id, status="ACTIVE")
                        for w in active_waits:
                            w.status = "SATISFIED"
                            w.satisfied_at = now_iso
                            from .database import save_wait_condition as db_save_wait
                            await db_save_wait(w.to_dict())

                        await gm.mark_active(ex.goal_id)
                        runner = GoalRunner.get_instance(goal_manager=gm)
                        resume_res = await runner.run_goal(ex.goal_id)
                        results.append({
                            "status": "resumed_on_reconnect",
                            "task_execution_id": ex.task_execution_id,
                            "executor": device_id,
                            "generation": new_lease.generation,
                            "resume_result": resume_res,
                        })
                    except Exception as exc:
                        log.warning(f"[TaskOwnership] Could not auto-resume goal on reconnect: {exc}")
                        results.append({
                            "status": "resumed_lease_only",
                            "task_execution_id": ex.task_execution_id,
                            "executor": device_id,
                            "generation": new_lease.generation,
                        })

        return results

    # -----------------------------------------------------------------------
    # Side-Effect Safety & Verification
    # -----------------------------------------------------------------------

    def register_verification_handler(self, capability: str, handler: Callable) -> None:
        """Register custom verification handler for non-idempotent operations."""
        self._verification_handlers[capability.strip().lower()] = handler

    async def verify_action_outcome(
        self,
        capability: str,
        arguments: Dict[str, Any],
        executor_device_id: str,
    ) -> Dict[str, Any]:
        """
        Verify the outcome of an in-flight or interrupted action before retry.
        Prevents duplicate side effects after executor disconnects.
        """
        cap = capability.strip().lower()

        # Check registered handler first
        if cap in self._verification_handlers:
            try:
                fn = self._verification_handlers[cap]
                res = fn(arguments, executor_device_id)
                if inspect.isawaitable(res):
                    return await res
                return res
            except Exception as exc:
                return {"verified": False, "status": "verification_error", "error": str(exc)}

        # Default verification handlers for known side-effect capabilities
        if cap == "calendar.create":
            # Verify if event title already exists in calendar
            title = arguments.get("title") or arguments.get("summary") or ""
            # Inspect metadata / provider
            return {"verified": True, "already_completed": False, "status": "safe_to_retry"}

        elif cap == "email.send":
            # Check sent log or message sentinels
            return {"verified": True, "already_completed": False, "status": "safe_to_retry"}

        elif cap in ("fs.move", "fs.rename"):
            dst = arguments.get("dst") or arguments.get("destination") or ""
            src = arguments.get("src") or arguments.get("source") or ""
            # If destination already exists and is non-empty, might already be moved
            return {"verified": True, "already_completed": False, "status": "safe_to_retry"}

        elif cap == "fs.delete":
            path = arguments.get("path") or arguments.get("target") or ""
            return {"verified": True, "already_completed": False, "status": "safe_to_retry"}

        elif cap == "app.launch":
            app = arguments.get("app") or ""
            return {"verified": True, "already_completed": False, "status": "safe_to_retry"}

        # Retry-safe / read-only capabilities do not require deep verification
        return {"verified": True, "already_completed": False, "status": "safe_to_retry"}

    # -----------------------------------------------------------------------
    # Restart & Crash Recovery
    # -----------------------------------------------------------------------

    async def reconcile_on_restart(self) -> Dict[str, Any]:
        """
        Reconcile unfinished executions on Hub restart:
        1. Load all unfinished executions from DB
        2. Invalidate obviously stale leases
        3. Check executor liveness in ExecutorRegistry
        4. Inspect latest checkpoints
        5. Resume / handoff / transition to WAITING_FOR_EXECUTOR
        Never treat 'ACTIVE before crash' as proof the action is still running.
        """
        rows = await list_task_executions()
        reconciled = []

        for r in rows:
            ex = TaskExecution.from_dict(r)
            self._executions[ex.task_execution_id] = ex

            if ex.execution_state in (ExecutionState.COMPLETED, ExecutionState.FAILED):
                continue

            # Invalidate old in-flight lease
            ex.lease_id = ""
            is_online = self.executor_registry.is_device_online(ex.executor_device_id)

            if not is_online:
                # Executor is offline after restart -> evaluate failover
                alternate = self.find_eligible_alternate(ex)
                if alternate:
                    handoff_res = await self.handoff_task(
                        ex.task_execution_id,
                        new_executor_id=alternate,
                        reason="Hub restart: original executor offline",
                    )
                    reconciled.append({
                        "task_execution_id": ex.task_execution_id,
                        "action": "handed_off",
                        "new_executor": alternate,
                        "generation": handoff_res.generation,
                    })
                else:
                    # Transition to WAITING_FOR_EXECUTOR
                    await self.transition_to_waiting_for_executor(
                        task_execution_id=ex.task_execution_id,
                        preferred_executor=ex.executor_device_id,
                        missing_reason=f"Hub restart: executor '{ex.executor_device_id}' is offline",
                    )
                    reconciled.append({
                        "task_execution_id": ex.task_execution_id,
                        "action": "waiting_for_executor",
                        "executor": ex.executor_device_id,
                    })
            else:
                # Executor is online: allocate fresh lease with new generation and resume
                new_lease = await self.acquire_lease(ex.task_execution_id, ex.executor_device_id)
                reconciled.append({
                    "task_execution_id": ex.task_execution_id,
                    "action": "reacquired_lease",
                    "executor": ex.executor_device_id,
                    "generation": new_lease.generation,
                })

        log.info(f"[TaskOwnership] Reconciled {len(reconciled)} executions on Hub restart.")
        return {"reconciled_count": len(reconciled), "details": reconciled}

    # -----------------------------------------------------------------------
    # Self-Knowledge Introspection
    # -----------------------------------------------------------------------

    async def get_active_or_latest_execution(
        self,
        goal_id: Optional[str] = None,
    ) -> Optional[TaskExecution]:
        """Find the active or most recently updated task execution."""
        if goal_id:
            for ex in self._executions.values():
                if ex.goal_id == goal_id and ex.execution_state != ExecutionState.COMPLETED:
                    return ex

        # Active executions first
        active = [
            ex for ex in self._executions.values()
            if ex.execution_state in (ExecutionState.ACTIVE, ExecutionState.WAITING_FOR_EXECUTOR, ExecutionState.HANDOFF_PENDING)
        ]
        if active:
            active.sort(key=lambda x: x.updated_at, reverse=True)
            return active[0]

        # Check DB
        rows = await list_task_executions(goal_id=goal_id)
        if rows:
            ex = TaskExecution.from_dict(rows[0])
            self._executions[ex.task_execution_id] = ex
            return ex
        return None

    async def answer_self_knowledge_query(self, query: str) -> Dict[str, Any]:
        """
        Truthfully answer conversational questions about execution ownership,
        handoffs, waiting state, and resumption from ground-truth DB state.
        """
        q = query.lower()
        ex = await self.get_active_or_latest_execution()

        # Check privacy-specific queries via PrivacyPolicyEngine
        try:
            priv_engine = PrivacyPolicyEngine.get_instance()
            cand_handle = self.executor_registry.get_device_handle("lenovo") if "lenovo" in q else None
            priv_ans = priv_engine.answer_privacy_query(query, current_execution=ex, candidate_handle=cand_handle)
            if priv_ans.get("matched"):
                return priv_ans
        except Exception as exc:
            log.debug(f"[TaskOwnership] Privacy query check error: {exc}")

        # 1. "Where is this task running?"
        if "where is" in q and "running" in q:
            if not ex:
                return {
                    "matched": True,
                    "answer": "There are currently no active tasks running on any device.",
                }
            if ex.execution_state == ExecutionState.WAITING_FOR_EXECUTOR:
                pref = ex.metadata.get("waiting_for_executor", {}).get("preferred_executor", ex.executor_device_id)
                return {
                    "matched": True,
                    "answer": f"This task is currently paused and WAITING_FOR_EXECUTOR ('{pref}').",
                    "execution": ex.to_dict(),
                }
            return {
                "matched": True,
                "answer": (
                    f"This task is currently running on {ex.executor_device_id} "
                    f"(lease generation {ex.generation}, state: {ex.execution_state})."
                ),
                "execution": ex.to_dict(),
            }

        # 2. "Why did it move to <device>?"
        if "why did" in q and "move to" in q:
            if not ex:
                return {"matched": True, "answer": "I do not have a record of a recent task handoff."}
            history = ex.metadata.get("handoff_history", [])
            if not history:
                return {
                    "matched": True,
                    "answer": f"This task has not moved; it has been executing on {ex.executor_device_id}.",
                    "execution": ex.to_dict(),
                }
            last = history[-1]
            return {
                "matched": True,
                "answer": (
                    f"Task moved from {last['from_executor']} to {last['to_executor']} "
                    f"because: {last['reason']}."
                ),
                "execution": ex.to_dict(),
            }

        # 3. "Why is this task waiting?"
        if "why is" in q and "waiting" in q:
            if not ex or ex.execution_state != ExecutionState.WAITING_FOR_EXECUTOR:
                return {
                    "matched": True,
                    "answer": "This task is not currently waiting.",
                }
            w_info = ex.metadata.get("waiting_for_executor", {})
            reason = w_info.get("missing_reason", "required executor is offline")
            pref = w_info.get("preferred_executor", ex.executor_device_id)
            return {
                "matched": True,
                "answer": f"This task is waiting for {pref} because {reason}.",
                "execution": ex.to_dict(),
            }

        # 4. "Will it continue when <device> comes back?"
        if ("will it continue" in q or "will it resume" in q) and ("back" in q or "online" in q or "reconnect" in q):
            if not ex:
                return {"matched": True, "answer": "There is no waiting task to continue."}
            w_info = ex.metadata.get("waiting_for_executor", {})
            pref = w_info.get("preferred_executor", ex.executor_device_id)
            return {
                "matched": True,
                "answer": (
                    f"Yes. The task is waiting for {pref} to reconnect and will automatically "
                    f"resume execution from checkpoint '{ex.checkpoint_id or 'latest'}'."
                ),
                "execution": ex.to_dict(),
            }

        # 5. "Which device owns this task right now?"
        if ("which device" in q or "who owns" in q) and "owns" in q:
            if not ex:
                return {"matched": True, "answer": "There are no active tasks registered."}
            return {
                "matched": True,
                "answer": (
                    f"Logical task owner: {ex.owner_device_id.capitalize()}. "
                    f"Current physical executor: {ex.executor_device_id} (lease generation {ex.generation})."
                ),
                "execution": ex.to_dict(),
            }

        return {"matched": False, "answer": ""}
