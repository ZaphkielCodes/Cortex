"""
hub/pipeline/deterministic_router.py
------------------------------------
Safety-critical deterministic router (Section 1).
Only handles:
- Pending confirmation/approval resolution (yes/no/approve/deny)
- Kill switch / emergency stop
"""

import logging
from typing import Any, Dict, Optional

from ..approval_resolver import resolve_approval_intent
from ..database import get_pending_confirmations, resolve_confirmation
from ..models import ConversationalResponse
from .normalizer import NormalizedInput

log = logging.getLogger("cortex.pipeline.deterministic_router")


class DeterministicSafetyRouter:
    """
    Evaluates safety-critical and ground-truth deterministic routes.
    Returns ConversationalResponse if handled, None otherwise.
    """

    @classmethod
    async def route(
        cls,
        norm: NormalizedInput,
        source_device_id: str,
        context: Dict[str, Any],
        control_layer: Any,
    ) -> Optional[ConversationalResponse]:
        text = norm.stripped_text
        clean = norm.cleaned_text

        # -------------------------------------------------------------------
        # 1. Active Pending Approval / Confirmation Resolution
        # -------------------------------------------------------------------
        pending_confs = await get_pending_confirmations()
        if pending_confs:
            matching_conf = next(
                (c for c in reversed(pending_confs) if c.get("requested_by") == source_device_id),
                None
            )
            approval_decision = resolve_approval_intent(clean)
            if approval_decision and matching_conf:
                cmd_id = matching_conf["command_id"]
                if matching_conf.get("command") == "model.share":
                    from ..main import _execute_resolve_confirmation
                    allowed, message = await _execute_resolve_confirmation(cmd_id, approval_decision, source_device_id)
                    if not allowed:
                        return ConversationalResponse(status="error", message=message, command_id=cmd_id)
                else:
                    await resolve_confirmation(cmd_id, approval_decision, source_device_id)
                from ..main import PENDING_FUTURES
                fut = PENDING_FUTURES.get(cmd_id)
                if fut and not fut.done():
                    fut.set_result(approval_decision)

                if approval_decision == "approved":
                    # Check goals waiting for approval
                    from ..goals import GoalManager, GoalRunner
                    gm = GoalManager.get_instance()
                    goals = await gm.list_goals(status="NEEDS_APPROVAL")
                    matching_goal = next((g for g in goals if g.approval_request_id == cmd_id), None)
                    if matching_goal:
                        await gm.mark_active(matching_goal.goal_id)
                        runner = GoalRunner.get_instance(control_layer=control_layer, goal_manager=gm)
                        run_res = await runner.run_goal(matching_goal.goal_id)
                        if run_res.get("status") == "COMPLETED":
                            msg = f"Approved. {matching_goal.result_summary or f'Goal {matching_goal.title} completed successfully.'}"
                            status = "success"
                        elif run_res.get("status") == "FAILED":
                            err_reason = run_res.get("error") or run_res.get("reason") or matching_goal.blocking_reason or "Verification failed"
                            msg = f"Approved, but execution failed: {err_reason}"
                            status = "error"
                        else:
                            msg = "Approved and execution resumed."
                            status = "success"
                        return ConversationalResponse(status=status, message=msg, command_id=cmd_id)

                    return ConversationalResponse(
                        status="success",
                        message="Approved. Proceeding with execution.",
                        command_id=cmd_id,
                    )
                else:
                    from ..goals import GoalManager
                    gm = GoalManager.get_instance()
                    goals = await gm.list_goals(status="NEEDS_APPROVAL")
                    matching_goal = next((g for g in goals if g.approval_request_id == cmd_id), None)
                    if matching_goal:
                        await gm.cancel_goal(matching_goal.goal_id, reason="Denied by user")
                    return ConversationalResponse(
                        status="cancelled",
                        message="Cancelled. The requested action was denied and will not be performed.",
                        command_id=cmd_id,
                    )

        # -------------------------------------------------------------------
        # 2. Kill Switch / Emergency Stop
        # -------------------------------------------------------------------
        if clean in ("stop", "cortex stop", "stop cortex", "stop listening"):
            from ..agent_turn import AgentTurn
            from ..request_lifecycle import RequestLifecycle
            RequestLifecycle.cancel_for_device(source_device_id)
            AgentTurn.cancel_for_device(source_device_id)
            return ConversationalResponse(status="success", message="Stopped. An action already sent may still finish.")
        if clean in ("stop all actions", "emergency stop", "kill switch", "abort all tasks", "cancel all"):
            from ..task_orchestrator import TaskOrchestrator
            from ..goals import GoalManager
            from ..agent_turn import AgentTurn
            from ..request_lifecycle import RequestLifecycle
            for job in list(RequestLifecycle._jobs.values()):
                RequestLifecycle.cancel_for_device(job.device)
            for device in list(AgentTurn._devices):
                AgentTurn.cancel_for_device(device)
            TaskOrchestrator().cancel_task_for_device(source_device_id)
            # Cancel active goals
            gm = GoalManager.get_instance()
            active_goals = await gm.list_goals(status="ACTIVE")
            for g in active_goals:
                await gm.cancel_goal(g.goal_id, reason="User triggered emergency stop")
            return ConversationalResponse(
                status="success",
                message="Emergency stop acknowledged. All active tasks and goals have been cancelled.",
            )

        return None
