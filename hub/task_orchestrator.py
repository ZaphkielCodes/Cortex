import logging
import time
import json
from uuid import uuid4
from typing import Dict, Any, List, Optional, Literal
from pydantic import BaseModel, Field

from .intent_schema import ActionPlan, ModelResult
from .control_layer import CortexControlLayer
from .model_router import ModelRouter

log = logging.getLogger("cortex.task_orchestrator")

class TaskState(BaseModel):
    task_id: str = Field(default_factory=lambda: uuid4().hex)
    original_user_goal: str
    source_device: str
    current_step: int = 0
    completed_steps: List[dict] = Field(default_factory=list)
    latest_action_result: Optional[dict] = None
    status: Literal["RUNNING", "WAITING", "COMPLETED", "FAILED", "CANCELLED"] = "RUNNING"
    waiting_reason: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    max_steps: int = 10

# Global state for Phase 6 per-device task serialization
_active_tasks: Dict[str, TaskState] = {}
_device_active_task: Dict[str, str] = {}
_background_tasks: Dict[str, "asyncio.Task"] = {}

class TaskOrchestrator:
    def __init__(self, router: Any = None, control_layer: Optional[CortexControlLayer] = None):
        if control_layer is None and isinstance(router, CortexControlLayer):
            self.control = router
            self.router = None
        else:
            self.router = router
            self.control = control_layer
        self.active_tasks = _active_tasks
        self.device_active_task = _device_active_task
        self.background_tasks = _background_tasks
        
    def cancel_task_for_device(self, device_id: str):
        active_id = self.device_active_task.get(device_id)
        if active_id:
            st = self.active_tasks.get(active_id)
            if st and st.status in ("RUNNING", "WAITING"):
                st.status = "CANCELLED"
                st.updated_at = time.time()
                log.info(f"Task cancelled | task_id={st.task_id} | source_device={device_id}")
                import asyncio
                asyncio.create_task(self._persist_task_state(st))
                
    async def _persist_task_state(self, state: TaskState):
        from .database import add_task, update_task, get_task
        task_data = {
            "id": state.task_id,
            "title": state.original_user_goal[:50],
            "type": "orchestration",
            "status": state.status,
            "assigned_device": state.source_device,
            "command": "orchestrate",
            "context": state.model_dump(),
        }
        existing = await get_task(state.task_id)
        if existing:
            await update_task(state.task_id, {"status": state.status, "context": task_data["context"]})
        else:
            await add_task(task_data)
            
    async def reload_active_tasks(self):
        from .database import get_tasks
        tasks = await get_tasks()
        count = 0
        for t in tasks:
            if t.get('type') == 'orchestration_worker':
                if t['status'] in {'PENDING', 'RUNNING', 'WAITING'}:
                    from .orchestration import recover_worker
                    await recover_worker(t)
                continue
            if t.get('type') == 'orchestration':
                if t['status'] in {'RUNNING', 'WAITING'}:
                    from .orchestration import recover_graph
                    await recover_graph(t)
                continue
            if t.get("type") in {"agent_turn", "request"}:
                from .session_context import SessionContextManager
                try:
                    saved = json.loads(t.get("context") or "{}") if isinstance(t.get("context"), str) else dict(t.get("context") or {})
                    if not isinstance(saved, dict):
                        saved = {}
                except (ValueError, TypeError):
                    saved = {}
                result = saved.get("result")
                if t["status"] == "RUNNING" or (saved.get("waiting_device") and not result):
                    from .database import update_task
                    message = "Hub restarted. Completion is unverified; automatic action replay is disabled."
                    result = {"status": "error", "message": message, "task_id": t["id"],
                        "data": {"task_id": t["id"], "error_code": "hub_restart_unverified"}}
                    compound = saved.get("compound_ui")
                    if isinstance(compound,dict):
                        compound.update(current_stage="FAILED",task_status="FAILED",fresh_required=True,
                            blocked_reason="hub_restart_unverified",verification_result={"verified":False,"reason":"restart_unverified"})
                        receipt=compound.get("execution_receipt") or {}
                        if receipt.get("status")=="in_flight":
                            receipt.update(ran="unverified",retry_safe=False,verification_required=True)
                    await update_task(t["id"], {"status": "WAITING", "last_error": message,
                        "context": dict(saved, result=result, runtime="agent_turn")})
                if saved.get("parent_request_id"):
                    continue  # The outer request owns session/delivery reconciliation.
                if (t.get("assigned_device") and isinstance(result, dict) and
                        result.get("status") not in (None, "in_progress") and
                        isinstance(result.get("message"), str) and result.get("task_id") in (None, t["id"])):
                    session = await SessionContextManager.get_session(t["assigned_device"])
                    previous = next((turn for turn in session["turns"] if turn.get("task_id") == t["id"]), {})
                    if t["status"] == "RUNNING" or previous.get("response_status") == "in_progress":
                        request = previous.get("request") or saved.get("request") or t.get("title", "Interrupted request")
                        await SessionContextManager.update_session(t["assigned_device"], request, dict(result, task_id=t["id"]))
                continue
            if t["status"] in ("RUNNING", "WAITING"):
                context_data = json.loads(t["context"])
                state = TaskState(**context_data)
                self.active_tasks[state.task_id] = state
                self.device_active_task[state.source_device] = state.task_id
                count += 1
                log.info(f"[TaskOrchestrator] Reloaded task {state.task_id} in state {state.status}")
                # We do not automatically start RUNNING tasks to avoid high-impact actions running on boot blindly.
                # RUNNING tasks from last boot are paused as WAITING.
                if state.status == "RUNNING":
                    state.status = "WAITING"
                    state.waiting_reason = "Paused due to Hub restart."
                    await self._persist_task_state(state)
        log.info(f"[TaskOrchestrator] Loaded {count} active tasks from persistence.")

    def _create_task_prompt(self, state: TaskState, context: Dict[str, Any]) -> str:
        from .capability_registry import CanonicalCapabilityRegistry
        reg = CanonicalCapabilityRegistry.get_instance()
        avail_caps = reg.get_available_capabilities(device_id=state.source_device)
        cap_summary = ", ".join([c.name for c in avail_caps]) if avail_caps else "app.launch, device.status, fs.read, ui.notify"

        prompt = (
            f"You are the Cortex Task Orchestrator.\n"
            f"Goal: {state.original_user_goal}\n"
            f"Current Step: {state.current_step}\n"
            f"Status: {state.status}\n"
            f"Waiting Reason: {state.waiting_reason or 'None'}\n"
            f"Completed Steps: {json.dumps(state.completed_steps, indent=2)}\n"
            f"Latest Result: {json.dumps(state.latest_action_result, indent=2) if state.latest_action_result else 'None'}\n\n"
            f"Available Capabilities: {cap_summary}\n"
            f"Rules:\n"
            f"- Evaluate the latest result. If the goal is complete, return FINAL_RESPONSE.\n"
            f"- If the goal is not complete, return exactly ONE NEXT_ACTION to progress.\n"
            f"- If you need to wait (e.g. for a device to reconnect), return WAIT and provide the reason in 'response_text'.\n"
            f"- If the task cannot be completed, return FAIL.\n"
            f"- Do NOT generate a sequence of actions. Only propose the NEXT action.\n"
            f"Format your response as EXACTLY this JSON structure (do not use markdown blocks):\n"
            f"{{\n"
            f"  \"type\": \"TASK_STEP\",\n"
            f"  \"step_decision\": {{\n"
            f"    \"decision\": \"<NEXT_ACTION | FINAL_RESPONSE | WAIT | FAIL>\",\n"
            f"    \"response_text\": \"<text if FINAL_RESPONSE, FAIL, or WAIT, otherwise null>\",\n"
            f"    \"action_plan\": {{\n"
            f"      \"intent\": \"<e.g. open_app, get_device_status>\",\n"
            f"      \"source_device\": \"{state.source_device}\",\n"
            f"      \"target_device\": null,\n"
            f"      \"target_device_reference\": \"this device\",\n"
            f"      \"capability\": \"<capability>\",\n"
            f"      \"action\": \"<e.g. open_app, get_status>\",\n"
            f"      \"arguments\": {{}},\n"
            f"      \"requires_confirmation\": false,\n"
            f"      \"confidence\": 1.0,\n"
            f"      \"user_facing_summary\": \"<summary>\"\n"
            f"    }} // ONLY include action_plan if decision is NEXT_ACTION, else null\n"
            f"  }}\n"
            f"}}\n"
        )
        return prompt

    async def resume_task(self, task_id: str, context: Dict[str, Any], log_action_callback, request_id: str = "unknown") -> ModelResult:
        state = self.active_tasks.get(task_id)
        if not state:
            return ModelResult(type="RESPONSE", response_text="Task not found.")
        if state.status == "RUNNING":
            return ModelResult(type="RESPONSE", response_text="Task is already running.")
        
        state.status = "RUNNING"
        state.waiting_reason = None
        state.updated_at = time.time()
        await self._persist_task_state(state)
        
        log.info(f"[TaskOrchestrator] Resuming task {state.task_id} for goal: {state.original_user_goal}")
        import asyncio
        task_coro = self._run_task_background(state, context, log_action_callback, request_id)
        bg_task = asyncio.create_task(task_coro)
        self.background_tasks[state.task_id] = bg_task
        bg_task.add_done_callback(lambda t: self.background_tasks.pop(state.task_id, None))
        
        return ModelResult(type="TASK_STARTED", response_text=f"Resuming task: {state.original_user_goal}")

    async def execute_task(
        self, 
        natural_request: str, 
        source_device_id: str, 
        context: Dict[str, Any],
        log_action_callback,
        request_id: str = "unknown"
    ) -> ModelResult:
        if source_device_id in self.device_active_task:
            active_id = self.device_active_task[source_device_id]
            st = self.active_tasks.get(active_id)
            if st and st.status == "RUNNING":
                return ModelResult(type="RESPONSE", response_text="I'm still working on your previous request.")
                
        state = TaskState(original_user_goal=natural_request, source_device=source_device_id)
        self.active_tasks[state.task_id] = state
        self.device_active_task[state.source_device] = state.task_id
        await self._persist_task_state(state)
        log.info(f"[TaskOrchestrator] Started task {state.task_id} for goal: {natural_request}")
        
        # Start background task instead of blocking
        import asyncio
        task_coro = self._run_task_background(state, context, log_action_callback, request_id)
        bg_task = asyncio.create_task(task_coro)
        self.background_tasks[state.task_id] = bg_task
        # Clean up reference when done
        bg_task.add_done_callback(lambda t: self.background_tasks.pop(state.task_id, None))
        
        return ModelResult(type="TASK_STARTED", response_text="Okay, I'm working on it.")
        
    async def _run_task_background(
        self,
        state: Any = None,
        context: Optional[Dict[str, Any]] = None,
        log_action_callback: Any = None,
        request_id: str = "",
        task_id: Optional[str] = None,
        title: Optional[str] = None,
        plan: Optional[Any] = None,
        source_device_id: Optional[str] = None,
        **kwargs,
    ):
        # Direct execution path for single action plan and capability validation
        if plan is not None or (isinstance(state, str) and not isinstance(context, dict)):
            tid = task_id or (state if isinstance(state, str) else "task_direct")
            t_plan = plan or kwargs.get("plan")
            src_dev = source_device_id or kwargs.get("source_device_id", "")
            from .capability_registry import CanonicalCapabilityRegistry
            reg = CanonicalCapabilityRegistry.get_instance()
            cap_name = getattr(t_plan, "capability", None) or getattr(t_plan, "action", None)
            cap_obj = reg.get_capability(cap_name) if cap_name else None
            if not cap_obj:
                log.warning(f"[TaskOrchestrator] Rejecting unregistered capability '{cap_name}'")
                return {
                    "status": "FAILED",
                    "error": f"Capability '{cap_name}' is an unregistered or invented capability.",
                }
            if not cap_obj.availability:
                return {
                    "status": "FAILED",
                    "error": f"Capability '{cap_name}' is currently unavailable.",
                }
            if self.control and hasattr(self.control, "execute_command"):
                try:
                    await self.control.execute_command(
                        dev=src_dev,
                        action=t_plan.action,
                        args=t_plan.arguments,
                    )
                except Exception as e:
                    return {"status": "FAILED", "error": str(e)}
            return {"status": "COMPLETED", "task_id": tid, "capability": cap_name}

        log.info(f"TASK_STARTED | task_id={state.task_id} | request_id={request_id} | source_device={state.source_device}")
        log.info(f"BACKGROUND_LOOP_ENTERED | task_id={state.task_id}")
        start_time = time.time()
        final_response = "Task ended unexpectedly."
        try:
            while state.status == "RUNNING":
                if time.time() - start_time > 120:
                    state.status = "FAILED"
                    state.updated_at = time.time()
                    log.warning(f"[TaskOrchestrator] Task {state.task_id} failed: overall timeout exceeded.")
                    final_response = "I couldn't complete the task within the time limit."
                    break
                    
                if state.current_step >= state.max_steps:
                    state.status = "FAILED"
                    state.updated_at = time.time()
                    log.warning(f"[TaskOrchestrator] Task {state.task_id} failed: max steps exceeded.")
                    final_response = "I couldn't complete the task within the step limit."
                    break
                    
                prompt = self._create_task_prompt(state, context)
                context_override = dict(context)
                context_override["force_task_orchestration"] = True
                
                log.info(f"TASK_STEP start | task_id={state.task_id} | step={state.current_step}")
                log.info(f"TASK_STEP_MODEL_REQUEST | task_id={state.task_id} | step={state.current_step}")
                step_start_t = time.monotonic()
                try:
                    res = await self.router.generate_result(prompt, context_override)
                    log.info(f"TASK_STEP_MODEL_RESULT | task_id={state.task_id} | type={res.type}")
                except Exception as e:
                    log.error(f"[TaskOrchestrator] Model error: {e}")
                    state.status = "FAILED"
                    final_response = "I encountered a reasoning error while planning the task."
                    break
                finally:
                    step_end_t = time.monotonic()
                    log.info(f"TASK_STEP end | task_id={state.task_id} | step={state.current_step} | latency={step_end_t - step_start_t:.3f}s")
                    
                if not getattr(res, "step_decision", None):
                    log.error("[TaskOrchestrator] Malformed model output: missing step_decision")
                    state.status = "FAILED"
                    final_response = "I encountered an internal error while parsing the task plan."
                    break
                    
                decision = res.step_decision.get("decision")
                
                if decision == "FINAL_RESPONSE":
                    state.status = "COMPLETED"
                    state.updated_at = time.time()
                    final_response = res.step_decision.get("response_text", "Task completed.")
                    break
                    
                elif decision == "FAIL":
                    state.status = "FAILED"
                    state.updated_at = time.time()
                    final_response = res.step_decision.get("response_text", "I was unable to complete the task.")
                    break
                    
                elif decision == "WAIT":
                    state.status = "WAITING"
                    state.waiting_reason = res.step_decision.get("response_text", "Waiting for unknown reason.")
                    state.updated_at = time.time()
                    import asyncio
                    asyncio.create_task(self._persist_task_state(state))
                    final_response = state.waiting_reason
                    break
                    
                elif decision == "NEXT_ACTION":
                    # Check cancellation BEFORE dispatch
                    if state.status == "CANCELLED":
                        log.info(f"Task cancelled before dispatch | task_id={state.task_id}")
                        final_response = ""
                        break
                        
                    log.info(f"NEXT_ACTION | task_id={state.task_id} | action={res.step_decision.get('action_plan', {}).get('action')}")
                    action_data = res.step_decision.get("action_plan")
                    if not action_data:
                        log.error("[TaskOrchestrator] NEXT_ACTION missing action_plan")
                        state.status = "FAILED"
                        final_response = "I encountered an internal error: missing action plan."
                        import asyncio
                        asyncio.create_task(self._persist_task_state(state))
                        break
                        
                    try:
                        plan = ActionPlan(**action_data)
                    except Exception as e:
                        log.error(f"[TaskOrchestrator] ActionPlan validation failed: {e}")
                        state.status = "FAILED"
                        final_response = "I encountered an internal error: invalid action plan."
                        import asyncio
                        asyncio.create_task(self._persist_task_state(state))
                        break
                    
                    # Loop detection: did we just do this exact action?
                    if state.completed_steps:
                        last_step = state.completed_steps[-1]
                        if last_step.get("action") == plan.action and last_step.get("arguments") == plan.arguments:
                            log.error("[TaskOrchestrator] Loop detected: repeated action.")
                            state.status = "FAILED"
                            final_response = "I realized I was repeating the same action, so I stopped."
                            break
                    
                    state.current_step += 1
                    state.updated_at = time.time()
                    
                    # Execute it!
                    try:
                        # Resolve device
                        target_device = await self.control.resolve_target_device(state.source_device, plan.target_device_reference)
                        if not target_device:
                            state.latest_action_result = {"status": "error", "message": f"Device '{plan.target_device_reference}' not found."}
                            continue
                            
                        # Validate capabilities against CanonicalCapabilityRegistry
                        from .capability_registry import CanonicalCapabilityRegistry
                        reg = CanonicalCapabilityRegistry.get_instance()
                        cap_obj = reg.get_capability(plan.capability)
                        if not cap_obj:
                            state.latest_action_result = {
                                "status": "error",
                                "message": f"Capability '{plan.capability}' is not a registered capability in Cortex."
                            }
                            continue

                        if not cap_obj.availability:
                            state.latest_action_result = {
                                "status": "error",
                                "message": f"Capability '{plan.capability}' is currently unavailable."
                            }
                            continue

                        val_call = reg.validate_call(plan.capability, plan.arguments or {}, device_id=target_device)
                        if not val_call["valid"]:
                            state.latest_action_result = {
                                "status": "error",
                                "message": f"Capability arguments invalid: {val_call['reason']}"
                            }
                            continue

                        val = await self.control.validate_target_and_capability(target_device, plan.capability)
                        if not val["valid"]:
                            state.latest_action_result = {"status": "error", "message": f"Validation failed: {val['reason']}"}
                            continue
                            
                        # Permissions
                        perm = await self.control.check_permissions(state.source_device, target_device, plan.capability)
                        if not perm or not perm.allowed:
                            state.latest_action_result = {"status": "error", "message": "Permission denied."}
                            continue

                        # Side-effect safety check
                        from .task_ownership import get_action_safety_class, ActionSafetyClass
                        safety_cls = get_action_safety_class(plan.capability)
                        if safety_cls == ActionSafetyClass.HUMAN_APPROVAL_REQUIRED and not plan.requires_confirmation:
                            plan.requires_confirmation = True
                            
                        # Check cancellation again right before execution
                        if state.status == "CANCELLED":
                            log.info(f"Task cancelled immediately before dispatch | task_id={state.task_id}")
                            final_response = ""
                            break
                            
                        # Wait for result via PENDING_FUTURES (this requires integration with main.py)
                        import asyncio
                        import uuid
                        from .main import PENDING_FUTURES
                        
                        cmd_id = uuid.uuid4().hex
                        fut = asyncio.get_running_loop().create_future()
                        PENDING_FUTURES[cmd_id] = fut
                        
                        # Dispatch
                        log.info(f"ACTION_DISPATCH | task_id={state.task_id} | action={plan.action}")
                        log.info(f"action dispatch | task_id={state.task_id} | action={plan.action} | cmd_id={cmd_id[:8]}...")
                        await self.control.execute_command(target_device, plan.action, plan.arguments, cmd_id=cmd_id)
                        
                        try:
                            exec_res = await asyncio.wait_for(fut, timeout=15.0)
                            state.latest_action_result = {"status": exec_res.status, "message": exec_res.message, "data": getattr(exec_res, "data", None)}
                        except asyncio.TimeoutError:
                                state.latest_action_result = {"status": "error", "message": "Execution timed out."}
                        finally:
                            PENDING_FUTURES.pop(cmd_id, None)
                            
                        state.completed_steps.append({
                            "step": state.current_step,
                            "action": plan.action,
                            "arguments": plan.arguments,
                            "result": state.latest_action_result
                        })
                        log.info(f"ACTION_RESULT | task_id={state.task_id} | status={state.latest_action_result.get('status')}")
                        log.info(f"action result | task_id={state.task_id} | action={plan.action} | status={state.latest_action_result.get('status')}")
                        
                    except Exception as e:
                        log.error(f"[TaskOrchestrator] Execution error: {e}")
                        state.latest_action_result = {"status": "error", "message": str(e)}
                        
                else:
                    log.error(f"[TaskOrchestrator] Unknown decision: {decision}")
                    state.status = "FAILED"
                    final_response = "I encountered an internal logic error."
                    break
                    
        except Exception as e:
            import traceback
            log.error(f"Uncaught exception in _run_task_background: {e}\n{traceback.format_exc()}")
            state.status = "FAILED"
            final_response = "I encountered a severe internal error."
            
        finally:
            # Loop ended
            log.info(f"TASK_{state.status} | task_id={state.task_id}")
            
            if self.device_active_task.get(state.source_device) == state.task_id:
                del self.device_active_task[state.source_device]
                
            if state.status != "CANCELLED" and final_response:
                await self._send_final_spoken_response(state.source_device, state.task_id, final_response)
            
    async def _send_final_spoken_response(self, device_id: str, task_id: str, message: str):
        from .main import CONNECTED_DEVICES
        ws = CONNECTED_DEVICES.get(device_id)
        if not ws:
            return
            
        # Send an unsolicited voice_response to force the device to speak it
        resp = {
            "type": "voice_response",
            "command_id": f"task_{task_id}",
            "status": "success",
            "message": message
        }
        try:
            await ws.send_text(json.dumps(resp))
        except Exception as e:
            log.error(f"Failed to send final task response: {e}")
