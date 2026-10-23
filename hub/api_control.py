"""
hub/api_control.py
------------------
Phase A9: Cortex Control Center & Product Frontend Backend Layer.

Provides:
- Authoritative canonical state aggregation across all Cortex subsystems:
  * SystemRegistry (identity, models, providers, integrations, capabilities)
  * ExecutorRegistry (live device handles, online status, capabilities, workload)
  * TaskOwnershipManager & GoalManager (tasks, goals, leases, handoffs, wait conditions)
  * Pending Confirmations / Approval store (Tier 3 approval queue)
  * FileTransferManager (A8 chunked transfer sessions, progress, verification)
  * WorldStateManager (fused situational context, foreground app/window, telemetry)
  * Global Autonomy state (kill switch toggle)
  * Structured frontend-safe activity event stream (no chain-of-thought)
- Unified WebSocket channel (/ws-control) with real-time broadcasting and reconnect reconciliation.
- REST endpoints under /api/control/ for snapshot retrieval and state manipulation.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends, Request
from pydantic import BaseModel

from .system_registry import SystemRegistry
from .executor_registry import ExecutorRegistry
from .task_ownership import TaskOwnershipManager
from .goals import GoalManager
from .world_state import WorldStateManager
from .file_transfer import FileTransferManager, TransferSession
from .database import (
    get_pending_confirmations,
    get_confirmation,
    resolve_confirmation,
    record_command,
    list_transfer_sessions,
    get_tasks,
)
from .pipeline.event_bus import (
    EventBus,
    CortexEvent,
    DeviceConnected,
    DeviceDisconnected,
    TaskStateChanged,
    WorldStateChanged,
)

log = logging.getLogger("cortex.api_control")

async def authenticate_control(request: Request):
    from .main import _authenticate_http_headers
    caller = await _authenticate_http_headers(request.headers.get("X-Cortex-Device"),
        request.headers.get("X-Cortex-Secret"), request.cookies.get("cortex_session"))
    if request.url.path != "/api/control/message" and caller.get("role") not in {"admin", "primary_admin"}:
        raise HTTPException(status_code=403, detail="Administrator access is required for global controls.")
    request.state.control_caller = caller
    return caller


router = APIRouter(prefix="/api/control", tags=["control_center"], dependencies=[Depends(authenticate_control)])

# ---------------------------------------------------------------------------
# Activity Stream (Frontend-Safe Structured Operational Events)
# ---------------------------------------------------------------------------
MAX_ACTIVITY_HISTORY = 50
_activity_history: List[Dict[str, Any]] = []

def emit_activity(
    label: str,
    category: str = "execution",
    status: str = "active",
    device_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Emit a concise, frontend-safe activity event.
    Strictly operational: NO chain-of-thought or model reasoning is permitted.
    """
    event = {
        "type": "activity",
        "category": category,
        "label": label,
        "status": status,
        "device_id": device_id,
        "task_id": task_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _activity_history.append(event)
    if len(_activity_history) > MAX_ACTIVITY_HISTORY:
        _activity_history.pop(0)

    # Schedule broadcast to connected WebSockets
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(broadcast_control("activity", event))
    except RuntimeError:
        pass

    return event


def get_recent_activities(limit: int = 30) -> List[Dict[str, Any]]:
    return list(_activity_history[-limit:])


# ---------------------------------------------------------------------------
# WebSocket Client Registry & Real-Time Broadcast
# ---------------------------------------------------------------------------
CONTROL_CONNECTIONS: List[WebSocket] = []

async def broadcast_control(event_type: str, data: Any) -> None:
    """Broadcast real-time state change event to all active Control Center sessions."""
    payload = json.dumps({"event": event_type, "data": data})
    dead: List[WebSocket] = []
    for ws in list(CONTROL_CONNECTIONS):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in CONTROL_CONNECTIONS:
            CONTROL_CONNECTIONS.remove(ws)


# ---------------------------------------------------------------------------
# Canonical State Aggregator
# ---------------------------------------------------------------------------
async def build_canonical_state() -> Dict[str, Any]:
    """
    Assemble authoritative snapshot of live backend state from real sources.
    NO fake or mock frontend-only state.
    """
    # 1. System Registry (Identity, Models, Providers, Integrations)
    sys_reg = SystemRegistry.get_instance()
    identity = sys_reg.get_identity()
    models = sys_reg.get_models()
    providers = sys_reg.get_providers()
    integrations = await sys_reg.list_integrations()
    capabilities = sys_reg.list_capabilities()

    # 2. Devices (ExecutorRegistry - canonical liveness)
    exec_reg = ExecutorRegistry.get_instance()
    device_handles = exec_reg.list_device_handles()
    devices_data = []
    for h in device_handles:
        d_dict = h.to_dict()
        # Ensure canonical liveness field
        d_dict["online"] = exec_reg.is_device_online(h.device_id)
        d_dict["heartbeat_freshness"] = exec_reg.get_heartbeat_freshness(h.device_id)
        devices_data.append(d_dict)

    # 3. Tasks & Goals (TaskOwnershipManager + GoalManager)
    tom = TaskOwnershipManager.get_instance()
    gm = GoalManager.get_instance()
    
    tasks_data = []
    # Fetch goals
    try:
        goals = await gm.list_goals()
        for g in goals:
            tasks_data.append({
                "id": g.goal_id,
                "title": g.title,
                "status": g.status,
                "owner": "cortex-hub",
                "executor": g.metadata.get("executor_id"),
                "current_step": g.metadata.get("current_step", ""),
                "waiting_reason": g.waiting_reason or "",
                "progress": g.metadata.get("progress", 0.0),
                "created_at": g.created_at,
                "updated_at": g.updated_at,
                "handoff_state": g.metadata.get("handoff_state", "none"),
                "is_goal": True,
            })
    except Exception as e:
        log.warning(f"Error listing goals: {e}")

    # Fetch executions from TaskOwnershipManager
    for ex in list(tom._executions.values()):
        # Avoid duplicate if goal_id matches
        if not any(t["id"] == ex.task_execution_id for t in tasks_data):
            tasks_data.append({
                "id": ex.task_execution_id,
                "title": f"Task Step ({ex.step_id or ex.task_execution_id[:8]})",
                "status": ex.execution_state,
                "parent_task_id": ex.goal_id,
                "owner": ex.owner_device_id,
                "executor": ex.executor_device_id,
                "current_step": ex.step_id,
                "waiting_reason": ex.metadata.get("waiting_reason", ""),
                "progress": 1.0 if ex.execution_state == "COMPLETED" else 0.0 if ex.execution_state == "FAILED" else 0.5,
                "created_at": ex.created_at,
                "updated_at": ex.updated_at,
                "handoff_state": ex.metadata.get("handoff_state", "none"),
                "is_goal": False,
            })

    # Fallback to database tasks if empty
    if not tasks_data:
        try:
            db_tasks = await get_tasks()
            for dt in db_tasks:
                tasks_data.append({
                    "id": dt.get("id"),
                    "title": dt.get("title", "Scheduled Task"),
                    "status": dt.get("status", "pending"),
                    "owner": "cortex-hub",
                    "executor": dt.get("assigned_device"),
                    "current_step": dt.get("command", ""),
                    "waiting_reason": "",
                    "progress": 0.0,
                    "created_at": dt.get("created_at", ""),
                    "updated_at": dt.get("updated_at", ""),
                    "handoff_state": "none",
                    "is_goal": False,
                })
        except Exception:
            pass

    # 4. Approvals (authoritative pending confirmations)
    # Agent turns must remain visible alongside goal/ownership tasks.
    existing_ids = {t["id"] for t in tasks_data}
    for dt in await get_tasks():
        if dt.get("type") in {"agent_turn", "request"} and dt["id"] not in existing_ids:
            try:
                task_context = json.loads(dt.get("context") or "{}") if isinstance(dt.get("context"), str) else (dt.get("context") or {})
                if not isinstance(task_context, dict):
                    task_context = {}
            except (TypeError, ValueError):
                task_context = {}
            tasks_data.append({"id": dt["id"], "title": dt.get("title", "Agent task"),
                "status": dt.get("status"), "owner": dt.get("assigned_device"),
                "executor": dt.get("assigned_device"), "current_step": dt.get("type"),
                "parent_task_id": task_context.get("parent_request_id"),
                "waiting_reason": dt.get("last_error", ""), "progress": 1.0 if dt.get("status") == "COMPLETED" else 0.0,
                "created_at": dt.get("created_at", ""), "updated_at": dt.get("updated_at", ""),
                "handoff_state": "none", "is_goal": False})
    # Display safe operational state for both child and owning request identities.
    for durable in await get_tasks():
        saved = durable.get("context") or {}
        try:
            saved = json.loads(saved) if isinstance(saved,str) else saved
        except (TypeError,ValueError):
            continue
        compound = saved.get("compound_ui") or {}
        if not compound:
            continue
        for visible in tasks_data:
            if visible["id"] in {durable["id"], saved.get("parent_request_id")}:
                visible["compound"] = {key:compound.get(key) for key in
                    ("current_stage","current_observation_id","retry_count","blocked_reason","trace_sequence")}
                visible["current_step"] = compound.get("current_stage")
    pending_raw = await get_pending_confirmations()
    approvals_data = []
    for r in pending_raw:
        from .approval_observability import snapshot as approval_snapshot
        args_parsed = []
        try:
            args_parsed = json.loads(r.get("args", "[]"))
        except Exception:
            pass

        approvals_data.append({
            "command_id": r.get("command_id"),
            "target_device": r.get("target_device"),
            "source_device": r.get("requested_by"),
            "command": r.get("command"),
            "args": args_parsed,
            "action_summary": (args_parsed[0] if args_parsed else "Approve cloud sharing") if r.get("command") == "model.share" else f"Execute action '{r.get('command')}' on {r.get('target_device')}",
            "privacy_risk": "Requires administrator permission (Tier 3 confirmation)" if r.get("tier", 2) >= 3 else "Action confirmation required",
            "data_leaves_local": r.get("command") == "model.share" or r.get("target_device") != r.get("requested_by"),
            "tier": r.get("tier", 2),
            "status": r.get("status", "pending"),
            "requested_at": r.get("requested_at"),
            "reason": "Share this result with the named cloud model for this request" if r.get("command") == "model.share" else "Action involves cross-device command or elevated permissions",
            **approval_snapshot(r.get("command_id")),
            "server_now": datetime.now(timezone.utc).isoformat(),
        })

    # 5. Transfers (A8 chunked transfer sessions)
    ftm = FileTransferManager.get_instance()
    transfers_data = []
    # From in-memory active sessions
    for sess in list(ftm._active_sessions.values()):
        pct = sess.progress_pct() if callable(getattr(sess, "progress_pct", None)) else getattr(sess, "progress_pct", 0.0)
        transfers_data.append({
            "transfer_id": sess.transfer_id,
            "source": sess.source_device_id,
            "destination": sess.destination_device_id,
            "filename": sess.filename,
            "source_path": sess.source_path,
            "destination_path": sess.destination_path,
            "bytes_transferred": sess.transferred_bytes,
            "total_bytes": sess.total_bytes,
            "percent": round(pct or 0.0, 1),
            "transport": sess.transport,
            "resumable": sess.resumable,
            "checksum_algorithm": sess.checksum_algorithm,
            "source_checksum": sess.source_checksum[:12] if sess.source_checksum else "",
            "destination_checksum": getattr(sess, "destination_checksum", "")[:12] if getattr(sess, "destination_checksum", "") else "",
            "state": sess.state,
            "waiting_reason": sess.metadata.get("waiting_reason"),
            "privacy_class": sess.privacy_class,
            "created_at": sess.created_at,
        })

    # If no in-memory sessions, check DB
    if not transfers_data:
        try:
            db_sessions = await list_transfer_sessions(limit=10)
            for ds in db_sessions:
                s_obj = TransferSession.from_dict(ds)
                db_pct = s_obj.progress_pct() if callable(getattr(s_obj, "progress_pct", None)) else getattr(s_obj, "progress_pct", 0.0)
                transfers_data.append({
                    "transfer_id": s_obj.transfer_id,
                    "source": s_obj.source_device_id,
                    "destination": s_obj.destination_device_id,
                    "filename": s_obj.filename,
                    "source_path": s_obj.source_path,
                    "destination_path": s_obj.destination_path,
                    "bytes_transferred": s_obj.transferred_bytes,
                    "total_bytes": s_obj.total_bytes,
                    "percent": round(db_pct or 0.0, 1),
                    "transport": s_obj.transport,
                    "resumable": s_obj.resumable,
                    "checksum_algorithm": s_obj.checksum_algorithm,
                    "source_checksum": s_obj.source_checksum[:12] if s_obj.source_checksum else "",
                    "destination_checksum": getattr(s_obj, "destination_checksum", "")[:12] if getattr(s_obj, "destination_checksum", "") else "",
                    "state": s_obj.state,
                    "waiting_reason": s_obj.metadata.get("waiting_reason"),
                    "privacy_class": s_obj.privacy_class,
                    "created_at": s_obj.created_at,
                })
        except Exception:
            pass

    # 6. World State & Context (WorldStateManager)
    wsm = WorldStateManager.get_instance()
    world_summary = await wsm.get_compact_world_summary()
    foreground_history = wsm.get_foreground_history(limit=5)
    if world_summary.get("foreground_app") in ("unknown", None) and foreground_history:
        world_summary["foreground_app"] = foreground_history[-1].get("app_name", "unknown")
        world_summary["foreground_window_title"] = foreground_history[-1].get("window_title", "")

    # 7. Safety & Autonomy (GoalManager kill switch)
    autonomy_enabled = await gm.is_autonomy_enabled()

    # 8. Recent Activities
    activities = get_recent_activities(limit=30)
    if not activities:
        # Default initialization activity
        activities = [emit_activity("Cortex Control Center initialized", category="system", status="completed")]

    from .llm_provider import get_llm_provider
    import time
    provider = get_llm_provider()
    model_status = {"provider": provider.name, "model": getattr(provider, "model", "unknown"),
                    "last_failure": getattr(provider, "last_primary_failure_category", None),
                    "circuit_open": bool(getattr(provider, "circuit_open_until", 0) > time.time())}
    router = provider
    while hasattr(router, "provider"):
        router = router.provider
    from .direct_router import DirectModelRouter
    if isinstance(router, DirectModelRouter):
        model_status["routes"] = router.status()
        model_status["routing"] = router.last_routing
    return {
        "identity": identity,
        "devices": devices_data,
        "tasks": tasks_data,
        "approvals": approvals_data,
        "transfers": transfers_data,
        "world_state": {
            "summary": world_summary,
            "foreground_history": foreground_history,
        },
        "system_health": {
            "models": models,
            "model_status": model_status,
            "providers": providers,
            "integrations": integrations,
            "capabilities": capabilities,
            "status": "healthy",
        },
        "safety": {
            "autonomy_enabled": autonomy_enabled,
        },
        "activities": activities,
    }


# ---------------------------------------------------------------------------
# EventBus Subscription Glue
# ---------------------------------------------------------------------------
async def _on_eventbus_event(event: CortexEvent) -> None:
    """Bridge internal typed EventBus events to the Control Center WebSocket."""
    etype = getattr(event, "event_type", "unknown")
    if isinstance(event, DeviceConnected) or isinstance(event, DeviceDisconnected) or "device" in etype:
        exec_reg = ExecutorRegistry.get_instance()
        handles = [h.to_dict() for h in exec_reg.list_device_handles()]
        await broadcast_control("device_state", handles)
    elif isinstance(event, TaskStateChanged) or "task" in etype:
        snapshot = await build_canonical_state()
        await broadcast_control("task_state", snapshot["tasks"])
    elif isinstance(event, WorldStateChanged) or "world_state" in etype:
        wsm = WorldStateManager.get_instance()
        summary = await wsm.get_compact_world_summary()
        await broadcast_control("world_state", summary)

# Register subscribers once
_eventbus_registered = False
def init_control_event_bridge():
    global _eventbus_registered
    if not _eventbus_registered:
        bus = EventBus.get_instance()
        bus.subscribe(DeviceConnected, _on_eventbus_event)
        bus.subscribe(DeviceDisconnected, _on_eventbus_event)
        bus.subscribe(TaskStateChanged, _on_eventbus_event)
        bus.subscribe(WorldStateChanged, _on_eventbus_event)
        _eventbus_registered = True


# ---------------------------------------------------------------------------
# REST API Endpoints
# ---------------------------------------------------------------------------

@router.get("/state")
async def get_state_endpoint():
    """Retrieve full canonical state snapshot for initial render or reconnect reconciliation."""
    return await build_canonical_state()


class MessageRequest(BaseModel):
    text: str
    device_id: Optional[str] = None
    speaker_id: Optional[str] = None
    is_voice: Optional[bool] = False
    target_device: Optional[str] = None
    request_id: Optional[str] = None


@router.post("/message")
async def send_message_endpoint(req: MessageRequest, request: Request = None):
    """
    Process conversation input from the UI through AgentHarness pipeline.
    Emits operational activity events and returns structured response.
    """
    if request is not None:
        caller_id = request.state.control_caller["device_id"]
        if req.device_id and req.device_id != caller_id:
            raise HTTPException(status_code=403, detail="A request cannot impersonate another device.")
        req = req.model_copy(update={"device_id": caller_id})
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message text cannot be empty.")

    emit_activity("Understanding request", category="planning", status="active", device_id=req.device_id)

    import hub.main as hub_main
    harness = getattr(hub_main, "_agent_harness", None)
    if harness is None:
        from .agent_harness import AgentHarness
        from .control_layer import CortexControlLayer
        conn_devs = getattr(hub_main, "CONNECTED_DEVICES", {})
        harness = AgentHarness(CortexControlLayer(conn_devs))

    from .context import ContextManager
    from .database import log_action

    if not req.device_id:
        raise HTTPException(status_code=400, detail="An authenticated source device is required.")
    from .request_lifecycle import RequestLifecycle
    import uuid
    request_id = req.request_id or uuid.uuid4().hex
    async def prepare():
        context = await ContextManager.get_active_context(device_id=req.device_id, request=text)
        if req.target_device:
            context["requested_device"] = req.target_device
        emit_activity("Checking current context", category="perception", status="active", device_id=req.device_id)
        return context
    async def audit(src, intent, status, detail):
        await log_action(device_id=src, command=intent, result=status, message=detail, command_id=request_id)
    async def deliver(task_id, result):
        emit_activity("Chat request finished", category="execution", device_id=req.device_id,
            task_id=task_id, status="completed" if result.status == "success" else "error")
        await broadcast_control("task_state", {"task_id": task_id, "status": result.status})
    result = await RequestLifecycle.submit(req.device_id, request_id, text, prepare, harness,
        channel="gui", deliver=deliver, log_action_callback=audit)
    event_status = "active" if result.status == "in_progress" else "completed" if result.status == "success" else "error"
    emit_activity(f"Request outcome: {result.status}", category="execution", status=event_status, device_id=req.device_id)

    resp_data = {
        "user_message": text,
        "response_text": result.message,
        "status": result.status,
        "task_id": (result.data or {}).get("task_id"),
        "data": result.data,
        "action_plan": result.action_plan.model_dump() if result.action_plan else None,
        "is_voice": bool(req.is_voice),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Broadcast conversation event to any other connected sessions
    await broadcast_control("conversation", resp_data)

    return resp_data


class AutonomyRequest(BaseModel):
    enabled: bool


@router.post("/autonomy")
async def set_autonomy_endpoint(req: AutonomyRequest):
    """Toggle global autonomy kill switch."""
    gm = GoalManager.get_instance()
    await gm.set_autonomy_enabled(req.enabled)
    status_label = "enabled" if req.enabled else "stopped (kill switch engaged)"
    emit_activity(f"Global autonomy {status_label}", category="system", status="active")
    
    if not req.enabled:
        from .agent_turn import AgentTurn
        from .request_lifecycle import RequestLifecycle
        RequestLifecycle.cancel_all()
        for device in list(AgentTurn._devices):
            AgentTurn.cancel_for_device(device)
        # Emergency pause of active goals
        await gm.pause_all_goals(reason="Global autonomy kill switch engaged by user")

    await broadcast_control("safety_state", {"autonomy_enabled": req.enabled})
    return {"status": "success", "autonomy_enabled": req.enabled}


class TaskActionRequest(BaseModel):
    action: str = "stop"  # "stop" or "pause"


@router.post("/tasks/{task_id}/stop")
async def stop_task_endpoint(task_id: str, req: TaskActionRequest = None):
    """Stop/cancel a specific goal or task."""
    from .agent_turn import AgentTurn
    from .database import get_task, update_task, save_task_execution
    gm = GoalManager.get_instance()
    goal = await gm.get_goal(task_id)
    stored = await get_task(task_id)
    tom = TaskOwnershipManager.get_instance()
    execution = await tom.get_execution(task_id)
    # A stale control must never overwrite a durable outcome. The legacy
    # dashboard uses this endpoint directly, without Workspace's terminal guard.
    current_status = (goal.status if goal else stored.get("status") if stored else
                      execution.execution_state if execution else "")
    if str(current_status).upper() in {
        "COMPLETED", "CANCELLED", "FAILED", "DENIED", "EXPIRED", "INTERRUPTED",
        "SUCCESS", "ERROR", "UNSUPPORTED", "VERIFICATION_FAILED",
    }:
        raise HTTPException(status_code=409, detail="The task has already ended; refresh its state.")
    if goal:
        await gm.cancel_goal(task_id, reason="Cancelled from Control Center")
    elif stored:
        from .request_lifecycle import RequestLifecycle
        RequestLifecycle.cancel_task(task_id)
        AgentTurn.cancel_task(task_id)
        from .orchestration import Orchestrator
        Orchestrator.cancel(task_id)
        await update_task(task_id, {"status": "CANCELLED" if stored.get("type") == "agent_turn" else "cancelled"})
    elif execution:
        await tom.expire_lease(task_id)
        execution.execution_state = "FAILED"
        await save_task_execution(execution.to_dict())
    else:
        raise HTTPException(status_code=404, detail="Task not found.")

    emit_activity(f"Task '{task_id[:8]}' stopped by user", category="execution", status="completed")
    
    snapshot = await build_canonical_state()
    await broadcast_control("task_state", snapshot["tasks"])
    return {"status": "success", "task_id": task_id, "state": "CANCELLED"}


@router.post("/tasks/pause_all")
async def pause_all_tasks_endpoint():
    """Emergency pause of all active goals."""
    gm = GoalManager.get_instance()
    paused = await gm.pause_all_goals(reason="Paused from Control Center")
    emit_activity(f"Paused {len(paused)} active tasks", category="execution", status="active")

    snapshot = await build_canonical_state()
    await broadcast_control("task_state", snapshot["tasks"])
    return {"status": "success", "paused_count": len(paused), "paused_ids": paused}


class ResolveApprovalRequest(BaseModel):
    decision: str  # "approve" or "deny"


@router.get("/approvals/{command_id}")
async def approval_status_endpoint(command_id: str, request: Request):
    """Origin-scoped status/expiry metadata, with no observation contents."""
    row = await get_confirmation(command_id)
    if not row:
        raise HTTPException(status_code=404, detail="Approval not found")
    if row.get("requested_by") != request.state.control_caller["device_id"]:
        raise HTTPException(status_code=403, detail="Only the requesting device can inspect this approval")
    from .approval_observability import snapshot
    return {"command_id":command_id, "status":row["status"], **snapshot(command_id),
            "server_now":datetime.now(timezone.utc).isoformat()}


@router.post("/approvals/{command_id}/resolve")
async def resolve_approval_endpoint(command_id: str, req: ResolveApprovalRequest, request: Request = None):
    """Resolve pending Tier 3 approval with allow or deny."""
    if req.decision not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="Decision must be 'approve' or 'deny'.")

    import hub.main as hub_main
    if request is None:
        raise HTTPException(status_code=401, detail="An authenticated approving device is required.")
    success, msg = await hub_main._execute_resolve_confirmation(command_id, req.decision,
                                                              request.state.control_caller["device_id"])
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    emit_activity(f"Approval '{command_id[:8]}' resolved: {req.decision.upper()}", category="approval", status="completed")

    # Broadcast updated approvals
    pending = await get_pending_confirmations()
    await broadcast_control("approval_state", pending)

    return {"status": "success", "command_id": command_id, "decision": req.decision}


@router.post("/transfers/{transfer_id}/cancel")
async def cancel_transfer_endpoint(transfer_id: str):
    """Cancel an ongoing file transfer session."""
    ftm = FileTransferManager.get_instance()
    sess = await ftm.get_session(transfer_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Transfer session not found.")

    if sess.metadata.get("worker_protocol"):
        from .peer_transfer import cancel_worker_transfer
        try:
            await cancel_worker_transfer(transfer_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "success", "transfer_id": transfer_id, "state": "CANCELLED"}
    sess.state = "CANCELLED"
    sess.metadata["failure_reason"] = "Cancelled by user from Control Center"
    from .database import save_transfer_session
    await save_transfer_session(sess.to_dict())

    emit_activity(f"File transfer '{sess.filename}' cancelled", category="transfer", status="completed")

    snapshot = await build_canonical_state()
    await broadcast_control("transfer_state", snapshot["transfers"])
    return {"status": "success", "transfer_id": transfer_id, "state": "CANCELLED"}


@router.post("/transfers/{transfer_id}/resume")
async def resume_transfer_endpoint(transfer_id: str):
    """Resume a paused or failed transfer session if resumable."""
    ftm = FileTransferManager.get_instance()
    sess = await ftm.get_session(transfer_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Transfer session not found.")
    if not sess.resumable:
        raise HTTPException(status_code=400, detail="Transfer session is not marked resumable.")

    sess.state = "ACTIVE"
    from .database import save_transfer_session
    await save_transfer_session(sess.to_dict())

    emit_activity(f"Resumed transfer '{sess.filename}'", category="transfer", status="active")

    snapshot = await build_canonical_state()
    await broadcast_control("transfer_state", snapshot["transfers"])
    return {"status": "success", "transfer_id": transfer_id, "state": "ACTIVE"}


# ---------------------------------------------------------------------------
# WebSocket Handler (/ws-control)
# ---------------------------------------------------------------------------
async def control_websocket_endpoint(websocket: WebSocket):
    """
    Primary Control Center real-time channel.
    Connects frontend with automatic initial state delivery,
    reconnect reconciliation, and event broadcasts.
    """
    from .main import _authenticate_http_headers
    try:
        caller = await _authenticate_http_headers(websocket.headers.get("X-Cortex-Device"),
            websocket.headers.get("X-Cortex-Secret"), websocket.cookies.get("cortex_session"))
        if caller.get("role") not in {"admin", "primary_admin"}:
            await websocket.close(code=4403)
            return
    except HTTPException:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    CONTROL_CONNECTIONS.append(websocket)
    log.info(f"Control Center WebSocket connected (total={len(CONTROL_CONNECTIONS)})")

    # Ensure event bridge is initialized
    init_control_event_bridge()

    # Step 1: Send canonical initial state immediately
    try:
        initial_state = await build_canonical_state()
        await websocket.send_text(json.dumps({
            "event": "initial_state",
            "data": initial_state
        }))
    except Exception as exc:
        log.warning(f"Error sending initial state over /ws-control: {exc}")

    # Step 2: Inbound message loop
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"event": "error", "message": "Invalid JSON"}))
                continue

            msg_type = data.get("type", "")

            if msg_type == "get_state":
                # Reconnection reconciliation request
                state = await build_canonical_state()
                await websocket.send_text(json.dumps({"event": "initial_state", "data": state}))

            elif msg_type == "send_message":
                text = data.get("text", "").strip()
                dev_id = caller["device_id"]
                if data.get("device_id", dev_id) != dev_id:
                    await websocket.send_json({"event": "error", "message": "A request cannot impersonate another device."})
                    continue
                is_voice = data.get("is_voice", False)
                if text:
                    await send_message_endpoint(MessageRequest(text=text, device_id=dev_id,
                        target_device=data.get("target_device"), is_voice=is_voice, request_id=data.get("request_id")))

            elif msg_type == "resolve_approval":
                cid = data.get("command_id")
                dec = data.get("decision", "deny")
                if cid:
                    import hub.main as hub_main
                    success, msg = await hub_main._execute_resolve_confirmation(cid, dec, caller["device_id"])
                    if not success:
                        await websocket.send_json({"event": "error", "message": msg})
                        continue
                    emit_activity(f"Approval '{cid[:8]}' resolved: {dec.upper()}", category="approval", status="completed")
                    state = await build_canonical_state()
                    await broadcast_control("approval_state", state["approvals"])

            elif msg_type == "set_autonomy":
                en = bool(data.get("enabled", False))
                gm = GoalManager.get_instance()
                await gm.set_autonomy_enabled(en)
                if not en:
                    from .request_lifecycle import RequestLifecycle
                    RequestLifecycle.cancel_all()
                    from .agent_turn import AgentTurn
                    for device in list(AgentTurn._devices):
                        AgentTurn.cancel_for_device(device)
                    await gm.pause_all_goals(reason="Kill switch engaged")
                emit_activity(f"Autonomy set to {en}", category="system", status="active")
                await broadcast_control("safety_state", {"autonomy_enabled": en})

            elif msg_type == "stop_task":
                tid = data.get("task_id")
                if tid:
                    from .agent_turn import AgentTurn
                    AgentTurn.cancel_task(tid)
                    from .request_lifecycle import RequestLifecycle
                    from .orchestration import Orchestrator
                    RequestLifecycle.cancel_task(tid)
                    Orchestrator.cancel(tid)
                    gm = GoalManager.get_instance()
                    goal = await gm.get_goal(tid)
                    if goal:
                        goal.status = "CANCELLED"
                        await gm.update_goal(goal)
                    state = await build_canonical_state()
                    await broadcast_control("task_state", state["tasks"])

            elif msg_type == "cancel_transfer":
                xid = data.get("transfer_id")
                if xid:
                    ftm = FileTransferManager.get_instance()
                    sess = await ftm.get_session(xid)
                    if sess:
                        if sess.metadata.get("worker_protocol"):
                            from .peer_transfer import cancel_worker_transfer
                            try:
                                await cancel_worker_transfer(xid)
                            except ValueError as exc:
                                await websocket.send_json({"event": "error", "message": str(exc)})
                            continue
                        sess.state = "CANCELLED"
                        from .database import save_transfer_session
                        await save_transfer_session(sess.to_dict())
                    state = await build_canonical_state()
                    await broadcast_control("transfer_state", state["transfers"])

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning(f"Control Center WebSocket error: {exc}")
    finally:
        if websocket in CONTROL_CONNECTIONS:
            CONTROL_CONNECTIONS.remove(websocket)
        log.info(f"Control Center WebSocket disconnected (remaining={len(CONTROL_CONNECTIONS)})")
