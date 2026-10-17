"""
hub/pipeline/deterministic_router.py
------------------------------------
Safety-critical deterministic router (Section 1).
Only handles:
- Pending confirmation/approval resolution (yes/no/approve/deny)
- Kill switch / emergency stop
- Wake/sleep controls
- Exact ground-truth self-knowledge (SystemRegistry)
- Unambiguous factual people queries (PeopleManager)
"""

import logging
import os
import re
from typing import Any, Dict, Optional

from ..approval_resolver import resolve_approval_intent
from ..database import get_pending_confirmations, resolve_confirmation
from ..models import ConversationalResponse
from ..system_registry import SystemRegistry
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
            AgentTurn.cancel_for_device(source_device_id)
            return ConversationalResponse(status="success", message="Stopped. An action already sent may still finish.")
        if clean in ("stop all actions", "emergency stop", "kill switch", "abort all tasks", "cancel all"):
            from ..task_orchestrator import TaskOrchestrator
            from ..goals import GoalManager
            from ..agent_turn import AgentTurn
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

        # -------------------------------------------------------------------
        # Non-safety phrase routing is available only in explicit legacy mode.
        if os.environ.get("CORTEX_LEGACY_COMPATIBILITY", "0") != "1":
            return None
        log.info("legacy_fallback reason=explicit_compatibility stage=non_safety_router")

        # 3. Exact Ground-Truth Self-Knowledge (SystemRegistry)
        # -------------------------------------------------------------------
        is_devices_query = bool(re.search(
            r"\b(?:what devices are connected|which devices are connected|list connected devices|show connected devices|what devices do you have|what devices are online|which devices are online)\b",
            clean,
        ))
        if is_devices_query:
            sys_reg = SystemRegistry.get_instance()
            conn_map = getattr(control_layer, "connected_devices", {}) if control_layer else {}
            devices = await sys_reg.list_devices(current_node=source_device_id, connected_devices_map=conn_map)
            online_devs = [d for d in devices if d.get("online")]
            dev_names = ", ".join([f"{d['name']} ({'online' if d.get('online') else 'offline'}, role: {d.get('role')})" for d in devices])
            msg = f"Connected devices ({len(online_devs)} online): {dev_names}."
            return ConversationalResponse(
                status="success",
                message=msg,
                data={"devices": devices},
            )

        if re.search(r"\b(?:can you (?:check|get|see|tell me)(?: the)? weather|do you have weather|are you able to check weather)\b", clean):
            from ..weather_provider import get_weather_provider
            wp = get_weather_provider()
            is_avail = bool(wp and getattr(wp, "is_available", lambda: True)())
            if is_avail:
                return ConversationalResponse(
                    status="success",
                    message="Yes, I can check live weather conditions and forecasts.",
                    data={"capabilities": {"weather.current": {"available": True}}},
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message="No, weather access is currently unavailable because the weather provider is disabled.",
                    data={"capabilities": {"weather.current": {"available": False}}},
                )

        # Capabilities List Query
        if re.search(r"\b(?:what can you (?:actually )?do|what are your capabilities|what capabilities do you have|what actions can you perform|list (?:your )?capabilities)\b", clean):
            sys_reg = SystemRegistry.get_instance()
            caps = sys_reg.list_capabilities()
            active_caps = [c["name"] for c in caps if c.get("availability")]
            msg = (
                "I can perform live web searches, fetch live weather reports, open browser URLs, "
                "capture and visually analyze your screen, launch and gracefully close desktop applications, "
                "switch and focus windows, and reliably perform verified file operations (open, copy, move, rename, delete)."
            )
            return ConversationalResponse(
                status="success",
                message=msg,
                data={"capabilities": caps, "active_count": len(active_caps)},
            )

        # Integration Queries
        if re.search(r"\b(?:do you have (?:access to (?:my )?)?calendar|can you access (?:my )?calendar|is calendar connected)\b", clean):
            sys_reg = SystemRegistry.get_instance()
            c_stat = await sys_reg.get_integration_status("calendar")
            if c_stat.get("connected"):
                return ConversationalResponse(
                    status="success",
                    message="Yes, Google Calendar is connected and authorized.",
                    data=c_stat,
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message="No, Google Calendar access is not currently connected or authorized.",
                    data=c_stat,
                )

        if re.search(r"\b(?:do you have (?:access to )?gmail|do you have gmail access|do you have (?:access to (?:my )?)?email|can you read (?:my )?email|is gmail connected|do you have gmail)\b", clean):
            sys_reg = SystemRegistry.get_instance()
            g_stat = await sys_reg.get_integration_status("gmail")
            if g_stat.get("connected"):
                return ConversationalResponse(
                    status="success",
                    message="Yes, Gmail is connected and authorized.",
                    data=g_stat,
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message="No, Gmail access is not currently connected or authorized.",
                    data=g_stat,
                )

        # Task Ownership & Privacy Self-Knowledge Queries (Phase A6 & A7)
        try:
            from ..task_ownership import TaskOwnershipManager
            tom = TaskOwnershipManager.get_instance()
            tom_res = await tom.answer_self_knowledge_query(clean)
            if tom_res and tom_res.get("matched"):
                return ConversationalResponse(
                    status="success",
                    message=tom_res.get("answer", ""),
                    data=tom_res.get("execution"),
                )
        except Exception:
            pass

        # File Transfer Self-Knowledge Queries (Phase A8)
        try:
            is_transfer_sk = bool(re.search(
                r"\b(?:where is (?:my|the|this) file|is (?:it|the file|the transfer) still transferring|how much is (?:left|remaining)|why did the transfer pause|which transport (?:are you|is) using|can (?:this|the transfer) resume|did lenovo receive (?:it|the file)|cancel (?:that|the) transfer|resume (?:that|the) transfer)\b",
                clean,
            ))
            if is_transfer_sk:
                from ..file_transfer import FileTransferManager
                ftm = FileTransferManager.get_instance()
                ft_ans = await ftm.answer_self_knowledge_query(clean)
                if ft_ans and ft_ans.get("matched"):
                    return ConversationalResponse(
                        status="success",
                        message=ft_ans["answer"],
                        data=ft_ans.get("session") or ft_ans.get("locality"),
                    )
        except Exception:
            pass

        # Device Capability Discovery across nodes ("Which devices can capture my screen?")
        m_dev_cap = re.search(
            r"\b(?:which|what)\s+devices\s+(?:can|are\s+able\s+to)\s+(capture\s+(?:my\s+)?screen|take\s+(?:a\s+)?screenshots?|open\s+apps?|launch\s+apps?)\b",
            clean,
        )
        if m_dev_cap:
            act = m_dev_cap.group(1).lower()
            cap = "screen.capture" if ("screen" in act or "screenshot" in act) else "app.launch"
            from ..executor_registry import ExecutorRegistry
            er = ExecutorRegistry.get_instance()
            capable_online = er.get_capable_devices(cap, online_only=True)
            if capable_online:
                names = ", ".join([d.display_name or d.device_id for d in capable_online])
                msg = f"The following online devices can {act}: {names}."
            else:
                msg = f"No online devices currently support {act}."
            return ConversationalResponse(
                status="success",
                message=msg,
                data={"capable_devices": [d.to_dict() for d in capable_online]},
            )

        # Device Capability Inquiry ("Can my Lenovo open apps?")
        m_can_dev = re.search(
            r"\bcan\s+(?:my\s+)?([a-zA-Z0-9_-]+)\s+(open\s+apps?|launch\s+apps?|take\s+(?:a\s+)?screenshots?|capture\s+(?:my\s+)?screen)\b",
            clean,
        )
        if m_can_dev:
            target_name = m_can_dev.group(1).lower()
            act = m_can_dev.group(2).lower()
            cap = "screen.capture" if ("screen" in act or "screenshot" in act) else "app.launch"
            from ..executor_registry import ExecutorRegistry
            er = ExecutorRegistry.get_instance()
            target_handle = None
            for h in er.list_device_handles():
                if h.device_id.lower() == target_name or h.display_name.lower() == target_name or target_name in h.display_name.lower():
                    target_handle = h
                    break
            if target_handle and target_handle.supports_capability(cap):
                return ConversationalResponse(
                    status="success",
                    message=f"Yes, {target_handle.display_name} can {act}.",
                )
            elif target_handle:
                return ConversationalResponse(
                    status="success",
                    message=f"No, {target_handle.display_name} does not support {act}.",
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message=f"Device '{target_name}' is not recognized in Cortex.",
                )

        # Device Limitation Inquiry ("Why can't you run this on my phone?")
        m_why_dev = re.search(
            r"\bwhy\s+can(?:['’]t| not)\s+you\s+(?:run|do)\s+(?:this|that)\s+on\s+(?:my\s+)?([a-zA-Z0-9_-]+)\b",
            clean,
        )
        if m_why_dev:
            target_name = m_why_dev.group(1).lower()
            from ..executor_registry import ExecutorRegistry
            er = ExecutorRegistry.get_instance()
            target_handle = None
            for h in er.list_device_handles():
                if h.device_id.lower() == target_name or h.display_name.lower() == target_name or target_name in h.display_name.lower():
                    target_handle = h
                    break
            if target_handle:
                if not er.is_device_online(target_handle.device_id):
                    return ConversationalResponse(
                        status="success",
                        message=f"I cannot run this on {target_handle.display_name} because it is currently offline.",
                    )
                if target_handle.trust_level == "restricted":
                    return ConversationalResponse(
                        status="success",
                        message=f"I cannot run this on {target_handle.display_name} because it has restricted permissions.",
                    )
                return ConversationalResponse(
                    status="success",
                    message=f"I cannot run this on {target_handle.display_name} due to missing capabilities or trust restrictions.",
                )

        # Default Device Query ("Which device would run this?")
        if re.search(r"\bwhich\s+device\s+would\s+run\s+(?:this|that)\b", clean):
            return ConversationalResponse(
                status="success",
                message=f"The current primary device ({source_device_id}) would run this.",
            )

        # Limitation & Capability Explanations
        m_explain = re.search(r"\bwhy can(?:['’]t| not) you\s+(.+)", clean)
        if m_explain:
            sys_reg = SystemRegistry.get_instance()
            target_task = m_explain.group(1).strip()
            exp = await sys_reg.explain_capability(target_task)
            return ConversationalResponse(
                status="success",
                message=exp.get("explanation", ""),
                data=exp,
            )

        reg_resp = await SystemRegistry.answer_query(clean, context)
        if reg_resp:
            return ConversationalResponse(
                status="success",
                message=reg_resp,
            )

        # -------------------------------------------------------------------
        # 4. Factual People Query
        # -------------------------------------------------------------------
        try:
            from ..people import PeopleManager
            if hasattr(PeopleManager, "try_answer_people_question"):
                people_resp = await PeopleManager.try_answer_people_question(clean, context)
                if people_resp:
                    return ConversationalResponse(
                        status="success",
                        message=people_resp,
                    )
        except Exception:
            pass

        return None
