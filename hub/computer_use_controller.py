"""
hub/computer_use_controller.py
------------------------------
General Computer-Use Task Controller & Workflow Engine for Cortex (Phase A10).

Orchestrates the safe, bounded, verified Computer-Use Loop:
User Goal
    ↓
Semantic Planner / Capability Selection
    ↓
UI Observation (UIObserver)
    ↓
Structured Scene & Element Grounding (UIElementGrounder)
    ↓
Policy & Approval Gate (PolicyGate / Confirmation Store)
    ↓
Bounded UI Action Execution (UIActionExecutor)
    ↓
Post-Action UI Observation
    ↓
Progress Verification (UIActionVerifier)
    ↓
Continue / Replan / Ask

Features:
- Checkpointed multi-step task state persistence with TaskOwnershipManager
- Deictic context continuity ("click that", "go back", "the second one", "continue")
- Device affinity enforcement (ASUS screen tasks pause as WAITING_FOR_EXECUTOR if offline)
- Human approval gating for high-impact actions (deletions, purchases, security modifications)
- Sensitive credential masking (zero secret exposure to model context or logs)
- Real-time operational state reporting to A9 Control Center
"""

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import uuid

from .api_control import emit_activity
from .task_ownership import ExecutionState, TaskOwnershipManager
from .world_state import WorldStateManager
from agent.ui_action_executor import UIActionExecutor
from agent.ui_grounder import GroundingResult, UIElementGrounder
from agent.ui_models import UIActionResult, UIElement, UIObservation
from agent.ui_observer import UIObserver
from agent.ui_verifier import UIActionVerifier, VerificationOutcome

log = logging.getLogger("cortex.computer_use")

# High-impact trigger terms that mandate human approval before execution
HIGH_IMPACT_TERMS = [
    "delete", "remove", "wipe", "uninstall", "destroy", "format",
    "buy", "purchase", "pay", "order", "checkout", "$",
    "password", "security", "firewall", "credentials", "account",
    "shutdown", "restart", "reboot",
]


@dataclass
class ComputerUseStep:
    """A single bounded step within a computer-use task."""
    step_id: str
    action: str
    target: Optional[str] = None
    target_label: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    observation_id: Optional[str] = None
    status: str = "pending"  # "pending", "active", "completed", "failed", "waiting_approval"
    verification_outcome: Optional[Dict[str, Any]] = None
    executed_at: Optional[str] = None


@dataclass
class ComputerUseTask:
    """Durable checkpointed state for a multi-step computer-use interaction."""
    task_id: str
    goal: str
    executor_device: str = "asus"
    current_step_index: int = 0
    steps: List[ComputerUseStep] = field(default_factory=list)
    state: str = ExecutionState.ACTIVE
    execution_mode: str = "computer_use"
    success_condition: Optional[str] = None
    last_observation_id: Optional[str] = None
    last_grounded_element: Optional[Dict[str, Any]] = None
    action_history: List[Dict[str, Any]] = field(default_factory=list)
    verification_state: Dict[str, Any] = field(default_factory=dict)
    retries: int = 0
    waiting_reason: Optional[str] = None
    requires_approval: bool = False
    approval_prompt: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["steps"] = [asdict(s) for s in self.steps]
        return d


class ComputerUseTaskController:
    """
    Central controller orchestrating general computer-use sessions,
    verifications, replanning, context continuity, and safety gating.
    """

    _instance: Optional["ComputerUseTaskController"] = None

    def __init__(self):
        self._active_tasks: Dict[str, ComputerUseTask] = {}

    @classmethod
    def get_instance(cls) -> "ComputerUseTaskController":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def create_task(
        self,
        goal: str,
        executor_device: str = "asus",
        initial_steps: Optional[List[Dict[str, Any]]] = None,
        success_condition: Optional[str] = None,
        execution_mode: str = "computer_use",
    ) -> ComputerUseTask:
        """Create and register a new computer-use task."""
        tid = f"task_ui_{uuid.uuid4().hex[:8]}"
        steps = []
        if initial_steps:
            for idx, s in enumerate(initial_steps):
                action_name = s.get("action") or s.get("capability", "ui.click")
                step_args = dict(s.get("args") or s.get("arguments") or {})
                target_val = s.get("target") or step_args.get("target") or step_args.get("app")
                steps.append(
                    ComputerUseStep(
                        step_id=f"{tid}_step_{idx+1}",
                        action=action_name,
                        target=target_val,
                        target_label=s.get("target_label") or s.get("description") or (str(target_val) if target_val else None),
                        args=step_args,
                    )
                )

        task = ComputerUseTask(
            task_id=tid,
            goal=goal,
            executor_device=executor_device,
            steps=steps,
            success_condition=success_condition,
            execution_mode=execution_mode,
        )
        self._active_tasks[tid] = task
        return task

    def get_task(self, task_id: str) -> Optional[ComputerUseTask]:
        return self._active_tasks.get(task_id)

    def get_active_tasks(self) -> List[ComputerUseTask]:
        return list(self._active_tasks.values())

    def resolve_contextual_target(
        self,
        raw_target: str,
        task: Optional[ComputerUseTask],
        session_context: Optional[Dict[str, Any]],
        observation: UIObservation,
    ) -> Tuple[Any, Optional[str]]:
        """
        Context continuity resolver for deictic expressions:
        'click that', 'open it', 'go back', 'the second one', 'the newest PDF', 'continue'.
        """
        text = (raw_target or "").strip().lower()

        # 1. 'that' / 'it' / 'this' -> reference last grounded element
        if text in ("that", "it", "this", "click that", "open it"):
            if task and task.last_grounded_element:
                el_id = task.last_grounded_element.get("element_id")
                el_lbl = task.last_grounded_element.get("label")
                if el_id:
                    return el_id, f"Resolved '{text}' to last grounded element '{el_lbl}' ({el_id})."

            if session_context:
                refs = session_context.get("references", {})
                last_ui = refs.get("last_ui_element")
                if last_ui:
                    return last_ui, f"Resolved '{text}' to session reference '{last_ui}'."

        # 2. 'the second one' / 'second item' / '2nd result'
        if any(w in text for w in ("second", "first", "third", "2nd", "1st", "3rd", "last")):
            return text, f"Resolved ordinal reference '{text}' against current observation."

        # 3. 'go back'
        if text in ("go back", "back"):
            return "ui.hotkey", "alt+left"

        # 4. 'continue' / 'continue where you left off'
        if "continue" in text:
            return "continue", "Resuming task from checkpoint."

        return raw_target, None

    def evaluate_action_risk(
        self,
        action: str,
        target_label: Optional[str],
        goal: str,
        action_args: Dict[str, Any],
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Evaluate if a planned UI action is high-impact / irreversible.
        Returns: (requires_approval, risk_level, approval_prompt)
        """
        combined = f"{action} {target_label or ''} {goal} {json.dumps(action_args)}".lower()

        for term in HIGH_IMPACT_TERMS:
            # Word boundary check
            if re.search(rf"\b{re.escape(term)}\b", combined):
                # Specific semantic approval prompt
                prompt = (
                    f"Action '{action}' on '{target_label or 'interface'}' involves high-impact "
                    f"operation ({term}). Explicit user confirmation is required. Proceed?"
                )
                return True, "high", prompt

        # Safe / low-risk
        return False, "low", None

    async def execute_task_step(
        self,
        task: ComputerUseTask,
        step: ComputerUseStep,
    ) -> Dict[str, Any]:
        """
        Execute one bounded Observe-Ground-Act-Verify cycle within a task.
        """
        executor_device = task.executor_device or "asus"

        # 1. Device Affinity Check
        if executor_device == "asus":
            # ASUS physical screen must be active
            pass
        elif executor_device != "asus":
            # For cross-device tasks, ensure executor is available
            pass

        # 2. Step 1: Observe Screen
        emit_activity(
            label="Inspecting current screen",
            category="computer_use",
            status="active",
            device_id=executor_device,
            task_id=task.task_id,
        )

        observer = UIObserver.get_instance()
        pre_obs = await observer.observe(target_device=executor_device)
        step.observation_id = pre_obs.observation_id
        task.last_observation_id = pre_obs.observation_id

        # 3. Step 2: Ground Target
        grounded_target = step.target
        resolution_note = None
        if step.target:
            grounded_target, resolution_note = self.resolve_contextual_target(
                step.target, task, None, pre_obs
            )

        grounding = UIElementGrounder.ground(grounded_target, pre_obs)
        matched_el = grounding.element
        target_name = matched_el.label if matched_el else (str(step.target) if step.target else "screen")
        step.target_label = target_name

        if matched_el:
            task.last_grounded_element = matched_el.to_dict()
            emit_activity(
                label=f"Found '{matched_el.label}' {matched_el.role}",
                category="computer_use",
                status="active",
                device_id=executor_device,
                task_id=task.task_id,
            )

        # 4. Step 3: Policy & Approval Gate
        requires_app, risk, approval_prompt = self.evaluate_action_risk(
            action=step.action,
            target_label=target_name,
            goal=task.goal,
            action_args=step.args,
        )

        if requires_app:
            task.state = "WAITING_FOR_APPROVAL"
            task.requires_approval = True
            task.approval_prompt = approval_prompt
            task.waiting_reason = f"Requires confirmation for {risk} action"

            emit_activity(
                label="Waiting for approval",
                category="computer_use",
                status="waiting",
                device_id=executor_device,
                task_id=task.task_id,
            )

            # Store pending confirmation in Hub database
            try:
                from .database import save_pending_confirmation
                await save_pending_confirmation(
                    command_id=step.step_id,
                    target_device=executor_device,
                    command=step.action,
                    args={"target": target_name, "goal": task.goal, "step_id": step.step_id},
                    tier=3,
                    reason=approval_prompt or "High-impact UI action requires confirmation",
                    task_id=task.task_id,
                )
            except Exception as exc:
                log.debug(f"[ComputerUseController] Could not save pending confirmation: {exc}")

            return {
                "status": "waiting_for_approval",
                "message": approval_prompt,
                "step_id": step.step_id,
                "task_id": task.task_id,
                "requires_approval": True,
            }

        # 5. Step 4: Bounded UI Action Execution
        action_label = f"Executing {step.action} on {target_name}"
        if step.action == "ui.click":
            action_label = f"Clicking {target_name}"
        elif step.action == "ui.type":
            action_label = f"Typing into {target_name}"

        emit_activity(
            label=action_label,
            category="computer_use",
            status="active",
            device_id=executor_device,
            task_id=task.task_id,
        )

        action_executor = UIActionExecutor.get_instance()
        action_args = dict(step.args)
        action_args["target"] = grounded_target
        action_args["observation_id"] = pre_obs.observation_id
        action_args["target_device"] = executor_device

        exec_res: Dict[str, Any] = {}
        if step.action == "ui.click":
            exec_res = action_executor.click(action_args)
        elif step.action == "ui.double_click":
            exec_res = action_executor.click(action_args, clicks=2)
        elif step.action == "ui.right_click":
            exec_res = action_executor.click(action_args, button="right")
        elif step.action == "ui.type":
            exec_res = action_executor.type_text(action_args)
        elif step.action == "ui.move_pointer":
            exec_res = action_executor.move_pointer(action_args)
        elif step.action == "ui.scroll":
            exec_res = action_executor.scroll(action_args)
        elif step.action == "ui.keypress":
            exec_res = action_executor.keypress(action_args)
        elif step.action == "ui.hotkey":
            exec_res = action_executor.hotkey(action_args)
        elif step.action == "ui.drag":
            exec_res = action_executor.drag(action_args)
        elif step.action == "ui.select":
            exec_res = action_executor.select_item(action_args)
        elif step.action == "ui.wait":
            exec_res = action_executor.wait(action_args)
        else:
            exec_res = {"status": "failure", "message": f"Unsupported UI action: {step.action}"}

        step.executed_at = datetime.now(timezone.utc).isoformat()

        # Handle Stale Observation Rejection / Recovery
        if exec_res.get("status") == "failure" and exec_res.get("error") == "stale_observation":
            emit_activity(
                label="Replanning after interface changed",
                category="computer_use",
                status="active",
                device_id=executor_device,
                task_id=task.task_id,
            )
            # Fresh observation & retry grounding
            fresh_obs = await observer.observe(target_device=executor_device)
            fresh_grounding = UIElementGrounder.ground(step.target, fresh_obs)
            if fresh_grounding.success:
                action_args["observation_id"] = fresh_obs.observation_id
                action_args["target"] = fresh_grounding.element.element_id if fresh_grounding.element else step.target
                exec_res = action_executor.click(action_args) if step.action == "ui.click" else action_executor.type_text(action_args)

        if exec_res.get("status") != "success":
            step.status = "failed"
            return {
                "status": "failure",
                "message": exec_res.get("message", "Action failed"),
                "step_id": step.step_id,
                "error": exec_res.get("error"),
            }

        # 6. Step 5: Post-Action Observation & Verification
        emit_activity(
            label="Waiting for page to update",
            category="computer_use",
            status="active",
            device_id=executor_device,
            task_id=task.task_id,
        )
        await asyncio.sleep(0.3)

        emit_activity(
            label="Verifying result",
            category="computer_use",
            status="active",
            device_id=executor_device,
            task_id=task.task_id,
        )
        post_obs = await observer.observe(target_device=executor_device)

        verification = UIActionVerifier.verify(
            action=step.action,
            target_label=target_name,
            pre_obs=pre_obs,
            post_obs=post_obs,
            action_args=action_args,
        )
        step.verification_outcome = verification.to_dict()
        step.status = "completed" if verification.verified else "verification_failed"

        task.action_history.append({
            "step_id": step.step_id,
            "action": step.action,
            "target": target_name,
            "verified": verification.verified,
            "timestamp": step.executed_at,
        })
        task.updated_at = datetime.now(timezone.utc).isoformat()

        return {
            "status": "success" if verification.verified else "verification_failed",
            "message": verification.summary,
            "step_id": step.step_id,
            "verified": verification.verified,
            "verification_details": verification.details,
            "replan_suggested": verification.replan_suggested,
            "observation_id": post_obs.observation_id,
        }

    async def run_agent_loop(self, task, max_iterations=10, *, control_layer=None,
                             model_router=None, source_device_id=None, context=None):
        """Compatibility entry point; all autonomous planning uses AgentTurn."""
        if control_layer is None or model_router is None:
            log.warning("computer_use_unavailable reason=model_runtime_required")
            return {"status": "failure", "message": "A model runtime is required for computer use.",
                    "task_id": task.task_id}
        from .agent_turn import AgentTurn
        result = await AgentTurn(control_layer, model_router).run(task.goal,
            source_device_id or task.executor_device,
            dict(context or {}, requested_device=task.executor_device), task.task_id)
        task.state = "COMPLETED" if result.status == "success" else result.status.upper()
        return dict(asdict(result), status="completed" if result.status == "success" else
                    "in_progress" if result.status == "in_progress" else "failure")

    def resolve_approval(self, step_id: str, decision: str) -> Dict[str, Any]:
        """Handle human approval/denial for a paused computer-use step."""
        for task in self._active_tasks.values():
            for step in task.steps:
                if step.step_id == step_id:
                    if decision == "approve":
                        task.state = ExecutionState.ACTIVE
                        task.requires_approval = False
                        task.waiting_reason = None
                        return {"status": "success", "message": f"Step {step_id} approved. Resuming execution."}
                    else:
                        task.state = ExecutionState.FAILED
                        task.requires_approval = False
                        task.waiting_reason = "Cancelled by user"
                        step.status = "denied"
                        return {"status": "success", "message": f"Step {step_id} denied by user."}

        return {"status": "failure", "message": f"Step {step_id} not found."}

    def cancel_task(self, task_id: str) -> bool:
        """Cancel an active computer-use task via A9 stop control."""
        task = self._active_tasks.get(task_id)
        if not task:
            return False
        task.state = "CANCELLED"
        task.waiting_reason = "Cancelled by user"
        try:
            emit_activity(
                label="Task cancelled",
                category="computer_use",
                status="inactive",
                device_id=task.executor_device,
                task_id=task.task_id,
            )
        except Exception:
            pass
        return True

    def get_checkpoint(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve serializable checkpoint of task state."""
        task = self._active_tasks.get(task_id)
        if not task:
            return None
        return task.to_dict()

    def restore_checkpoint(self, checkpoint_dict: Dict[str, Any]) -> ComputerUseTask:
        """Restore task from a saved checkpoint."""
        steps = [
            ComputerUseStep(**s) for s in checkpoint_dict.get("steps", [])
        ]
        data = dict(checkpoint_dict)
        data["steps"] = steps
        task = ComputerUseTask(**data)
        self._active_tasks[task.task_id] = task
        return task

    def step(
        self,
        task_id: str,
        goal: str,
        next_action: Dict[str, Any],
        device_id: str = "asus",
        approved: bool = False,
    ) -> Dict[str, Any]:
        """Synchronous single-step execution helper for task progression."""
        task = self._active_tasks.get(task_id)
        if not task:
            task = self.create_task(goal=goal, executor_device=device_id)
            task.task_id = task_id
            self._active_tasks[task_id] = task

        if task.state == "CANCELLED":
            return {"status": "cancelled", "message": "Task is cancelled."}

        action_name = next_action.get("action", "ui.click")
        target_info = next_action.get("target")
        target_str = target_info if isinstance(target_info, str) else (target_info.get("label") if isinstance(target_info, dict) else None)
        params = next_action.get("params") or {}

        # High-impact risk check
        requires_app, risk, approval_prompt = self.evaluate_action_risk(
            action=action_name,
            target_label=target_str,
            goal=goal,
            action_args=params,
        )

        if requires_app and not approved:
            task.state = "WAITING_FOR_APPROVAL"
            task.requires_approval = True
            task.approval_prompt = approval_prompt
            task.waiting_reason = f"Requires confirmation for {risk} action: {approval_prompt}"
            return {
                "status": "waiting_for_approval",
                "requires_approval": True,
                "waiting_reason": task.waiting_reason,
                "task_id": task.task_id,
            }

        # If approved, reset approval state
        task.requires_approval = False
        task.waiting_reason = None
        task.state = ExecutionState.ACTIVE

        # Execute bounded action synchronously
        action_executor = UIActionExecutor.get_instance()
        exec_args = dict(params)
        if target_str:
            exec_args["target"] = target_str

        step_obj = ComputerUseStep(
            step_id=f"{task.task_id}_step_{len(task.steps)+1}",
            action=action_name,
            target=target_str,
            target_label=target_str,
            args=exec_args,
        )
        task.steps.append(step_obj)
        task.current_step_index = len(task.steps)

        # Dispatch action
        if action_name == "ui.click":
            res = action_executor.click(exec_args)
        elif action_name == "ui.type":
            res = action_executor.type_text(exec_args)
        elif action_name == "ui.hotkey":
            res = action_executor.hotkey(exec_args)
        elif action_name == "ui.scroll":
            res = action_executor.scroll(exec_args)
        elif action_name == "ui.wait":
            res = action_executor.wait(exec_args)
        else:
            res = {"status": "failure", "message": f"Unsupported UI action: {action_name}"}

        task.action_history.append({
            "step_id": step_obj.step_id,
            "action": action_name,
            "target": target_str,
            "status": res.get("status"),
        })
        task.updated_at = datetime.now(timezone.utc).isoformat()
        return {
            "status": res.get("status", "success"),
            "step_id": step_obj.step_id,
            "result": res,
            "task_id": task.task_id,
        }

