"""
hub/goals.py
-------------
Phase 9A: Persistent Goal Engine Foundation.

Core Goal:
Persistent goal and step model that survives Hub restarts and can represent
where a long-running autonomous task stands, without executing autonomous loops.

Responsibilities:
- Goal & GoalStep models with strict status validations
- Immediate database persistence on every state transition and checkpoint
- Safe startup recovery: ACTIVE/RUNNING normalized to safe WAITING; terminal states preserved
- Bounded safety limits (max_steps, max_failures)
- Internal Tier 1 capability execution (create, get, list, cancel, resume)
- Minimal deterministic natural language handlers
"""

import re
import asyncio
import uuid
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List

from .database import (
    save_goal as db_save_goal,
    get_goal as db_get_goal,
    list_goals as db_list_goals,
    delete_goal as db_delete_goal,
    save_goal_step as db_save_goal_step,
    get_goal_step as db_get_goal_step,
    list_goal_steps as db_list_goal_steps,
    delete_goal_step as db_delete_goal_step,
    save_wait_condition as db_save_wait_condition,
    get_wait_condition as db_get_wait_condition,
    list_wait_conditions as db_list_wait_conditions,
    delete_wait_condition as db_delete_wait_condition,
)

log = logging.getLogger("cortex.goals")

# ---------------------------------------------------------------------------
# Status Constants & Validation
# ---------------------------------------------------------------------------

VALID_GOAL_STATUSES = {
    "PENDING",
    "ACTIVE",
    "WAITING",
    "BLOCKED",
    "NEEDS_APPROVAL",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
}

TERMINAL_GOAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}

VALID_STEP_STATUSES = {
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "SKIPPED",
    "WAITING",
}

VALID_WAIT_STATUSES = {
    "ACTIVE",
    "SATISFIED",
    "CANCELLED",
    "EXPIRED",
}

VALID_CONDITION_TYPES = {
    "world_state",
    "time",
    "device_online",
    "capable_executor_available",
    "resource_owner_online",
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class GoalStep:
    """A bounded discrete step within a Goal."""
    step_id: str
    goal_id: str
    index: int
    description: str
    capability: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    status: str = "PENDING"
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    checkpoint_data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in VALID_STEP_STATUSES:
            raise ValueError(f"Invalid GoalStep status: '{self.status}'. Must be one of {VALID_STEP_STATUSES}")
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["step_index"] = self.index
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GoalStep":
        idx = data.get("index")
        if idx is None:
            idx = data.get("step_index", 0)
        return cls(
            step_id=data["step_id"],
            goal_id=data["goal_id"],
            index=int(idx),
            description=data.get("description", ""),
            capability=data.get("capability", ""),
            arguments=data.get("arguments") or {},
            status=data.get("status", "PENDING"),
            created_at=data.get("created_at") or datetime.now(timezone.utc).isoformat(),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            result=data.get("result"),
            error=data.get("error"),
            checkpoint_data=data.get("checkpoint_data") or {},
        )


@dataclass
class Goal:
    """A persistent long-running Goal container and state record."""
    goal_id: str
    title: str
    original_request: str
    source: str = "user"
    status: str = "PENDING"
    priority: int = 1
    created_at: str = ""
    updated_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    current_step_index: int = 0
    max_steps: int = 20
    failure_count: int = 0
    max_failures: int = 3
    max_runtime_seconds: Optional[int] = 3600
    consecutive_failures: int = 0
    max_consecutive_failures: int = 3
    replan_count: int = 0
    max_replans: int = 2
    checkpoint_data: Dict[str, Any] = field(default_factory=dict)
    waiting_reason: Optional[str] = None
    blocking_reason: Optional[str] = None
    approval_request_id: Optional[str] = None
    result_summary: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in VALID_GOAL_STATUSES:
            raise ValueError(f"Invalid Goal status: '{self.status}'. Must be one of {VALID_GOAL_STATUSES}")
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Goal":
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            goal_id=data["goal_id"],
            title=data.get("title", ""),
            original_request=data.get("original_request", ""),
            source=data.get("source", "user"),
            status=data.get("status", "PENDING"),
            priority=int(data.get("priority", 1)),
            created_at=data.get("created_at") or now,
            updated_at=data.get("updated_at") or now,
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            current_step_index=int(data.get("current_step_index", 0)),
            max_steps=int(data.get("max_steps", 20)),
            failure_count=int(data.get("failure_count", 0)),
            max_failures=int(data.get("max_failures", 3)),
            max_runtime_seconds=int(data["max_runtime_seconds"]) if data.get("max_runtime_seconds") is not None else None,
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            max_consecutive_failures=int(data.get("max_consecutive_failures", 3)),
            replan_count=int(data.get("replan_count", 0)),
            max_replans=int(data.get("max_replans", 2)),
            checkpoint_data=data.get("checkpoint_data") or {},
            waiting_reason=data.get("waiting_reason"),
            blocking_reason=data.get("blocking_reason"),
            approval_request_id=data.get("approval_request_id"),
            result_summary=data.get("result_summary"),
            metadata=data.get("metadata") or {},
        )


@dataclass
class WaitCondition:
    """A persistent bounded wait condition for long-running Goals and Steps."""
    wait_id: str
    goal_id: str
    condition_type: str
    step_id: Optional[str] = None
    condition_key: str = ""
    operator: str = "=="
    expected_value: Any = ""
    status: str = "ACTIVE"
    created_at: str = ""
    satisfied_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in VALID_WAIT_STATUSES:
            raise ValueError(f"Invalid WaitCondition status: '{self.status}'. Must be one of {VALID_WAIT_STATUSES}")
        if self.condition_type not in VALID_CONDITION_TYPES:
            raise ValueError(f"Invalid condition_type: '{self.condition_type}'. Must be one of {VALID_CONDITION_TYPES}")
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WaitCondition":
        return cls(
            wait_id=data["wait_id"],
            goal_id=data["goal_id"],
            step_id=data.get("step_id"),
            condition_type=data.get("condition_type", "world_state"),
            condition_key=data.get("condition_key", ""),
            operator=data.get("operator", "=="),
            expected_value=data.get("expected_value", ""),
            status=data.get("status", "ACTIVE"),
            created_at=data.get("created_at") or datetime.now(timezone.utc).isoformat(),
            satisfied_at=data.get("satisfied_at"),
            metadata=data.get("metadata") or {},
        )


# ---------------------------------------------------------------------------
# Goal Manager
# ---------------------------------------------------------------------------

class GoalManager:
    """
    State and persistence manager for long-running goals and steps.
    Survives restarts, enforces bounded safety limits, and performs immediate checkpointing.
    """
    _instance: Optional["GoalManager"] = None

    def __init__(self):
        self._goals_cache: Dict[str, Goal] = {}
        self._eval_lock = asyncio.Lock()
        self._resuming_goals: set = set()
        self._autonomy_enabled: Optional[bool] = None

    @classmethod
    def get_instance(cls) -> "GoalManager":
        if cls._instance is None:
            cls._instance = GoalManager()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton for test isolation."""
        cls._instance = None

    async def is_autonomy_enabled(self) -> bool:
        """Check if global autonomy is enabled (persisted in SQLite)."""
        if self._autonomy_enabled is None:
            from .database import get_autonomy_state
            val = await get_autonomy_state("autonomy_enabled", "true")
            self._autonomy_enabled = (val.lower() == "true") if val else True
        return self._autonomy_enabled

    async def set_autonomy_enabled(self, enabled: bool) -> None:
        """Set global autonomy state and persist to SQLite."""
        self._autonomy_enabled = bool(enabled)
        from .database import set_autonomy_state
        await set_autonomy_state("autonomy_enabled", "true" if enabled else "false")
        log.info(f"[GoalManager] Global autonomy enabled set to: {self._autonomy_enabled}")

    async def pause_all_goals(self, reason: str = "Paused by user") -> List[str]:
        """
        Pause all ACTIVE goals:
        ACTIVE goals -> WAITING
        waiting_reason = "Paused by user"
        metadata["user_paused"] = True
        Active autonomous execution stops safely.
        Wait conditions remain persisted.
        Returns list of paused goal IDs.
        """
        goals = await self.list_goals(status="ACTIVE")
        paused_ids = []
        now = datetime.now(timezone.utc).isoformat()
        for g in goals:
            g.status = "WAITING"
            g.waiting_reason = reason
            g.metadata["user_paused"] = True
            g.metadata["paused_at"] = now
            await self.update_goal(g)
            paused_ids.append(g.goal_id)
        log.info(f"[GoalManager] Paused {len(paused_ids)} active goal(s): {paused_ids}")
        return paused_ids

    async def resume_all_goals(self) -> List[str]:
        """
        Only resumes goals that were specifically paused by the user.
        Do not resume:
        BLOCKED, NEEDS_APPROVAL, FAILED, CANCELLED, COMPLETED, or goals waiting on external wait conditions.
        """
        goals = await self.list_goals(status="WAITING")
        resumed_ids = []
        for g in goals:
            is_user_paused = g.metadata.get("user_paused") is True or g.waiting_reason in ("Paused by user", "Autonomy stopped by user")
            if not is_user_paused:
                continue

            active_waits = await self.list_wait_conditions(goal_id=g.goal_id, status="ACTIVE")
            if active_waits:
                continue

            g.metadata.pop("user_paused", None)
            g.waiting_reason = None
            g.status = "ACTIVE"
            await self.update_goal(g)
            resumed_ids.append(g.goal_id)

        log.info(f"[GoalManager] Resumed {len(resumed_ids)} user-paused goal(s): {resumed_ids}")
        return resumed_ids

    async def initialize(self) -> int:
        """Initialize and run restart recovery on startup."""
        return await self.recover_on_startup()

    # -----------------------------------------------------------------------
    # Restart Recovery
    # -----------------------------------------------------------------------

    async def recover_on_startup(self) -> int:
        """
        On Hub startup:
        - COMPLETED / FAILED / CANCELLED -> remain terminal
        - WAITING / BLOCKED / NEEDS_APPROVAL -> restore exactly
        - ACTIVE / RUNNING at process death -> normalize safely to WAITING with:
            waiting_reason = "Paused due to Hub restart"
        Preserves current step index and checkpoint data.
        Does NOT automatically continue execution after restart.
        """
        # Ensure autonomy state is loaded from SQLite
        await self.is_autonomy_enabled()

        goals_data = await db_list_goals()
        recovered_count = 0
        now = datetime.now(timezone.utc).isoformat()

        for g_dict in goals_data:
            goal = Goal.from_dict(g_dict)
            needs_save = False

            # Check steps for running state
            steps = await self.list_steps(goal.goal_id)
            for s in steps:
                if s.status == "RUNNING":
                    s.status = "WAITING"
                    await self.update_step(s)

            if goal.status == "ACTIVE":
                goal.status = "WAITING"
                goal.waiting_reason = "Paused due to Hub restart"
                goal.updated_at = now
                needs_save = True
                recovered_count += 1
            elif goal.status in ("WAITING", "BLOCKED", "NEEDS_APPROVAL"):
                # Restored exactly
                pass
            elif goal.status in TERMINAL_GOAL_STATUSES:
                # Terminal status preserved
                pass

            if needs_save:
                await self.update_goal(goal, allow_terminal=True)
            else:
                self._goals_cache[goal.goal_id] = goal

        log.info(f"[GoalManager] Restart recovery complete: {recovered_count} active goals paused safely.")
        return recovered_count

    # -----------------------------------------------------------------------
    # Goal CRUD & State Transitions
    # -----------------------------------------------------------------------

    async def create_goal(
        self,
        title: str,
        original_request: str = "",
        source: str = "user",
        priority: int = 1,
        status: str = "ACTIVE",
        max_steps: int = 20,
        max_failures: int = 3,
        max_runtime_seconds: Optional[int] = 3600,
        max_consecutive_failures: int = 3,
        max_replans: int = 2,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Goal:
        """Create and immediately persist a Goal container."""
        if status not in VALID_GOAL_STATUSES:
            raise ValueError(f"Invalid initial goal status: '{status}'")

        goal_id = f"goal_{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()
        goal = Goal(
            goal_id=goal_id,
            title=title.strip(),
            original_request=original_request.strip() or title.strip(),
            source=source,
            status=status,
            priority=priority,
            created_at=now,
            updated_at=now,
            started_at=now if status == "ACTIVE" else None,
            max_steps=max(1, max_steps),
            max_failures=max(1, max_failures),
            max_runtime_seconds=max_runtime_seconds,
            consecutive_failures=0,
            max_consecutive_failures=max(1, max_consecutive_failures),
            replan_count=0,
            max_replans=max(1, max_replans),
            metadata=metadata or {},
        )
        await db_save_goal(goal.to_dict())
        self._goals_cache[goal.goal_id] = goal
        log.info(f"[GoalManager] Created goal '{goal.title}' ({goal.goal_id}) status={goal.status}")
        return goal

    async def get_goal(self, goal_id: str) -> Optional[Goal]:
        """Retrieve a Goal by ID, prioritizing DB for ground truth."""
        row = await db_get_goal(goal_id)
        if not row:
            return None
        goal = Goal.from_dict(row)
        self._goals_cache[goal.goal_id] = goal
        return goal

    async def list_goals(self, status: Optional[str] = None) -> List[Goal]:
        """List goals ordered deterministically by created_at ASC."""
        rows = await db_list_goals(status=status)
        goals = [Goal.from_dict(r) for r in rows]
        for g in goals:
            self._goals_cache[g.goal_id] = g
        return goals

    async def update_goal(self, goal: Goal, allow_terminal: bool = False) -> Goal:
        """Persist updates to an existing Goal with terminal-state checks."""
        existing = await self.get_goal(goal.goal_id)
        if existing and existing.status in TERMINAL_GOAL_STATUSES and not allow_terminal:
            raise ValueError(f"Cannot mutate goal '{goal.goal_id}' in terminal state '{existing.status}'.")

        goal.updated_at = datetime.now(timezone.utc).isoformat()
        await db_save_goal(goal.to_dict())
        self._goals_cache[goal.goal_id] = goal
        return goal

    async def mark_active(self, goal_id: str) -> Goal:
        """Transition goal to ACTIVE."""
        goal = await self._ensure_mutable_goal(goal_id)
        goal.status = "ACTIVE"
        if not goal.started_at:
            goal.started_at = datetime.now(timezone.utc).isoformat()
        goal.waiting_reason = None
        goal.blocking_reason = None
        return await self.update_goal(goal)

    async def mark_waiting(self, goal_id: str, reason: str) -> Goal:
        """Transition goal to WAITING with explicit reason."""
        goal = await self._ensure_mutable_goal(goal_id)
        goal.status = "WAITING"
        goal.waiting_reason = reason
        return await self.update_goal(goal)

    async def mark_blocked(self, goal_id: str, reason: str) -> Goal:
        """Transition goal to BLOCKED with explicit reason."""
        goal = await self._ensure_mutable_goal(goal_id)
        goal.status = "BLOCKED"
        goal.blocking_reason = reason
        return await self.update_goal(goal)

    async def mark_needs_approval(self, goal_id: str, approval_request_id: str) -> Goal:
        """Transition goal to NEEDS_APPROVAL."""
        goal = await self._ensure_mutable_goal(goal_id)
        goal.status = "NEEDS_APPROVAL"
        goal.approval_request_id = approval_request_id
        return await self.update_goal(goal)

    async def complete_goal(self, goal_id: str, result_summary: str = "") -> Goal:
        """Transition goal to terminal COMPLETED state."""
        goal = await self.get_goal(goal_id)
        if not goal:
            raise ValueError(f"Goal '{goal_id}' not found.")
        goal.status = "COMPLETED"
        goal.completed_at = datetime.now(timezone.utc).isoformat()
        goal.result_summary = result_summary
        return await self.update_goal(goal, allow_terminal=True)

    async def fail_goal(self, goal_id: str, error: str = "") -> Goal:
        """Transition goal to terminal FAILED state."""
        goal = await self.get_goal(goal_id)
        if not goal:
            raise ValueError(f"Goal '{goal_id}' not found.")
        goal.status = "FAILED"
        goal.completed_at = datetime.now(timezone.utc).isoformat()
        goal.blocking_reason = error
        return await self.update_goal(goal, allow_terminal=True)

    async def cancel_goal(self, goal_id: str, reason: str = "Cancelled by user") -> Goal:
        """Transition goal to terminal CANCELLED state and cancel active wait conditions."""
        goal = await self.get_goal(goal_id)
        if not goal:
            raise ValueError(f"Goal '{goal_id}' not found.")
        if goal.status == "COMPLETED":
            raise ValueError(f"Cannot cancel completed goal '{goal_id}'.")
        goal.status = "CANCELLED"
        goal.completed_at = datetime.now(timezone.utc).isoformat()
        goal.blocking_reason = reason
        updated = await self.update_goal(goal, allow_terminal=True)

        # Phase 9C: Cancel all active wait conditions for this goal
        waits = await self.list_wait_conditions(goal_id=goal_id, status="ACTIVE")
        for w in waits:
            w.status = "CANCELLED"
            await db_save_wait_condition(w.to_dict())
            log.info(f"[GoalManager] Cancelled wait condition '{w.wait_id}' due to goal cancellation")

        return updated

    async def resume_goal(self, goal_id: str) -> Goal:
        """Resume a WAITING, BLOCKED, or PENDING goal to ACTIVE."""
        goal = await self.get_goal(goal_id)
        if not goal:
            raise ValueError(f"Goal '{goal_id}' not found.")
        if goal.status in TERMINAL_GOAL_STATUSES:
            raise ValueError(f"Cannot resume goal '{goal_id}' in terminal state '{goal.status}'.")

        goal.status = "ACTIVE"
        goal.waiting_reason = None
        goal.blocking_reason = None
        if not goal.started_at:
            goal.started_at = datetime.now(timezone.utc).isoformat()
        return await self.update_goal(goal)

    async def checkpoint(
        self,
        goal_id: str,
        checkpoint_data: Dict[str, Any],
        step_id: Optional[str] = None,
    ) -> Goal:
        """Immediately persist checkpoint metadata to goal and optional step."""
        goal = await self._ensure_mutable_goal(goal_id)
        goal.checkpoint_data.update(checkpoint_data)
        if step_id:
            step = await self.get_step(step_id)
            if step:
                step.checkpoint_data.update(checkpoint_data)
                await self.update_step(step)
        return await self.update_goal(goal)

    # -----------------------------------------------------------------------
    # Step CRUD & Safety Enforcement
    # -----------------------------------------------------------------------

    async def add_step(
        self,
        goal_id: str,
        description: str,
        capability: str = "",
        arguments: Optional[Dict[str, Any]] = None,
        index: Optional[int] = None,
    ) -> GoalStep:
        """Add a step to a goal, enforcing max_steps safety limit."""
        goal = await self._ensure_mutable_goal(goal_id)
        existing_steps = await self.list_steps(goal_id)

        # Enforce max_steps safety limit
        if len(existing_steps) >= goal.max_steps:
            reason = f"Exceeded maximum allowed steps ({goal.max_steps})"
            goal.status = "BLOCKED"
            goal.blocking_reason = reason
            await self.update_goal(goal)
            raise ValueError(f"Goal '{goal_id}' cannot add step: {reason}.")

        step_index = index if index is not None else len(existing_steps)
        step_id = f"step_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()

        step = GoalStep(
            step_id=step_id,
            goal_id=goal_id,
            index=step_index,
            description=description.strip(),
            capability=capability.strip(),
            arguments=arguments or {},
            status="PENDING",
            created_at=now,
        )
        await db_save_goal_step(step.to_dict())
        log.info(f"[GoalManager] Added step {step_index} '{step.description}' to goal {goal_id}")
        return step

    async def get_step(self, step_id: str) -> Optional[GoalStep]:
        """Retrieve a GoalStep by step_id."""
        row = await db_get_goal_step(step_id)
        return GoalStep.from_dict(row) if row else None

    async def list_steps(self, goal_id: str) -> List[GoalStep]:
        """List steps for a goal ordered deterministically by step index."""
        rows = await db_list_goal_steps(goal_id)
        return [GoalStep.from_dict(r) for r in rows]

    async def update_step(self, step: GoalStep) -> GoalStep:
        """Persist updates to a GoalStep."""
        await db_save_goal_step(step.to_dict())
        return step

    async def delete_step(self, step_id: str) -> bool:
        """Delete a step by step_id."""
        return await db_delete_goal_step(step_id)

    async def start_step(self, goal_id: str, step_id: str) -> GoalStep:
        """Transition step to RUNNING and ensure parent goal is ACTIVE."""
        goal = await self._ensure_mutable_goal(goal_id)
        step = await self.get_step(step_id)
        if not step or step.goal_id != goal_id:
            raise ValueError(f"Step '{step_id}' not found for goal '{goal_id}'.")

        step.status = "RUNNING"
        step.started_at = datetime.now(timezone.utc).isoformat()
        await self.update_step(step)

        if goal.status != "ACTIVE":
            goal.status = "ACTIVE"
            if not goal.started_at:
                goal.started_at = step.started_at
            await self.update_goal(goal)

        return step

    async def complete_step(
        self,
        goal_id: str,
        step_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> GoalStep:
        """Mark a step COMPLETED and increment goal's current_step_index."""
        goal = await self._ensure_mutable_goal(goal_id)
        step = await self.get_step(step_id)
        if not step or step.goal_id != goal_id:
            raise ValueError(f"Step '{step_id}' not found for goal '{goal_id}'.")

        step.status = "COMPLETED"
        step.completed_at = datetime.now(timezone.utc).isoformat()
        step.result = result
        await self.update_step(step)

        # Success resets consecutive failures
        goal.consecutive_failures = 0

        goal.current_step_index += 1
        if goal.current_step_index > goal.max_steps:
            goal.status = "BLOCKED"
            goal.blocking_reason = f"Exceeded maximum allowed steps ({goal.max_steps})"

        await self.update_goal(goal)
        return step

    async def fail_step(self, goal_id: str, step_id: str, error: str = "") -> GoalStep:
        """Mark a step FAILED and enforce max_failures & consecutive failure limits on parent goal."""
        goal = await self._ensure_mutable_goal(goal_id)
        step = await self.get_step(step_id)
        if not step or step.goal_id != goal_id:
            raise ValueError(f"Step '{step_id}' not found for goal '{goal_id}'.")

        step.status = "FAILED"
        step.completed_at = datetime.now(timezone.utc).isoformat()
        step.error = error
        await self.update_step(step)

        goal.failure_count += 1
        goal.consecutive_failures += 1

        if goal.consecutive_failures >= goal.max_consecutive_failures:
            reason = f"Exceeded maximum consecutive failures ({goal.max_consecutive_failures}): {error or 'Step failed'}"
            goal.status = "FAILED"
            goal.blocking_reason = reason
            await self.update_goal(goal, allow_terminal=True)
        elif goal.failure_count >= goal.max_failures:
            reason = f"Exceeded maximum allowed failures ({goal.max_failures}): {error or 'Step failed'}"
            goal.status = "FAILED"
            goal.blocking_reason = reason
            await self.update_goal(goal, allow_terminal=True)
        else:
            await self.update_goal(goal)

        return step

    # -----------------------------------------------------------------------
    # Wait Condition Management & Evaluation (Phase 9C)
    # -----------------------------------------------------------------------

    async def create_wait_condition(
        self,
        goal_id: str,
        condition_type: str,
        condition_key: str = "",
        operator: str = "==",
        expected_value: Any = "",
        step_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> WaitCondition:
        """Create and immediately persist a WaitCondition."""
        goal = await self._ensure_mutable_goal(goal_id)

        wait_id = f"wait_{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()
        wait = WaitCondition(
            wait_id=wait_id,
            goal_id=goal_id,
            step_id=step_id,
            condition_type=condition_type,
            condition_key=condition_key,
            operator=operator,
            expected_value=expected_value,
            status="ACTIVE",
            created_at=now,
            satisfied_at=None,
            metadata=metadata or {},
        )
        await db_save_wait_condition(wait.to_dict())
        log.info(f"[GoalManager] Created wait condition '{wait_id}' for goal '{goal_id}' ({condition_type}:{condition_key})")
        return wait

    async def get_wait_condition(self, wait_id: str) -> Optional[WaitCondition]:
        """Retrieve a WaitCondition by ID."""
        row = await db_get_wait_condition(wait_id)
        if not row:
            return None
        return WaitCondition.from_dict(row)

    async def list_wait_conditions(
        self,
        goal_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[WaitCondition]:
        """List wait conditions, optionally filtered by goal_id and/or status."""
        rows = await db_list_wait_conditions(goal_id=goal_id, status=status)
        return [WaitCondition.from_dict(r) for r in rows]

    async def cancel_wait_condition(self, wait_id: str) -> bool:
        """Cancel an individual wait condition."""
        wait = await self.get_wait_condition(wait_id)
        if not wait:
            return False
        if wait.status != "ACTIVE":
            return False
        wait.status = "CANCELLED"
        await db_save_wait_condition(wait.to_dict())
        log.info(f"[GoalManager] Cancelled wait condition '{wait_id}'")
        return True

    async def is_condition_satisfied(
        self,
        condition_type: str,
        condition_key: str = "",
        operator: str = "==",
        expected_value: Any = "",
        metadata: Optional[Dict[str, Any]] = None,
        current_time: Optional[Any] = None,
    ) -> bool:
        """Check whether a condition is satisfied based on current system state."""
        meta = metadata or {}
        if condition_type == "device_online":
            dev_id = condition_key or meta.get("device_id") or meta.get("target_device") or ""
            return await self._check_device_online(dev_id, meta)
        elif condition_type == "capable_executor_available":
            cap = condition_key or meta.get("capability") or ""
            return await self._check_capable_executor_available(cap, meta)
        elif condition_type == "resource_owner_online":
            res_owner = condition_key or meta.get("resource_owner") or meta.get("owner") or ""
            return await self._check_resource_owner_online(res_owner, meta)
        elif condition_type == "world_state":
            key = condition_key or meta.get("signal_key") or meta.get("key") or ""
            return await self._check_world_state(key, operator, expected_value, meta)
        elif condition_type == "time":
            target = expected_value if (expected_value != "" and expected_value is not None) else (meta.get("target_time") or condition_key)
            return self._check_time(target, current_time=current_time)
        return False

    async def _check_device_online(self, device_id: str, metadata: Dict[str, Any]) -> bool:
        if not device_id:
            return False
        clean_dev = device_id.lower().strip()
        if clean_dev.startswith("device.online:"):
            clean_dev = clean_dev.split(":", 1)[1]

        # 1. Check live connected devices map in ControlLayer / main
        try:
            from .goals import GoalRunner
            runner = GoalRunner.get_instance()
            ctrl = runner.get_control_layer()
            if ctrl and hasattr(ctrl, "connected_devices") and isinstance(ctrl.connected_devices, dict):
                if clean_dev in ctrl.connected_devices or any(k.lower() == clean_dev for k in ctrl.connected_devices.keys()):
                    return True
                return False
        except Exception:
            pass

        # 0. Check ExecutorRegistry (Phase A5/A6 ground-truth)
        try:
            from .executor_registry import ExecutorRegistry
            er = ExecutorRegistry.get_instance()
            if er.is_device_online(clean_dev):
                return True
        except Exception:
            pass

        try:
            import hub.main
            if hasattr(hub.main, "CONNECTED_DEVICES"):
                if clean_dev in hub.main.CONNECTED_DEVICES or any(k.lower() == clean_dev for k in hub.main.CONNECTED_DEVICES.keys()):
                    return True
        except Exception:
            pass

        # 2. Check WorldStateManager signals
        try:
            from .world_state import WorldStateManager
            ws_mgr = WorldStateManager.get_instance()
            sig = ws_mgr.get_signal(f"device.online:{clean_dev}") or ws_mgr.get_signal(clean_dev)
            if not sig:
                for k, s in getattr(ws_mgr, "_signals", {}).items():
                    if k.lower() == f"device.online:{clean_dev}" and s.is_fresh():
                        sig = s
                        break
            if sig and sig.is_fresh():
                payload = sig.payload
                if isinstance(payload, dict):
                    if payload.get("is_online") is True or payload.get("is_connected") is True:
                        return True
                    if payload.get("is_online") is False or payload.get("is_connected") is False:
                        return False
                elif payload is True or str(payload).lower() in ("true", "online", "connected"):
                    return True
        except Exception:
            pass

        # 3. Check DB connection_state
        try:
            from .database import get_device
            dev = await get_device(clean_dev)
            if dev and dev.get("connection_state") == "connected":
                return True
        except Exception:
            pass

        return False

    async def _check_capable_executor_available(self, capability: str, metadata: Dict[str, Any]) -> bool:
        if not capability:
            return False
        clean_cap = capability.strip()
        try:
            from .executor_registry import ExecutorRegistry
            er = ExecutorRegistry.get_instance()
            capable = er.get_capable_devices(clean_cap, online_only=True)
            return len(capable) > 0
        except Exception:
            return False

    async def _check_resource_owner_online(self, owner_or_res: str, metadata: Dict[str, Any]) -> bool:
        clean = owner_or_res.strip()
        owner = metadata.get("resource_owner") or metadata.get("preferred_executor") or clean
        return await self._check_device_online(owner, metadata)

    async def _check_world_state(
        self,
        key: str,
        operator: str,
        expected_value: Any,
        metadata: Dict[str, Any],
    ) -> bool:
        if not key:
            return False
        clean_key = key.strip()

        actual_val = None
        found = False

        try:
            from .world_state import WorldStateManager
            ws_mgr = WorldStateManager.get_instance()
            sig = ws_mgr.get_signal(clean_key)
            if not sig:
                for k, s in getattr(ws_mgr, "_signals", {}).items():
                    if k.lower() == clean_key.lower() and s.is_fresh():
                        sig = s
                        break
            if sig and sig.is_fresh():
                found = True
                payload = sig.payload
                if isinstance(payload, dict):
                    subkey = clean_key.split(".")[-1]
                    if clean_key in payload:
                        actual_val = payload[clean_key]
                    elif subkey in payload:
                        actual_val = payload[subkey]
                    elif "value" in payload:
                        actual_val = payload["value"]
                    elif len(payload) == 1:
                        actual_val = next(iter(payload.values()))
                    else:
                        actual_val = payload
                else:
                    actual_val = payload
        except Exception:
            pass

        if not found:
            return False

        return self._compare_values(actual_val, operator, expected_value)

    def _check_time(self, target_time_val: Any, current_time: Optional[Any] = None) -> bool:
        if target_time_val is None or target_time_val == "":
            return False

        if current_time is None:
            now = datetime.now(timezone.utc)
        elif isinstance(current_time, (int, float)):
            now = datetime.fromtimestamp(current_time, tz=timezone.utc)
        elif isinstance(current_time, str):
            now = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        elif isinstance(current_time, datetime):
            now = current_time
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        else:
            now = datetime.now(timezone.utc)

        try:
            if isinstance(target_time_val, (int, float)):
                target_dt = datetime.fromtimestamp(target_time_val, tz=timezone.utc)
            elif isinstance(target_time_val, str):
                target_dt = datetime.fromisoformat(target_time_val.replace("Z", "+00:00"))
                if target_dt.tzinfo is None:
                    target_dt = target_dt.replace(tzinfo=timezone.utc)
            elif isinstance(target_time_val, datetime):
                target_dt = target_time_val
                if target_dt.tzinfo is None:
                    target_dt = target_dt.replace(tzinfo=timezone.utc)
            else:
                return False
        except Exception:
            return False

        return now >= target_dt

    def _compare_values(self, actual: Any, op: str, expected: Any) -> bool:
        if op == "==":
            if isinstance(expected, bool):
                if isinstance(actual, str):
                    return (actual.lower() in ("true", "1")) == expected
                return bool(actual) is expected
            if isinstance(actual, (int, float)) and isinstance(expected, (int, float, str)):
                try:
                    return float(actual) == float(expected)
                except (ValueError, TypeError):
                    pass
            return str(actual).lower() == str(expected).lower()
        elif op == "!=":
            return not self._compare_values(actual, "==", expected)
        elif op == ">=":
            try:
                return float(actual) >= float(expected)
            except (ValueError, TypeError):
                return str(actual) >= str(expected)
        elif op == "<=":
            try:
                return float(actual) <= float(expected)
            except (ValueError, TypeError):
                return str(actual) <= str(expected)
        elif op == ">":
            try:
                return float(actual) > float(expected)
            except (ValueError, TypeError):
                return str(actual) > str(expected)
        elif op == "<":
            try:
                return float(actual) < float(expected)
            except (ValueError, TypeError):
                return str(actual) < str(expected)
        elif op == "in":
            return str(actual).lower() in str(expected).lower()
        elif op == "contains":
            return str(expected).lower() in str(actual).lower()
        return False

    async def evaluate_wait_conditions(self, current_time: Optional[Any] = None) -> List[Dict[str, Any]]:
        """
        Evaluate all ACTIVE wait conditions against current world state, device state, and time.
        If satisfied:
        - wait.status = 'SATISFIED', wait.satisfied_at = now
        - step.status = 'COMPLETED' (if wait step) or 'PENDING'
        - goal.status = 'ACTIVE'
        - resume using GoalRunner.run_goal
        Guarantees:
        - Atomic evaluation via lock
        - Cancelled/terminal goals never resume
        - Duplicate signals do not resume twice
        """
        if not hasattr(self, "_eval_lock"):
            self._eval_lock = asyncio.Lock()
        if not hasattr(self, "_resuming_goals"):
            self._resuming_goals = set()

        if not await self.is_autonomy_enabled():
            log.info("[GoalManager] Autonomy is disabled by global kill switch; skipping wait condition evaluation/resume.")
            return []

        async with self._eval_lock:
            active_waits = await self.list_wait_conditions(status="ACTIVE")
            if not active_waits:
                return []

            satisfied_results = []
            for wait in active_waits:
                goal = await self.get_goal(wait.goal_id)
                if not goal or goal.status in TERMINAL_GOAL_STATUSES:
                    if goal and goal.status == "CANCELLED":
                        wait.status = "CANCELLED"
                        await db_save_wait_condition(wait.to_dict())
                    continue

                satisfied = await self.is_condition_satisfied(
                    condition_type=wait.condition_type,
                    condition_key=wait.condition_key,
                    operator=wait.operator,
                    expected_value=wait.expected_value,
                    metadata=wait.metadata,
                    current_time=current_time,
                )
                if not satisfied:
                    continue

                # Condition is SATISFIED
                now_iso = datetime.now(timezone.utc).isoformat()
                wait.status = "SATISFIED"
                wait.satisfied_at = now_iso
                await db_save_wait_condition(wait.to_dict())
                log.info(f"[GoalManager] WaitCondition '{wait.wait_id}' SATISFIED for goal '{goal.goal_id}'")

                # Phase A6: Reacquire lease for TaskExecution if waiting for executor
                task_exec_id = (wait.metadata or {}).get("task_execution_id")
                if task_exec_id:
                    try:
                        from .task_ownership import TaskOwnershipManager, ExecutionState
                        tom = TaskOwnershipManager.get_instance()
                        t_ex = await tom.get_execution(task_exec_id)
                        if t_ex and t_ex.execution_state == ExecutionState.WAITING_FOR_EXECUTOR:
                            target_exec = wait.condition_key if wait.condition_type == "device_online" else t_ex.executor_device_id
                            await tom.acquire_lease(task_exec_id, target_exec)
                            t_ex.execution_state = ExecutionState.ACTIVE
                            from .database import save_task_execution
                            await save_task_execution(t_ex.to_dict())
                    except Exception as exc:
                        log.debug(f"[GoalManager] Could not reacquire lease for TaskExecution on resume: {exc}")


                # Update step status
                if wait.step_id:
                    step = await self.get_step(wait.step_id)
                    if step and step.status == "WAITING":
                        if step.capability in ("goal.wait", "wait"):
                            await self.complete_step(
                                goal.goal_id,
                                step.step_id,
                                result={
                                    "status": "success",
                                    "message": f"Wait condition satisfied: {wait.condition_type} ({wait.condition_key})",
                                    "data": {"wait_id": wait.wait_id, "satisfied_at": now_iso},
                                },
                            )
                        else:
                            step.status = "PENDING"
                            await self.update_step(step)

                # Check if there are other ACTIVE wait conditions for this goal
                remaining_active = await self.list_wait_conditions(goal_id=goal.goal_id, status="ACTIVE")
                if remaining_active:
                    continue

                # Transition goal to ACTIVE using fresh DB state
                await self.mark_active(goal.goal_id)

                # Avoid duplicate resume runs for the same goal
                if goal.goal_id in self._resuming_goals:
                    continue

                self._resuming_goals.add(goal.goal_id)
                try:
                    runner = GoalRunner.get_instance(goal_manager=self)
                    run_res = await runner.run_goal(goal.goal_id)
                    satisfied_results.append({
                        "wait_id": wait.wait_id,
                        "goal_id": goal.goal_id,
                        "resume_result": run_res,
                    })
                finally:
                    self._resuming_goals.discard(goal.goal_id)

            return satisfied_results

    # -----------------------------------------------------------------------
    # Natural Language Handler
    # -----------------------------------------------------------------------

    async def handle_natural_language(self, req: str, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """
        Handle minimal goal engine natural language requests:
        - "Create a goal to organize my school files."
        - "What goals are active?"
        - "Pause/cancel that goal." / "Cancel the school files goal."
        - "Resume my file organization goal."
        """
        clean = req.lower().strip(" \t.,!?;:")

        # Phase 9D Safety: Global Kill Switch / Autonomy Stop
        if re.search(r'\b(?:stop\s+all\s+autonomous\s+tasks|stop\s+autonomy|kill\s+switch|disable\s+autonomy|emergency\s+stop)\b', clean):
            await self.set_autonomy_enabled(False)
            paused = await self.pause_all_goals(reason="Autonomy stopped by user")
            return f"Autonomous execution disabled by global kill switch. Paused {len(paused)} active goal(s)."

        # Phase 9D Safety: Enable Autonomy
        if re.search(r'\b(?:enable\s+autonomous\s+execution|enable\s+autonomy|start\s+autonomy)\b', clean):
            await self.set_autonomy_enabled(True)
            return "Autonomous execution is now enabled."

        # Phase 9D Safety: Check Autonomy Enabled
        if re.search(r'\b(?:is\s+autonomous\s+execution\s+enabled|is\s+autonomy\s+enabled|autonomy\s+status|check\s+autonomy)\b', clean):
            enabled = await self.is_autonomy_enabled()
            status_str = "enabled" if enabled else "disabled"
            return f"Autonomous execution is {status_str}."

        # Phase 9D Safety: Pause All Goals
        if re.search(r'\b(?:pause\s+all\s+(?:my\s+)?goals|pause\s+all\s+goals|pause\s+goals)\b', clean):
            paused = await self.pause_all_goals(reason="Paused by user")
            if paused:
                return f"Paused {len(paused)} active goal(s)."
            return "No active goals to pause."

        # Phase 9D Safety: Resume All User-Paused Goals
        if re.search(r'\b(?:resume\s+(?:all\s+)?(?:my\s+)?paused\s+goals|resume\s+all\s+goals)\b', clean):
            resumed = await self.resume_all_goals()
            if resumed:
                return f"Resumed {len(resumed)} paused goal(s)."
            return "No user-paused goals found to resume."

        # Phase 9E: Create and run / plan and run goal
        m_create_run = re.search(r'\b(?:create\s+and\s+run|plan\s+and\s+run|create\s+and\s+execute)\s+(?:a\s+)?goal\s+(?:to\s+|for\s+)?(.+)', req, re.IGNORECASE)
        if m_create_run:
            raw_title = m_create_run.group(1).strip(" \t.,!?;:")
            title = re.sub(r'^(?:to|for)\s+', '', raw_title, flags=re.IGNORECASE).strip()
            if title:
                goal = await self.create_goal(title=title, original_request=title, source="user", status="ACTIVE")
                from .planner import GoalPlanner
                planner = GoalPlanner.get_instance(goal_manager=self)
                plan_res = await planner.plan_goal(goal.goal_id)
                if plan_res.get("status") == "blocked":
                    return f"Goal '{goal.title}' was created but blocked: {plan_res.get('reason')}"
                elif plan_res.get("status") == "planned":
                    runner = GoalRunner.get_instance(goal_manager=self)
                    run_res = await runner.run_goal(goal.goal_id)
                    return f"Goal '{goal.title}' planned ({plan_res.get('step_count')} steps) and executed: status={run_res.get('status')}."
                return f"Goal '{goal.title}' created and planned."

        # 1. Create a goal: "Create a goal to organize my school files"
        m_create = re.search(r'\b(?:create|add|start|set\s+up|new)\s+(?:a\s+)?goal\s+(?:to\s+|for\s+)?(.+)', req, re.IGNORECASE)
        if m_create:
            raw_title = m_create.group(1).strip(" \t.,!?;:")
            # Clean leading prepositions
            title = re.sub(r'^(?:to|for)\s+', '', raw_title, flags=re.IGNORECASE).strip()
            if title:
                goal = await self.create_goal(title=title, original_request=req, source="user", status="ACTIVE")
                return f"I created the goal '{goal.title}' (ID: {goal.goal_id}) and saved it. Autonomous execution is not enabled yet."

        # 2. Query active goals: "What goals are active?"
        if re.search(r'\b(?:what\s+goals\s+are\s+active|what\s+are\s+my\s+active\s+goals|active\s+goals|list\s+(?:active\s+)?goals|show\s+(?:active\s+)?goals|what\s+goals\s+do\s+i\s+have)\b', clean):
            goals = await self.list_goals()
            active_goals = [g for g in goals if g.status in ("ACTIVE", "WAITING", "PENDING", "NEEDS_APPROVAL", "BLOCKED")]
            if not active_goals:
                return "You have no active goals."
            if len(active_goals) == 1:
                g = active_goals[0]
                st_desc = f"{g.status} ({g.waiting_reason})" if g.waiting_reason else g.status
                return f"You have 1 active goal: '{g.title}' (ID: {g.goal_id}, status: {st_desc})."
            else:
                lines = [f"You have {len(active_goals)} active goals:"]
                for g in active_goals:
                    st_desc = f"{g.status} ({g.waiting_reason})" if g.waiting_reason else g.status
                    lines.append(f"• '{g.title}' (ID: {g.goal_id}, status: {st_desc})")
                return "\n".join(lines)

        # 3. Cancel goal: "Cancel the school files goal" / "Cancel that goal"
        m_cancel = re.search(r'^(?:please\s+)?(?:cancel|pause|abort)\s+(?:the\s+|my\s+|that\s+)?(.+?)(?:\s+goal)?$', clean)
        if m_cancel and not re.search(r'\b(?:create|add|what|list|resume)\b', clean):
            target_str = m_cancel.group(1).strip(" \t.,!?;:")
            goals = await self.list_goals()
            target_goal = self._find_matching_goal(target_str, goals)
            if target_goal:
                try:
                    cancelled = await self.cancel_goal(target_goal.goal_id, reason="Cancelled by user")
                    return f"Cancelled goal '{cancelled.title}' (ID: {cancelled.goal_id}). Status is now CANCELLED."
                except ValueError as e:
                    return f"Cannot cancel goal: {e}"
            return f"I couldn't find an active goal matching '{target_str}' to cancel."

        # 4. Resume goal: "Resume my file organization goal"
        m_resume = re.search(r'^(?:please\s+)?resume\s+(?:the\s+|my\s+|that\s+)?(.+?)(?:\s+goal)?$', clean)
        if m_resume:
            target_str = m_resume.group(1).strip(" \t.,!?;:")
            goals = await self.list_goals()
            target_goal = self._find_matching_goal(target_str, goals)
            if target_goal:
                try:
                    resumed = await self.resume_goal(target_goal.goal_id)
                    return f"Resumed goal '{resumed.title}' (ID: {resumed.goal_id}) to ACTIVE."
                except ValueError as e:
                    return f"Cannot resume goal: {e}"
            return f"I couldn't find a goal matching '{target_str}' to resume."

        # 5. Run goal: "Run my <goal> goal." / "Run that goal."
        m_run = re.search(r'^(?:please\s+)?run\s+(?:the\s+|my\s+|that\s+)?(.+?)(?:\s+goal)?$', clean)
        if m_run and not re.search(r'\b(?:create|add|what|list|cancel|pause|resume|action)\b', clean):
            target_str = m_run.group(1).strip(" \t.,!?;:")
            if target_str and not target_str.startswith("to "):
                goals = await self.list_goals()
                target_goal = self._find_matching_goal(target_str, goals)
                if target_goal:
                    runner = GoalRunner.get_instance(goal_manager=self)
                    await runner.run_goal(target_goal.goal_id)
                    updated = await self.get_goal(target_goal.goal_id)
                    st = updated.status if updated else "UNKNOWN"
                    if st == "COMPLETED":
                        return f"Ran goal '{target_goal.title}' (ID: {target_goal.goal_id}). All steps completed successfully."
                    elif st == "BLOCKED":
                        return f"Goal '{target_goal.title}' (ID: {target_goal.goal_id}) is blocked: {updated.blocking_reason}."
                    elif st == "NEEDS_APPROVAL":
                        return f"Goal '{target_goal.title}' (ID: {target_goal.goal_id}) paused: requires approval (ID: {updated.approval_request_id})."
                    elif st == "FAILED":
                        return f"Goal '{target_goal.title}' (ID: {target_goal.goal_id}) failed: {updated.blocking_reason or 'Step failure'}."
                    else:
                        return f"Goal '{target_goal.title}' (ID: {target_goal.goal_id}) status is now {st}."
                return f"I couldn't find an active goal matching '{target_str}' to run."

        return None

    # -----------------------------------------------------------------------
    # Internal Helpers
    # -----------------------------------------------------------------------

    async def _ensure_mutable_goal(self, goal_id: str) -> Goal:
        goal = await self.get_goal(goal_id)
        if not goal:
            raise ValueError(f"Goal '{goal_id}' not found.")
        if goal.status in TERMINAL_GOAL_STATUSES:
            raise ValueError(f"Cannot mutate goal '{goal_id}' in terminal state '{goal.status}'.")
        return goal

    def _find_matching_goal(self, query: str, goals: List[Goal]) -> Optional[Goal]:
        if not goals:
            return None
        q = query.lower().strip()
        if q in ("that", "it", "this", "the goal", "goal"):
            # Return most recently created non-terminal goal
            active = [g for g in goals if g.status not in TERMINAL_GOAL_STATUSES]
            return active[-1] if active else goals[-1]

        # Exact ID match
        for g in goals:
            if g.goal_id.lower() == q:
                return g

        # Title substring match
        for g in reversed(goals):
            if q in g.title.lower():
                return g

        # Word overlap match
        q_words = set(w for w in q.split() if len(w) > 2)
        if q_words:
            for g in reversed(goals):
                g_words = set(w for w in g.title.lower().split() if len(w) > 2)
                if q_words.intersection(g_words):
                    return g

        return None


# ---------------------------------------------------------------------------
# Goal Runner (Phase 9B)
# ---------------------------------------------------------------------------

class GoalRunner:
    """
    Bounded coordinator for autonomous execution of GoalSteps.
    Coordinates between GoalManager, CortexControlLayer, Capability Registry,
    and Permission Policy.

    Responsibilities:
    - run_goal(goal_id)
    - run_next_step(goal_id)
    - observe_result(goal_id, step_id, raw_result, checkpoint_data)

    Strictly bounded:
    - Does NOT generate plans
    - Does NOT synthesize tools
    - Does NOT rewrite goals
    - Does NOT invent capabilities
    - Stops on failure, blocking, approval, or max steps
    """
    _instance: Optional["GoalRunner"] = None

    def __init__(
        self,
        control_layer: Optional[Any] = None,
        goal_manager: Optional[GoalManager] = None,
    ):
        self._control_layer = control_layer
        self._goal_manager = goal_manager or GoalManager.get_instance()

    @classmethod
    def get_instance(
        cls,
        control_layer: Optional[Any] = None,
        goal_manager: Optional[GoalManager] = None,
    ) -> "GoalRunner":
        if cls._instance is None:
            cls._instance = GoalRunner(control_layer=control_layer, goal_manager=goal_manager)
        elif control_layer is not None:
            cls._instance._control_layer = control_layer
        if goal_manager is not None:
            cls._instance._goal_manager = goal_manager
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton for test isolation."""
        cls._instance = None

    def set_control_layer(self, control_layer: Any) -> None:
        self._control_layer = control_layer

    def get_control_layer(self) -> Optional[Any]:
        if self._control_layer:
            return self._control_layer
        try:
            import hub.main
            if hasattr(hub.main, "_control_layer") and hub.main._control_layer:
                return hub.main._control_layer
        except Exception:
            pass
        return None

    async def run_goal(self, goal_id: str) -> Dict[str, Any]:
        """
        Bounded execution loop for an already-defined goal.
        Executes pending steps one by one, observing results, checkpointing,
        and stopping on complete, fail, block, wait, or approval.
        """
        gm = self._goal_manager
        goal = await gm.get_goal(goal_id)
        if not goal:
            return {"status": "error", "reason": f"Goal '{goal_id}' not found", "stop": True}

        if goal.status in TERMINAL_GOAL_STATUSES:
            return {"status": "terminal", "reason": f"Goal is terminal ({goal.status})", "stop": True}

        # Check global kill switch
        if not await gm.is_autonomy_enabled():
            return {
                "status": "blocked",
                "reason": "Autonomous execution disabled by global kill switch",
                "stop": True,
            }

        # If goal is PENDING, mark ACTIVE; if WAITING with active conditions, do not auto-resume
        if goal.status == "PENDING":
            goal = await gm.mark_active(goal_id)
        elif goal.status == "WAITING":
            if (goal.metadata or {}).get("user_paused") or goal.waiting_reason in ("Paused by user", "Autonomy stopped by user"):
                return {
                    "status": "waiting",
                    "reason": f"Goal is paused by user: {goal.waiting_reason}",
                    "stop": True,
                }
            active_waits = await gm.list_wait_conditions(goal_id=goal_id, status="ACTIVE")
            if active_waits:
                return {
                    "status": "waiting",
                    "reason": f"Goal is waiting on {len(active_waits)} active condition(s)",
                    "stop": True,
                }
            if goal.waiting_reason:
                return {
                    "status": "waiting",
                    "reason": f"Goal is waiting: {goal.waiting_reason}",
                    "stop": True,
                }
            goal = await gm.mark_active(goal_id)
        elif goal.status == "NEEDS_APPROVAL":
            if goal.approval_request_id:
                from .database import get_confirmation
                conf = await get_confirmation(goal.approval_request_id)
                if conf and conf.get("status") == "approved":
                    goal = await gm.mark_active(goal_id)
                elif conf and conf.get("status") in ("denied", "rejected"):
                    await gm.fail_goal(goal_id, error="Goal failed: approval was denied by user")
                    return {"status": "failed", "reason": "Approval denied", "stop": True}
                else:
                    return {"status": "needs_approval", "reason": "Goal is waiting for user approval", "stop": True}
            else:
                goal = await gm.mark_active(goal_id)

        last_step_result = None
        while True:
            cur_goal = await gm.get_goal(goal_id)
            if not cur_goal or cur_goal.status in TERMINAL_GOAL_STATUSES:
                break
            if cur_goal.status in ("BLOCKED", "NEEDS_APPROVAL", "WAITING"):
                break
            # Check global kill switch at loop boundary
            if not await gm.is_autonomy_enabled():
                break
            if cur_goal.current_step_index >= cur_goal.max_steps:
                reason = f"Exceeded maximum allowed steps ({cur_goal.max_steps})"
                await gm.mark_blocked(goal_id, reason=reason)
                break

            step_res = await self.run_next_step(goal_id)
            last_step_result = step_res
            if step_res.get("stop", True):
                break

            # Safe boundary check after step execution
            if not await gm.is_autonomy_enabled():
                break
            cur_goal = await gm.get_goal(goal_id)
            if cur_goal and cur_goal.status in ("BLOCKED", "NEEDS_APPROVAL", "WAITING"):
                break

        final_goal = await gm.get_goal(goal_id)
        return {
            "goal_id": goal_id,
            "status": final_goal.status if final_goal else "UNKNOWN",
            "current_step_index": final_goal.current_step_index if final_goal else 0,
            "failure_count": final_goal.failure_count if final_goal else 0,
            "last_step_result": last_step_result,
        }

    async def run_next_step(self, goal_id: str) -> Dict[str, Any]:
        """
        Execute the single next PENDING step of an active goal.
        
        Core loop:
        Goal ACTIVE
        → get current PENDING step
        → validate capability exists (Capability Registry)
        → check permissions / approval
        → execute through ControlLayer
        → capture physical/result response
        → persist GoalStep result
        → checkpoint Goal
        → update current_step_index
        → continue OR stop
        """
        gm = self._goal_manager
        goal = await gm.get_goal(goal_id)
        if not goal:
            return {"status": "error", "reason": f"Goal '{goal_id}' not found", "stop": True}

        if goal.status in TERMINAL_GOAL_STATUSES:
            return {"status": "terminal", "reason": f"Goal is in terminal state '{goal.status}'", "stop": True}

        if goal.status in ("BLOCKED", "NEEDS_APPROVAL", "WAITING"):
            return {"status": "stopped", "reason": f"Goal is '{goal.status}'", "stop": True}

        # Enforce global kill switch
        if not await gm.is_autonomy_enabled():
            return {
                "status": "blocked",
                "reason": "Autonomous execution disabled by global kill switch",
                "stop": True,
            }

        # Enforce consecutive failures bound
        if goal.consecutive_failures >= goal.max_consecutive_failures:
            reason = f"Exceeded maximum consecutive failures ({goal.max_consecutive_failures})"
            await gm.fail_goal(goal_id, error=reason)
            return {"status": "failed", "reason": reason, "stop": True}

        # Enforce wall-clock runtime limit at safe step boundary
        if goal.max_runtime_seconds is not None and goal.started_at:
            try:
                st_dt = datetime.fromisoformat(goal.started_at)
                elapsed = (datetime.now(timezone.utc) - st_dt).total_seconds()
            except Exception:
                elapsed = 0.0
            elapsed = max(elapsed, float(goal.metadata.get("elapsed_runtime_seconds", 0.0)))
            if elapsed > goal.max_runtime_seconds:
                reason = f"Runtime limit exceeded ({int(elapsed)}s > {goal.max_runtime_seconds}s)"
                await gm.mark_blocked(goal_id, reason=reason)
                return {"status": "blocked", "reason": reason, "stop": True}

        # Enforce max failures bound
        if goal.failure_count >= goal.max_failures:
            reason = f"Exceeded maximum allowed failures ({goal.max_failures})"
            await gm.fail_goal(goal_id, error=reason)
            return {"status": "failed", "reason": reason, "stop": True}

        # Enforce max steps bound
        if goal.current_step_index >= goal.max_steps:
            reason = f"Exceeded maximum allowed steps ({goal.max_steps})"
            await gm.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason, "stop": True}

        if goal.status == "PENDING":
            goal = await gm.mark_active(goal_id)

        steps = await gm.list_steps(goal_id)
        if not steps:
            await gm.complete_goal(goal_id, result_summary="Completed goal (no steps).")
            return {"status": "completed", "stop": True}

        # Locate next pending step
        pending_step = None
        for s in steps:
            if s.index == goal.current_step_index and s.status == "PENDING":
                pending_step = s
                break
        if not pending_step:
            for s in steps:
                if s.status == "PENDING" and s.index >= goal.current_step_index:
                    pending_step = s
                    break
        if not pending_step:
            for s in steps:
                if s.status == "PENDING":
                    pending_step = s
                    break

        if not pending_step:
            running_steps = [s for s in steps if s.status == "RUNNING"]
            if not running_steps:
                await gm.complete_goal(goal_id, result_summary=f"All {len(steps)} steps completed successfully.")
                return {"status": "completed", "stop": True}
            return {"status": "running", "stop": True}

        step = pending_step
        ctrl = self.get_control_layer()
        if not ctrl:
            reason = "ControlLayer unavailable for execution"
            await gm.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason, "stop": True}

        # Resolve target device
        target_device = (
            step.arguments.get("target_device")
            or step.arguments.get("device_id")
            or (step.arguments.get("condition_key") if step.arguments.get("condition_type") == "device_online" else None)
            or goal.metadata.get("target_device")
            or goal.metadata.get("device_id")
        )
        if not target_device:
            if hasattr(ctrl, "connected_devices") and ctrl.connected_devices:
                target_device = list(ctrl.connected_devices.keys())[0]
            else:
                from .database import list_devices
                all_devs = await list_devices()
                target_device = all_devs[0]["device_id"] if all_devs else "default_device"

        source_device_id = goal.metadata.get("source_device") or target_device

        # -------------------------------------------------------------------
        # 1. Capability Validation (Capability Registry)
        # -------------------------------------------------------------------
        if not step.capability or not step.capability.strip():
            reason = "Step has no capability specified"
            await gm.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason, "stop": True}

        cap = step.capability.strip()
        if cap == "wait":
            cap = "goal.wait"
        val_res = await ctrl.validate_target_and_capability(target_device, cap)
        if not val_res.get("valid"):
            reason = f"Capability '{cap}' does not exist on target '{target_device}' (reason: {val_res.get('reason')})"
            await gm.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason, "stop": True}

        # -------------------------------------------------------------------
        # 2. Permission / Policy Check & Approval Flow
        # -------------------------------------------------------------------
        perm = await ctrl.check_permissions(source_device_id, target_device, cap)
        if not perm or not perm.allowed:
            reason = perm.reason if perm else f"Permission denied for '{cap}'"
            await gm.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason, "stop": True}

        # Check if this step was already approved via an approval request
        is_already_approved = False
        if goal.approval_request_id:
            from .database import get_confirmation
            conf = await get_confirmation(goal.approval_request_id)
            if conf and conf.get("status") == "approved":
                is_already_approved = True
                if (goal.metadata or {}).get("is_batch_operation"):
                    goal.metadata["batch_approved"] = True
                    await gm.update_goal(goal)
            elif conf and conf.get("status") in ("denied", "rejected"):
                reason = f"Execution rejected: approval for '{cap}' was denied by user"
                await gm.fail_goal(goal_id, error=reason)
                return {"status": "failed", "reason": reason, "stop": True}
        elif (goal.metadata or {}).get("batch_approved"):
            is_already_approved = True

        if perm.needs_confirmation and not is_already_approved:
            req_args = dict(step.arguments)
            if (goal.metadata or {}).get("is_batch_operation"):
                batch_removes = (goal.metadata or {}).get("intended_remove", [])
                req_args["batch_summary"] = f"Proposed batch deletion of {len(batch_removes)} items: {', '.join(batch_removes)}"

            cmd_id = await ctrl.request_approval(
                target_device=target_device,
                action=cap,
                args=req_args,
                source_device_id=source_device_id,
                tier=perm.tier,
            )
            await gm.mark_needs_approval(goal_id, approval_request_id=cmd_id)
            return {
                "status": "needs_approval",
                "approval_request_id": cmd_id,
                "step_id": step.step_id,
                "stop": True,
            }

        # -------------------------------------------------------------------
        # Phase A6: Task Ownership, Constraints & Lease Acquisition
        # -------------------------------------------------------------------
        from .task_ownership import (
            TaskOwnershipManager,
            ExecutionState,
            ActionSafetyClass,
            classify_action_safety,
        )
        tom = TaskOwnershipManager.get_instance()

        req_caps = [cap]
        req_res: List[str] = []
        req_locs: Dict[str, str] = {}
        movable = True

        for k, v in step.arguments.items():
            if isinstance(v, str):
                if re.match(r'^[a-zA-Z]:[\\/]', v) or v.startswith(("/", "\\")):
                    req_res.append(v)
                    # Resource pinned to target_device
                    req_locs[v] = target_device
                    movable = False

        if step.arguments.get("movable") is False or (goal.metadata or {}).get("movable") is False:
            movable = False

        if step.arguments.get("required_data_locations"):
            req_locs.update(step.arguments["required_data_locations"])
            movable = False

        t_ex = await tom.get_execution_for_step(goal_id, step.step_id)
        if not t_ex:
            t_ex = await tom.create_execution(
                goal_id=goal_id,
                step_id=step.step_id,
                executor_device_id=target_device,
                owner_device_id=(goal.metadata or {}).get("owner_device", "cortex"),
                required_capabilities=req_caps,
                required_resources=req_res,
                required_data_locations=req_locs,
                movable=movable,
            )

        # Check if execution is WAITING_FOR_EXECUTOR
        if t_ex.execution_state == ExecutionState.WAITING_FOR_EXECUTOR:
            pref = t_ex.metadata.get("waiting_for_executor", {}).get("preferred_executor", t_ex.executor_device_id)
            if tom.executor_registry.is_device_online(pref):
                await tom.acquire_lease(t_ex.task_execution_id, pref)
                t_ex.execution_state = ExecutionState.ACTIVE
                target_device = pref
            else:
                return {
                    "status": "waiting",
                    "reason": f"Waiting for executor '{pref}' to become available",
                    "stop": True,
                }

        if cap not in ("goal.wait", "wait"):
            # Check executor liveness and failover if lost
            failover_check = await tom.failover_if_needed(t_ex.task_execution_id)
            if failover_check.get("status") == "waiting_for_executor":
                await gm.mark_waiting(goal_id, reason=failover_check.get("reason", "Waiting for executor"))
                return {"status": "waiting", "reason": failover_check.get("reason"), "stop": True}
            elif failover_check.get("status") == "handed_off":
                target_device = failover_check["new_executor"]

            # Re-check executor liveness
            if not tom.executor_registry.is_device_online(target_device):
                await tom.transition_to_waiting_for_executor(
                    task_execution_id=t_ex.task_execution_id,
                    preferred_executor=target_device,
                    missing_reason=f"Executor '{target_device}' is offline",
                )
                await gm.mark_waiting(goal_id, reason=f"Waiting for executor '{target_device}'")
                return {"status": "waiting", "reason": f"Executor '{target_device}' is offline", "stop": True}

        # Side-effect verification: if re-executing a non-idempotent action
        if t_ex.attempt > 1:
            safety_cls = classify_action_safety(cap)
            if safety_cls == ActionSafetyClass.VERIFICATION_REQUIRED:
                ver_res = await tom.verify_action_outcome(cap, step.arguments, target_device)
                if ver_res.get("already_completed"):
                    return await self.observe_result(
                        goal_id=goal_id,
                        step_id=step.step_id,
                        raw_result={
                            "status": "success",
                            "message": f"Action '{cap}' verified as already completed; skipping duplicate execution.",
                        },
                    )
                elif not ver_res.get("verified", True) or ver_res.get("status") == "uncertain":
                    reason = f"Side-effect verification uncertain for non-idempotent action '{cap}'. Halting to avoid duplicate execution."
                    await gm.mark_blocked(goal_id, reason=reason)
                    return {"status": "blocked", "reason": reason, "stop": True}

        # -------------------------------------------------------------------
        # 3. Execution through ControlLayer
        # -------------------------------------------------------------------
        await gm.start_step(goal_id, step.step_id)

        raw_result = await self._execute_via_control_layer(
            ctrl=ctrl,
            target_device=target_device,
            capability=cap,
            arguments=step.arguments,
        )

        # -------------------------------------------------------------------
        # Phase A6: Fencing Token Validation & Checkpoint Persistence
        # -------------------------------------------------------------------
        is_valid_token, token_err = tom.validate_fencing_token(
            t_ex.task_execution_id,
            lease_id=t_ex.lease_id,
            generation=t_ex.generation,
        )
        if not is_valid_token:
            log.error(f"[GoalRunner] Fencing token validation failed: {token_err}")
            return {"status": "rejected", "reason": token_err, "stop": True}

        await tom.save_checkpoint(
            task_execution_id=t_ex.task_execution_id,
            completed_sub_actions=[{"step_id": step.step_id, "capability": cap, "result": raw_result}],
            structured_outputs={"last_result": raw_result},
            side_effect_state=classify_action_safety(cap),
        )

        # -------------------------------------------------------------------
        # 4. Observation & Checkpoint Persistence
        # -------------------------------------------------------------------
        obs_res = await self.observe_result(
            goal_id=goal_id,
            step_id=step.step_id,
            raw_result=raw_result,
        )

        return obs_res


    async def _execute_via_control_layer(
        self,
        ctrl: Any,
        target_device: str,
        capability: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Dispatches execution through CortexControlLayer and captures response.
        """
        if capability in ("goal.wait", "wait"):
            if hasattr(ctrl, "execute_and_observe"):
                return await ctrl.execute_and_observe(target_device, capability, arguments, is_autonomous=True)
            gm = self._goal_manager
            cond_type = arguments.get("condition_type", "world_state")
            cond_key = arguments.get("condition_key") or arguments.get("device_id") or arguments.get("key", "")
            op = arguments.get("operator", "==")
            exp_val = arguments.get("expected_value", "")
            is_satisfied = await gm.is_condition_satisfied(
                condition_type=cond_type,
                condition_key=cond_key,
                operator=op,
                expected_value=exp_val,
                metadata=arguments,
            )
            if is_satisfied:
                return {
                    "status": "success",
                    "message": f"Condition '{cond_type}:{cond_key}' satisfied",
                    "data": {"condition_type": cond_type, "condition_key": cond_key},
                }
            else:
                return {
                    "status": "waiting",
                    "message": arguments.get("reason") or f"Waiting for {cond_type} ({cond_key})",
                    "condition": {
                        "condition_type": cond_type,
                        "condition_key": cond_key,
                        "operator": op,
                        "expected_value": exp_val,
                        "metadata": arguments,
                    },
                }

        if hasattr(ctrl, "execute_and_observe"):
            return await ctrl.execute_and_observe(target_device, capability, arguments, is_autonomous=True)

        import uuid
        import asyncio
        from .main import PENDING_FUTURES

        cmd_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        PENDING_FUTURES[cmd_id] = fut

        try:
            await ctrl.execute_command(target_device, capability, arguments, cmd_id=cmd_id, is_autonomous=True)
            res = await asyncio.wait_for(fut, timeout=15.0)
            if hasattr(res, "model_dump"):
                return res.model_dump()
            elif hasattr(res, "to_dict"):
                return res.to_dict()
            elif isinstance(res, dict):
                return res
            return {
                "status": getattr(res, "status", "success"),
                "message": getattr(res, "message", ""),
                "command": getattr(res, "command", capability),
                "command_id": getattr(res, "command_id", cmd_id),
                "data": getattr(res, "data", None),
            }
        except asyncio.TimeoutError:
            return {"status": "failure", "message": "Execution timed out waiting for device response"}
        except Exception as e:
            return {"status": "failure", "message": f"Execution error: {e}"}
        finally:
            PENDING_FUTURES.pop(cmd_id, None)

    async def observe_result(
        self,
        goal_id: str,
        step_id: str,
        raw_result: Any,
        checkpoint_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Observe actual result response, persist result, error, completion status,
        and checkpoint data. Update step and goal.
        """
        gm = self._goal_manager
        step = await gm.get_step(step_id)
        if not step:
            raise ValueError(f"Step '{step_id}' not found for goal '{goal_id}'.")

        if isinstance(raw_result, dict):
            status = raw_result.get("status", "success")
            msg = raw_result.get("message", "")
            err = raw_result.get("error")
            data = raw_result.get("data")
        elif hasattr(raw_result, "status"):
            status = getattr(raw_result, "status", "success")
            msg = getattr(raw_result, "message", "")
            err = getattr(raw_result, "error", None)
            data = getattr(raw_result, "data", None)
        else:
            status = "success"
            msg = str(raw_result)
            err = None
            data = None

        now = datetime.now(timezone.utc).isoformat()

        if status == "waiting":
            # Phase 9C: Step deliberately enters WAITING state
            wait_info = {}
            if isinstance(raw_result, dict):
                wait_info = raw_result.get("condition") or raw_result.get("wait_condition") or {}
            cond_type = wait_info.get("condition_type", "world_state")
            cond_key = wait_info.get("condition_key") or wait_info.get("device_id") or wait_info.get("key", "")
            op = wait_info.get("operator", "==")
            exp_val = wait_info.get("expected_value", "")
            meta = wait_info.get("metadata") or {}

            wait_cond = await gm.create_wait_condition(
                goal_id=goal_id,
                step_id=step.step_id,
                condition_type=cond_type,
                condition_key=cond_key,
                operator=op,
                expected_value=exp_val,
                metadata=meta,
            )

            waiting_reason = msg or f"Waiting for {cond_type} ({cond_key})"
            step.status = "WAITING"
            step.checkpoint_data.update({
                "wait_id": wait_cond.wait_id,
                "waiting_for": f"{cond_type}:{cond_key}",
                "waiting_at": now,
            })
            if checkpoint_data:
                step.checkpoint_data.update(checkpoint_data)
            await gm.update_step(step)

            goal = await gm.get_goal(goal_id)
            if goal:
                goal.status = "WAITING"
                goal.waiting_reason = waiting_reason
                goal.checkpoint_data.update({
                    "wait_id": wait_cond.wait_id,
                    "last_step_index": step.index,
                })
                if checkpoint_data:
                    goal.checkpoint_data.update(checkpoint_data)
                await gm.update_goal(goal)

            return {
                "status": "waiting",
                "wait_id": wait_cond.wait_id,
                "goal_id": goal_id,
                "step_id": step.step_id,
                "reason": waiting_reason,
                "stop": True,
            }

        is_success = (status == "success")

        # Post-action verification for destructive filesystem actions (fs.delete)
        if is_success and step.capability in ("fs.delete", "delete_file"):
            from .environment import fs_stat
            from pathlib import Path
            target_path = step.arguments.get("path")
            stat_res = fs_stat(target_path) if target_path else {"exists": False}
            if stat_res.get("exists", False):
                is_success = False
                item_name = Path(target_path).name if target_path else "item"
                err = f"Post-action verification failed: item '{item_name}' still exists at {target_path}."
                status = "failure"
                msg = err

        if is_success:
            res_dict = {"status": "success", "message": msg, "data": data}
            step_ckpt = {
                "last_step_index": step.index,
                "step_id": step.step_id,
                "capability": step.capability,
                "status": "COMPLETED",
                "completed_at": now,
            }
            if checkpoint_data:
                step_ckpt.update(checkpoint_data)

            # Persist checkpoint immediately
            await gm.checkpoint(goal_id, step_ckpt, step_id=step.step_id)
            # Mark step COMPLETED and increment current_step_index
            await gm.complete_step(goal_id, step.step_id, result=res_dict)

            goal = await gm.get_goal(goal_id)
            if goal and goal.approval_request_id:
                goal.approval_request_id = None
                await gm.update_goal(goal)

            # Phase 9G / 9H: Grounded Target Resolution & Collection Classification
            if step.capability == "fs.list" and goal:
                from .target_resolver import TargetResolver, match_filesystem_items
                from pathlib import Path
                target_info = (goal.metadata or {}).get("resolved_target")
                if not target_info and goal.original_request:
                    dev_id = step.arguments.get("target_device") or goal.metadata.get("target_device") or "asus"
                    res_target = TargetResolver.resolve(goal.original_request, current_device=dev_id)
                    if res_target.action in ("delete", "move", "cleanup") and res_target.target_name:
                        target_info = res_target.to_dict()

                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("items", [])
                elif isinstance(raw_result, dict):
                    items = raw_result.get("items") or (raw_result.get("data") or {}).get("items", [])

                # -------------------------------------------------------------
                # Case 1: Post-Action Verification (Phase 9H)
                # -------------------------------------------------------------
                if (goal.metadata or {}).get("awaiting_verification"):
                    present_names = set()
                    for it in items:
                        if isinstance(it, dict):
                            present_names.add(it.get("name", ""))
                        elif isinstance(it, str):
                            present_names.add(Path(it).name)

                    intended_keep = set((goal.metadata or {}).get("intended_keep", []))
                    intended_remove = set((goal.metadata or {}).get("intended_remove", []))
                    initial_items = set((goal.metadata or {}).get("initial_items", []))

                    missing_keep = intended_keep - present_names
                    leftover_remove = intended_remove.intersection(present_names)
                    unexpected_missing = (initial_items - intended_remove) - present_names

                    if missing_keep:
                        reason = f"Post-action verification failed: intended keep item(s) missing: {', '.join(sorted(missing_keep))}"
                        await gm.fail_goal(goal_id, error=reason)
                        return {
                            "status": "failed",
                            "step_id": step.step_id,
                            "step_status": "COMPLETED",
                            "reason": reason,
                            "result": res_dict,
                            "stop": True,
                        }

                    if leftover_remove:
                        reason = f"Post-action verification failed: intended remove item(s) still present: {', '.join(sorted(leftover_remove))}"
                        await gm.fail_goal(goal_id, error=reason)
                        return {
                            "status": "failed",
                            "step_id": step.step_id,
                            "step_status": "COMPLETED",
                            "reason": reason,
                            "result": res_dict,
                            "stop": True,
                        }

                    if unexpected_missing:
                        reason = f"Post-action verification failed: unexpected item(s) deleted: {', '.join(sorted(unexpected_missing))}"
                        await gm.fail_goal(goal_id, error=reason)
                        return {
                            "status": "failed",
                            "step_id": step.step_id,
                            "step_status": "COMPLETED",
                            "reason": reason,
                            "result": res_dict,
                            "stop": True,
                        }

                    # Verified successfully!
                    meta = dict(goal.metadata or {})
                    meta["awaiting_verification"] = False
                    meta["verification_status"] = "verified"
                    meta["batch_approved"] = False
                    goal.metadata = meta
                    await gm.update_goal(goal)
                    await gm.complete_goal(
                        goal_id,
                        result_summary=f"Batch cleanup verified successfully. All {len(intended_keep)} kept items intact and {len(intended_remove)} removed items deleted."
                    )
                    return {
                        "status": "completed",
                        "step_id": step.step_id,
                        "step_status": "COMPLETED",
                        "result": res_dict,
                        "stop": True,
                    }

                # -------------------------------------------------------------
                # Case 2: Semantic Collection Classification & Batch Planning (Phase 9H)
                # -------------------------------------------------------------
                if target_info and (target_info.get("action") == "cleanup" or target_info.get("target_kind") == "collection"):
                    from .collection_classifier import CollectionClassifier
                    loc = target_info.get("location") or step.arguments.get("path") or "desktop"
                    c_res = CollectionClassifier.classify(
                        user_goal=goal.original_request,
                        items=items,
                        location=loc,
                    )
                    tgt_dev = step.arguments.get("target_device") or (goal.metadata or {}).get("target_device") or "asus"

                    # Ambiguity guard: If unsure items exist, prompt clarification and do not delete
                    if c_res.unsure:
                        unsure_names = [it.name for it in c_res.unsure]
                        reason = f"Ambiguous items require clarification before cleanup: {', '.join(unsure_names)}"
                        await gm.mark_blocked(goal_id, reason=reason)
                        return {
                            "status": "blocked",
                            "step_id": step.step_id,
                            "step_status": "COMPLETED",
                            "reason": reason,
                            "result": res_dict,
                            "stop": True,
                        }

                    # If no items require removal
                    if not c_res.remove:
                        await gm.complete_goal(
                            goal_id,
                            result_summary=f"Collection inspection complete. No items need removal. Kept {len(c_res.keep)} items."
                        )
                        return {
                            "status": "completed",
                            "step_id": step.step_id,
                            "step_status": "COMPLETED",
                            "result": res_dict,
                            "stop": True,
                        }

                    # Build batch delete plan and verification step
                    meta = dict(goal.metadata or {})
                    meta["classification_result"] = c_res.to_dict()
                    meta["intended_keep"] = [it.name for it in c_res.keep]
                    meta["intended_remove"] = [it.name for it in c_res.remove]
                    meta["initial_items"] = [it.get("name") if isinstance(it, dict) else Path(str(it)).name for it in items]
                    meta["is_batch_operation"] = True
                    meta["awaiting_verification"] = True
                    goal.metadata = meta
                    await gm.update_goal(goal)

                    for it in c_res.remove:
                        await gm.add_step(
                            goal_id=goal_id,
                            description=f"Delete '{it.name}'",
                            capability="fs.delete",
                            arguments={
                                "path": it.path,
                                "target_device": tgt_dev,
                            },
                        )

                    # Append post-action verification step
                    await gm.add_step(
                        goal_id=goal_id,
                        description="Verify collection final state",
                        capability="fs.list",
                        arguments={
                            "path": step.arguments.get("path", loc),
                            "target_device": tgt_dev,
                        },
                    )

                    return {
                        "status": "continued",
                        "step_id": step.step_id,
                        "step_status": "COMPLETED",
                        "result": res_dict,
                        "stop": False,
                    }

                # -------------------------------------------------------------
                # Case 3: Grounded Single-Target Match (Phase 9G)
                # -------------------------------------------------------------
                if target_info and target_info.get("action") == "delete" and target_info.get("target_name"):

                    match_res = match_filesystem_items(
                        target_name=target_info["target_name"],
                        items=items,
                        qualifiers=target_info.get("qualifiers", []),
                    )

                    if match_res.is_ambiguous:
                        reason = match_res.clarification_message or f"Ambiguous matches for '{target_info['target_name']}'"
                        await gm.mark_blocked(goal_id, reason=reason)
                        return {
                            "status": "blocked",
                            "step_id": step.step_id,
                            "step_status": "COMPLETED",
                            "reason": reason,
                            "result": res_dict,
                            "stop": True,
                        }
                    elif not match_res.matched_item:
                        reason = f"No matching item found for '{target_info['target_name']}' in {target_info.get('location', 'directory')}"
                        await gm.fail_goal(goal_id, error=reason)
                        return {
                            "status": "failed",
                            "step_id": step.step_id,
                            "step_status": "COMPLETED",
                            "reason": reason,
                            "result": res_dict,
                            "stop": True,
                        }
                    else:
                        matched = match_res.matched_item
                        tgt_dev = step.arguments.get("target_device") or goal.metadata.get("target_device") or "asus"
                        grounded_dict = {
                            "target_name": target_info.get("target_name"),
                            "matched_name": matched["name"],
                            "matched_path": matched["path"],
                            "path": matched["path"],
                            "location": target_info.get("location", "desktop"),
                            "target_kind": "filesystem_item",
                            "target_device": tgt_dev,
                        }
                        meta = dict(goal.metadata or {})
                        meta["grounded_target"] = grounded_dict
                        meta["matched_item"] = matched
                        goal.metadata = meta
                        await gm.update_goal(goal)

                        try:
                            from .session_context import SessionContextManager
                            await SessionContextManager.set_grounded_target(tgt_dev, grounded_dict)
                        except Exception:
                            pass

                        await gm.add_step(
                            goal_id=goal_id,
                            description=f"Delete '{matched['name']}'",
                            capability="fs.delete",
                            arguments={
                                "path": matched["path"],
                                "target_device": tgt_dev,
                            },
                        )
                        return {
                            "status": "continued",
                            "step_id": step.step_id,
                            "step_status": "COMPLETED",
                            "result": res_dict,
                            "stop": False,
                        }

            # Check if another PENDING step exists
            steps = await gm.list_steps(goal_id)
            remaining_pending = [s for s in steps if s.status == "PENDING"]
            if not remaining_pending:
                gr_target = (goal.metadata or {}).get("grounded_target")
                if gr_target and gr_target.get("matched_name"):
                    res_summary = f"Successfully deleted '{gr_target['matched_name']}' from {gr_target.get('location', 'desktop')}."
                else:
                    res_summary = f"All {len(steps)} steps completed successfully."
                await gm.complete_goal(goal_id, result_summary=res_summary)
                return {
                    "status": "completed",
                    "step_id": step.step_id,
                    "step_status": "COMPLETED",
                    "result": res_dict,
                    "stop": True,
                }
            return {
                "status": "continued",
                "step_id": step.step_id,
                "step_status": "COMPLETED",
                "result": res_dict,
                "stop": False,
            }
        else:
            err_msg = err or msg or "Capability execution reported failure"
            res_dict = {"status": "failure", "message": msg, "error": err_msg, "data": data}
            fail_ckpt = {
                "failed_step_index": step.index,
                "step_id": step.step_id,
                "capability": step.capability,
                "status": "FAILED",
                "error": err_msg,
                "failed_at": now,
            }
            if checkpoint_data:
                fail_ckpt.update(checkpoint_data)

            # Persist checkpoint immediately
            await gm.checkpoint(goal_id, fail_ckpt, step_id=step.step_id)
            # Mark step FAILED and increment failure_count (enforces max_failures)
            await gm.fail_step(goal_id, step.step_id, error=err_msg)
            if step.capability in ("fs.delete", "delete_file"):
                await gm.fail_goal(goal_id, error=err_msg)

            return {
                "status": "failed",
                "step_id": step.step_id,
                "step_status": "FAILED",
                "error": err_msg,
                "result": res_dict,
                "stop": True,
            }


# Re-export Phase 9E Planning components
from .planner import GoalPlanner, CapabilitySchema, BUILTIN_CAPABILITY_SCHEMAS
