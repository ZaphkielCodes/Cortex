"""
hub/pipeline/pipeline.py
------------------------
RequestPipeline orchestrating all decoupled request processing stages (Section 1).
"""

import logging
import os
from typing import Any, Dict, Optional

from ..models import ConversationalResponse
from .capability_resolver import CapabilityResolver
from .context_assembler import ContextAssembler
from .deterministic_router import DeterministicSafetyRouter
from .execution_dispatcher import ExecutionDispatcher
from .normalizer import InputNormalizer
from .planner_adapter import PipelinePlanner
from .policy_gate import PolicyGate
from .response_composer import ResponseComposer
from .result_observer import ResultObserver
from .semantic_interpreter import SemanticInterpreter
from .target_grounder import TargetGrounder

log = logging.getLogger("cortex.pipeline")


class RequestPipeline:
    """
    Decoupled request processing pipeline.
    Replaces monolithic regex trees with modular stages:
    InputNormalizer -> ContextAssembler -> DeterministicSafetyRouter ->
    SemanticInterpreter -> CapabilityResolver -> TargetGrounder ->
    Planner -> PolicyGate -> ExecutionDispatcher -> ResultObserver -> ResponseComposer
    """

    def __init__(self, control_layer: Any, model_router: Optional[Any] = None):
        self.control = control_layer
        self.router = model_router

    async def execute(
        self,
        source_device_id: str,
        natural_request: str,
        context: Dict[str, Any],
        request_id: str = "unknown",
    ) -> ConversationalResponse:
        log.info(f"[RequestPipeline] Executing request '{natural_request}' from {source_device_id}")

        # Stage 1: Input Normalization
        norm = InputNormalizer.normalize(natural_request)

        # Phase A9: Emit safe operational activity event (no chain-of-thought)
        try:
            from ..api_control import emit_activity
            emit_activity("Understanding request", category="planning", device_id=source_device_id)
        except Exception:
            pass

        # Stage 3: Deterministic Safety & Registry Routing
        det_response = await DeterministicSafetyRouter.route(
            norm=norm,
            source_device_id=source_device_id,
            context=context,
            control_layer=self.control,
        )
        if det_response is not None:
            return det_response

        # Model-first is authoritative. Compatibility is opt-in and never retries
        # an exception after an action might already have executed.
        compatibility = os.environ.get("CORTEX_LEGACY_COMPATIBILITY", "0") == "1"
        provider = getattr(self.router, "provider", self.router)
        mock_compatibility = compatibility and getattr(provider, "name", None) == "mock"
        if self.router is not None and not mock_compatibility:
            from ..agent_turn import AgentTurn
            try:
                result = await AgentTurn(self.control, self.router).run(
                    natural_request, source_device_id, context, request_id)
                return ConversationalResponse(status=result.status, message=result.message,
                    data=result.data, command_id=result.command_id,
                    provenance=result.provenance, latency_metrics=result.latency_metrics)
            except Exception as exc:
                log.error("agent_runtime_failed request_id=%s reason=%s legacy_fallback=false",
                          request_id, type(exc).__name__)
                return ConversationalResponse(status="error", message="The agent runtime could not complete that request.")
        if not compatibility:
            return ConversationalResponse(status="error", message="The reasoning provider is unavailable.")
        reason = "explicit_mock_compatibility" if mock_compatibility else "model_router_unconfigured"
        log.warning("legacy_fallback request_id=%s reason=%s stage=semantic_pipeline", request_id, reason)
        assembled_ctx = await ContextAssembler.assemble(source_device_id, context)
        assembled_ctx.raw_context["natural_request"] = natural_request

        # Stage 4: General Semantic Interpretation (legacy fallback for compatibility)
        intent = await SemanticInterpreter.interpret(
            norm=norm,
            context=assembled_ctx,
            model_router=self.router,
        )

        # Handle pure conversational grounding (e.g. "So I'm working on you?")
        if intent.type == "CONVERSATION" or intent.is_conversational_followup:
            return ConversationalResponse(
                status="success",
                message=intent.response_text or "Yes, that's correct.",
            )

        target_device = assembled_ctx.source_device

        # Stage 5: Target Grounding (Decoupled from intent interpretation)
        grounded = None
        if intent.candidate_capabilities or intent.target_reference:
            grounded = TargetGrounder.ground(intent, assembled_ctx)
            if grounded.target_device and grounded.target_device != "current":
                target_device = grounded.target_device

            if grounded.is_ambiguous and grounded.clarification_reason:
                return ConversationalResponse(
                    status="ambiguous",
                    message=grounded.clarification_reason,
                )

        # Stage 6: Capability Resolution & Truthful Limitation Reporting
        if intent.missing_capability:
            return ConversationalResponse(
                status="unsupported",
                message=intent.response_text or f"I don't have a capability for {intent.missing_capability}.",
                data={"genuine_capability_gap": True},
            )

        if not intent.candidate_capabilities and intent.type != "GOAL":
            return None

        cap_plan = CapabilityResolver.resolve_capability(intent, target_device)
        if not cap_plan.is_supported:
            if cap_plan.capability and not cap_plan.capability.availability:
                if cap_plan.capability.name.startswith("web.search"):
                    return ConversationalResponse(
                        status="error",
                        message="Live search provider is unavailable. I cannot search the web right now.",
                        data={"provider_unavailable": True},
                    )
                elif cap_plan.capability.name.startswith("weather."):
                    return ConversationalResponse(
                        status="error",
                        message="Live weather provider is unavailable. I cannot check current weather right now.",
                        data={"provider_unavailable": True},
                    )
            return ConversationalResponse(
                status="unsupported",
                message=intent.response_text or cap_plan.missing_reason or "I don't have a capability to perform that task.",
                data={"genuine_capability_gap": True},
            )

        # Stage 6b: Deterministic Executor Selection & Target Device Validation
        from ..executor_selector import ExecutorSelector
        target_cap = intent.candidate_capabilities[0] if intent.candidate_capabilities else None
        if target_cap:
            selector = ExecutorSelector()
            req_device = None
            if grounded and grounded.target_device and grounded.target_device not in ("current", assembled_ctx.source_device):
                req_device = grounded.target_device
            elif intent.arguments.get("target_device") and intent.arguments["target_device"] not in ("current", assembled_ctx.source_device):
                req_device = intent.arguments["target_device"]

            owning_dev = None
            if grounded and grounded.owning_device:
                owning_dev = grounded.owning_device

            sel = selector.select_executor(
                required_capability=target_cap,
                requested_device=req_device,
                source_device=assembled_ctx.source_device,
                owning_device=owning_dev,
            )
            if not sel.is_success and req_device:
                # When explicit device was requested, never fallback silently to ASUS
                if sel.status in ("capability_mismatch", "device_offline"):
                    return ConversationalResponse(
                        status="error",
                        message=sel.reason,
                        data={"selection_status": sel.status},
                    )
                elif sel.status == "trust_denied":
                    return ConversationalResponse(
                        status="denied",
                        message=sel.reason,
                        data={"selection_status": sel.status},
                    )
            elif sel.is_success:
                target_device = sel.selected_device
                intent.arguments["target_device"] = target_device

        # -------------------------------------------------------------------
        # Stage 6c: Contextual Computer-Use & Assistant Grounding Queries
        # -------------------------------------------------------------------
        if intent.required_effect == "switch_previous_app":
            from ..world_state import WorldStateManager
            from agent.desktop_actions import launch_app
            ws = WorldStateManager.get_instance()
            prev = ws.get_previous_foreground()
            prev_app = (prev or {}).get("app_name")
            if prev_app and prev_app.lower() not in ("unknown", "system"):
                launch_app({"app": prev_app})
                return ConversationalResponse(
                    status="success",
                    message=f"Switched back to '{prev_app}'.",
                    data={"previous_app": prev_app},
                )
            else:
                from agent.ui_action_executor import UIActionExecutor
                UIActionExecutor.get_instance().hotkey({"key": "alt+tab"})
                return ConversationalResponse(
                    status="success",
                    message="Switched back to your previous application window.",
                )

        if intent.required_effect == "grounded_screen_perception":
            from agent.ui_observer import UIObserver
            obs = await UIObserver.get_instance().observe(target_device=target_device)
            app_name = obs.foreground_app or "an application"
            title = obs.window_title or ""
            msg = f"You are currently in {app_name}"
            if title and title.lower() != app_name.lower():
                msg += f" with window '{title}' open"
            if obs.elements:
                msg += f". I can see {len(obs.elements)} interactive controls on your screen."
            else:
                msg += "."
            return ConversationalResponse(
                status="success",
                message=msg,
                data={"app": app_name, "title": title, "elements_count": len(obs.elements)},
            )

        if intent.required_effect == "check_last_task_status":
            from ..computer_use_controller import ComputerUseTaskController
            cu_ctrl = ComputerUseTaskController.get_instance()
            active_tasks = cu_ctrl.get_active_tasks()
            if active_tasks:
                last_task = active_tasks[-1]
                if last_task.state == "COMPLETED" or last_task.verification_state.get("verified"):
                    v_summary = last_task.verification_state.get("summary") or f"Successfully verified: {last_task.goal}"
                    return ConversationalResponse(
                        status="success",
                        message=f"Yes, {v_summary}",
                        data=last_task.to_dict(),
                    )
                else:
                    return ConversationalResponse(
                        status="success",
                        message=f"The last task '{last_task.goal}' is currently {last_task.state}.",
                        data=last_task.to_dict(),
                    )
            return ConversationalResponse(status="success", message="No recent computer-use actions to verify.")

        if intent.required_effect == "user_correction":
            intended = intent.arguments.get("intended_app")
            rejected = intent.arguments.get("rejected_app")
            from agent.desktop_actions import launch_app, close_app
            if rejected:
                try:
                    close_app({"app": rejected})
                except Exception:
                    pass
            if intended:
                launch_app({"app": intended})
                return ConversationalResponse(
                    status="success",
                    message=f"Understood. Switched to '{intended}' instead.",
                    data={"intended_app": intended, "rejected_app": rejected},
                )

        # Stage 7: Multi-step Goal Planning if needed
        if intent.type == "GOAL":
            plan = await PipelinePlanner.plan_goal(intent, assembled_ctx, self.control)
            if not plan.get("steps"):
                return ConversationalResponse(
                    status="unsupported",
                    message=plan.get("unachievable_reason", "Unable to construct an achievable plan for this goal."),
                )
            # Create persistent goal in GoalManager
            from ..goals import GoalManager, GoalRunner
            from ..planner import GoalPlanner
            gm = GoalManager.get_instance()
            planner = GoalPlanner.get_instance()
            created_goal = await gm.create_goal(
                title=intent.goal,
                original_request=intent.goal,
                metadata={"target_device": target_device, "source_device": source_device_id},
            )
            await planner.persist_plan(goal_id=created_goal.goal_id, plan=plan)

            # Link with ComputerUseTaskController if computer_use execution mode
            is_computer_use = (
                intent.arguments.get("execution_mode") == "computer_use"
                or intent.required_effect in ("desktop_state_change", "compound_app_computer_use")
                or any(s.get("capability", "").startswith("ui.") for s in plan.get("steps", []))
            )
            if is_computer_use:
                from ..computer_use_controller import ComputerUseTaskController
                cu_ctrl = ComputerUseTaskController.get_instance()
                cu_task = cu_ctrl.create_task(
                    goal=intent.goal,
                    executor_device=target_device,
                    initial_steps=plan.get("steps", []),
                    success_condition=intent.arguments.get("success_condition", f"desired state achieved: {intent.goal}"),
                    execution_mode="computer_use",
                )
                # Run the genuine iterative agentic computer-use loop!
                loop_res = await cu_ctrl.run_agent_loop(cu_task, control_layer=self.control,
                    model_router=self.router, source_device_id=source_device_id, context=context)
                if loop_res.get("status") == "completed":
                    await gm.complete_goal(created_goal.goal_id, result_summary=loop_res.get("message"))
                    return ConversationalResponse(
                        status="success",
                        message=loop_res.get("message", f"Goal '{intent.goal}' completed successfully."),
                        data=loop_res,
                    )
                elif loop_res.get("status") == "waiting_for_approval":
                    await gm.mark_needs_approval(created_goal.goal_id, approval_request_id=cu_task.task_id)
                    return ConversationalResponse(
                        status="needs_approval",
                        message=loop_res.get("message", "This action requires user approval."),
                        data=loop_res,
                    )
                elif loop_res.get("status") in ("failure", "verification_failed", "blocked"):
                    return ConversationalResponse(
                        status="error",
                        message=loop_res.get("message", f"Computer-use action failed for '{intent.goal}'."),
                        data=loop_res,
                    )
                else:
                    msg = loop_res.get("message", f"Computer-use task running: {cu_task.goal}")
                    return ConversationalResponse(status="in_progress", message=msg, data=loop_res)

            runner = GoalRunner.get_instance(control_layer=self.control, goal_manager=gm)
            run_res = await runner.run_goal(created_goal.goal_id)
            if run_res.get("status") == "COMPLETED":
                status = "success"
            elif run_res.get("status") == "NEEDS_APPROVAL":
                status = "needs_approval"
            elif run_res.get("status") in ("ACTIVE", "RUNNING"):
                status = "success"
            else:
                status = "error"
            msg = created_goal.result_summary or run_res.get("message") or f"Goal execution finished with status {run_res.get('status')}."
            return ConversationalResponse(status=status, message=msg, data=run_res)

        # Stage 8: Authoritative Policy Gate (Permissions, Privacy, Safety)
        policy_res = await PolicyGate.evaluate(intent, target_device, assembled_ctx, self.control)
        if not policy_res.allowed:
            return ConversationalResponse(
                status="denied",
                message=policy_res.denial_reason or "Action was denied by policy.",
            )
        if policy_res.requires_approval:
            return ConversationalResponse(
                status="needs_approval",
                message=policy_res.approval_prompt or "This action requires confirmation.",
            )

        # Stage 9: Execution Dispatching
        try:
            from ..api_control import emit_activity
            action_label = intent.goal or (intent.candidate_capabilities[0] if intent.candidate_capabilities else "action")
            emit_activity(f"Executing {action_label} on {target_device}", category="execution", device_id=target_device)
        except Exception:
            pass

        raw_result = await ExecutionDispatcher.dispatch(
            intent=intent,
            target_device=target_device,
            control_layer=self.control,
            request_id=request_id,
        )

        # Stage 10: Result Observation & Context Tracking
        ResultObserver.observe(intent, raw_result, assembled_ctx)

        try:
            from ..api_control import emit_activity
            emit_activity("Action execution completed", category="execution", status="completed", device_id=target_device)
        except Exception:
            pass

        # Stage 11: Response Composition
        return ResponseComposer.compose(intent, raw_result, grounded)
