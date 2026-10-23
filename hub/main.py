"""
hub/main.py
-----------
Cortex Hub — FastAPI WebSocket server (Phase 3).

Endpoints
---------
  GET   /                  — health check (hub status + connected devices)
  GET   /devices           — all enrolled devices with role, online status, last_seen
  GET   /logs              — last 20 action log entries from SQLite
  POST  /bootstrap         — first-run: create the admin device (localhost only)
  POST  /enroll            — register a new device (requires admin auth header)
  POST  /rotate-secret     — rotate a device's secret (admin only)
  GET   /pending           — list pending confirmations
  WS    /ws/{device_id}    — persistent WebSocket connection for agents (auth required)
  WS    /ws-cli            — CLI channel (auth required since Phase 3)

Security model (Phase 3)
-------------------------
  - Default-deny: PeerFilterMiddleware rejects all non-localhost/non-Tailscale
    traffic before any authentication logic runs.
  - Every agent must authenticate via challenge/response on connect.
  - CLI must authenticate with device_id + secret (same flow as agents).
  - HTTP read endpoints require X-Cortex-Device + X-Cortex-Secret headers.
  - /bootstrap is restricted to localhost only.
  - Commands are checked against role-based permissions before forwarding.
  - Tier 2/3 commands require explicit admin confirmation (60s timeout).
  - Duplicate command_ids are rejected (persisted in SQLite).
  - Commands expire after COMMAND_TTL seconds (hub receipt time, never client clocks).
  - Device secrets are never printed or logged.

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional, Any, Dict, List, Union, Callable
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, Cookie, Response
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import create_challenge, generate_device_secret, hash_secret, verify_secret
from .config import (
    AUTH_TIMEOUT,
    COMMAND_HISTORY_RETENTION_HOURS,
    COMMAND_TTL,
    CONFIRMATION_TIMEOUT,
    PRUNE_INTERVAL,
    is_trusted_peer,
)
from .database import (
    create_confirmation,
    enroll_device,
    get_device,
    get_pending_confirmations,
    get_recent_logs,
    has_admin,
    init_db,
    is_command_expired,
    is_duplicate_command,
    list_devices,
    log_action,
    prune_command_history,
    record_command,
    resolve_confirmation,
    rotate_device_secret,
    update_command_status,
    update_connection_state,
    reset_stale_device_connections,
    update_last_seen,
    update_device_capabilities,
    get_device_capabilities,
    get_device_permissions,
    set_device_permission,
    create_session,
    get_session,
    delete_session,
)
from .models import (
    AuthChallenge,
    AuthResponse,
    AuthResult,
    BootstrapRequest,
    CommandMessage,
    ConfirmationRequest,
    ConfirmationResponse,
    EnrollRequest,
    EnrollResponse,
    InboundEnvelope,
    ResultMessage,
    RotateSecretRequest,
    RotateSecretResponse,
    StatusMessage,
    VoiceRequestMessage,
    VoiceResponseMessage,
    VoicePartialMessage,
)
from .permissions import check_permission, get_command_tier

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HUB] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def sanitize_for_speech(text: Optional[str]) -> str:
    """Use the same generic internal-error redaction for text and voice delivery."""
    from .agent_harness import sanitize_for_speech as sanitize
    return sanitize(text)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
# device_id → active WebSocket (only authenticated devices)
CONNECTED_DEVICES: dict[str, WebSocket] = {}

# device_id → role (cached from DB on successful auth)
DEVICE_ROLES: dict[str, str] = {}

# cli.py connections — each entry is (websocket, device_id, role)
CLI_CONNECTIONS: list[tuple[WebSocket, str, str]] = []

# command_id → asyncio.Future for confirmation responses
PENDING_FUTURES: dict[str, asyncio.Future] = {}

# command_id → (voice_cmd_id, device_websocket) for post-approval voice reply
# When a Tier2/3 command executes after approval, we push a voice_response back
PENDING_VOICE_CALLBACKS: dict[str, tuple] = {}


# ---------------------------------------------------------------------------
# Peer-address filter middleware (default-deny)
# ---------------------------------------------------------------------------
class PeerFilterMiddleware:
    """ASGI middleware that rejects HTTP/WS connections from untrusted peers.

    This runs BEFORE any authentication logic.  Only localhost and
    Tailscale CGNAT (100.64.0.0/10) are allowed by default.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            peer_ip = client[0] if client else None

            if peer_ip and not is_trusted_peer(peer_ip):
                log.warning(f"Peer rejected (default-deny): {peer_ip}")
                if scope["type"] == "http":
                    response = JSONResponse(
                        status_code=403,
                        content={"detail": "Forbidden: untrusted network"},
                    )
                    await response(scope, receive, send)
                    return
                else:
                    # WebSocket — accept then immediately close with 4003
                    await send({"type": "websocket.close", "code": 4003})
                    return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# HTTP authentication helper
# ---------------------------------------------------------------------------
async def _authenticate_http_headers(
    x_cortex_device: str | None = None,
    x_cortex_secret: str | None = None,
    cortex_session: str | None = None,
) -> dict:
    """Validate X-Cortex-Device + X-Cortex-Secret headers or cortex_session cookie.
    Returns the device row dict.  Raises HTTPException on failure."""
    if cortex_session:
        session = await get_session(cortex_session)
        if session:
            device = await get_device(session["device_id"])
            if device:
                return device

    if not x_cortex_device or not x_cortex_secret:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Cortex-Device and X-Cortex-Secret headers, or valid session.",
        )
    device = await get_device(x_cortex_device)
    if not device:
        raise HTTPException(status_code=401, detail="Device not enrolled.")
    if not verify_secret(x_cortex_secret, device["secret_hash"]):
        raise HTTPException(status_code=401, detail="Invalid secret.")
    return device


_auth_wrapper = _authenticate_http_headers
# ---------------------------------------------------------------------------
# Periodic pruning task
# ---------------------------------------------------------------------------
_prune_task: asyncio.Task | None = None


async def _periodic_prune() -> None:
    """Delete old command_history rows every PRUNE_INTERVAL seconds."""
    while True:
        try:
            await asyncio.sleep(PRUNE_INTERVAL)
            deleted = await prune_command_history(COMMAND_HISTORY_RETENTION_HOURS)
            if deleted:
                log.info(f"Pruned {deleted} old command_history rows.")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error(f"Prune error: {exc}")


# ---------------------------------------------------------------------------
from .tasks import TaskEngine
task_engine: TaskEngine = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _prune_task
    global task_engine

    await init_db()
    from .integrations.ecosystem import IntegrationRegistry
    await IntegrationRegistry.get_instance().sync_connections()
    log.info("SQLite database initialised (migrations applied).")

    # Reset any stale connected states from prior ungraceful shutdowns
    stale_reset = await reset_stale_device_connections()
    if stale_reset:
        log.info(f"Reset {stale_reset} stale device connection state(s) to disconnected.")

    # Initial prune on startup
    deleted = await prune_command_history(COMMAND_HISTORY_RETENTION_HOURS)
    if deleted:
        log.info(f"Startup prune: removed {deleted} old command_history rows.")

    # Start periodic pruning
    _prune_task = asyncio.create_task(_periodic_prune())
    # Start Phase 5 Task Engine
    task_engine = TaskEngine({"CONNECTED_DEVICES": CONNECTED_DEVICES})
    await task_engine.start()

    from .control_layer import CortexControlLayer
    from .agent_harness import AgentHarness
    global _control_layer, _agent_harness
    _control_layer = CortexControlLayer(CONNECTED_DEVICES)
    _agent_harness = AgentHarness(_control_layer)
    from .attention_router import AttentionRouter
    AttentionRouter.get_instance().set_control_layer(_control_layer)
    await _agent_harness.task_orchestrator.reload_active_tasks()

    # Phase 8D Calendar & World State startup initialization
    try:
        from .world_state import WorldStateManager, CalendarSignalAdapter
        from .database import get_integrations
        wsm = WorldStateManager.get_instance()
        await wsm.load_from_db()

        integrations = await get_integrations()
        cal_active = any(i.get("plugin_name") == "calendar" and i.get("status") == "active" for i in integrations)
        if cal_active:
            conn_info = await CalendarSignalAdapter.get_connection_status()
            if conn_info.get("connected"):
                sigs = await CalendarSignalAdapter.fetch_and_create_signals(conn_info.get("account_id"))
                for s in sigs:
                    await wsm.ingest_signal(s)
                log.info(f"Google Calendar startup initialization complete: {len(sigs)} signals ingested.")
            else:
                log.info(f"Google Calendar integration status at startup: {conn_info.get('status')}")
    except Exception as exc:
        log.warning(f"Google Calendar startup initialization error: {exc}")

    # Phase 8F Learned Routines startup initialization
    try:
        from .routines import RoutineManager
        rm = RoutineManager.get_instance()
        await rm.load_from_db()
        await rm.update_world_state_signals()
    except Exception as exc:
        log.warning(f"Learned Routines startup initialization error: {exc}")

    # Phase A5: Executor Registry startup initialization
    try:
        from .executor_registry import ExecutorRegistry
        from .device_handle import DeviceHandle
        er = ExecutorRegistry.get_instance()
        enrolled_devs = await list_devices()
        for ed in enrolled_devs:
            caps = await get_device_capabilities(ed["device_id"])
            er.register_device(DeviceHandle.from_db_row(ed, live_online=False, live_capabilities=caps))
    except Exception as exc:
        log.warning(f"Executor Registry startup initialization error: {exc}")

    log.info("Cortex Hub is ready (Phase 6/7/8/9A/9B/A5). Waiting for agents…")
    yield

    if task_engine:
        await task_engine.stop()

    from .request_lifecycle import RequestLifecycle
    await RequestLifecycle.shutdown()

    if _prune_task:
        _prune_task.cancel()
        try:
            await _prune_task
        except asyncio.CancelledError:
            pass

    import hub.database as database
    await database.close_db()

    log.info("Hub shutting down.")


app = FastAPI(title="Cortex Hub", version="0.5.0", lifespan=lifespan)

# Add CORS Middleware for Control Center and web clients
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register the peer-filter middleware
app.add_middleware(PeerFilterMiddleware)

# Mount static files for Phase 4 Dashboard
import os
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the Cortex Control Center dashboard."""
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()

from pydantic import BaseModel
class LoginRequest(BaseModel):
    device_id: str
    secret: str

@app.post("/login")
async def login_endpoint(req: LoginRequest, response: Response):
    device = await get_device(req.device_id)
    if not device or not verify_secret(req.secret, device["secret_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
        
    token = await create_session(req.device_id)
    response.set_cookie(
        key="cortex_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # Set to true in prod
        max_age=7 * 24 * 3600
    )
    return {"status": "success", "role": device["role"]}

@app.post("/logout")
async def logout_endpoint(response: Response, cortex_session: str = Cookie(None)):
    if cortex_session:
        await delete_session(cortex_session)
    response.delete_cookie("cortex_session")
    return {"status": "success"}


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
async def _build_health_status() -> dict:
    from .task_orchestrator import _active_tasks
    import os
    
    devices_detail = {}
    for dev_id in list(CONNECTED_DEVICES.keys()):
        dev_row = await get_device(dev_id)
        devices_detail[dev_id] = {
            "authenticated": True,
            "role": DEVICE_ROLES.get(dev_id, dev_row["role"] if dev_row else "unknown"),
            "last_seen": dev_row["last_seen"] if dev_row else None,
        }

    model_name = os.environ.get("CORTEX_MODEL", "gemini-2.5-flash")
    provider_name = os.environ.get("CORTEX_LLM_PROVIDER", "gemini")

    return {
        "status": "running",
        "ready": True,
        "service": "cortex-hub",
        "version": "0.3.0",
        "connected_devices": list(CONNECTED_DEVICES.keys()),
        "devices_detail": devices_detail,
        "active_tasks_count": len(_active_tasks),
        "pending_approvals_count": len(PENDING_FUTURES),
        "current_model": model_name,
        "current_provider": provider_name,
    }

@app.get("/")
async def health():
    """Quick status check — shows connected devices and service metadata."""
    return await _build_health_status()

@app.get("/health")
async def health_endpoint():
    """Detailed runtime health check for GUI and external monitoring."""
    return await _build_health_status()

@app.get("/download/agent")
async def download_agent_package():
    """Serve lightweight agent bundle for enrolled nodes on the LAN."""
    from fastapi.responses import FileResponse
    from pathlib import Path
    zip_path = Path(__file__).resolve().parents[1] / "dist" / "cortex_lenovo.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Agent package not generated.")
    return FileResponse(str(zip_path), media_type="application/zip", filename="cortex_lenovo.zip")

@app.post("/runtime/sleep")
async def runtime_sleep_endpoint():
    """Trigger runtime sleep transition via Hub."""
    import urllib.request
    supervisor_port = int(os.environ.get("CORTEX_SUPERVISOR_PORT", "8008"))
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{supervisor_port}/sleep",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        await asyncio.to_thread(urllib.request.urlopen, req, timeout=2.0)
        return {"status": "success", "message": "Sleep requested from supervisor"}
    except Exception as exc:
        return {"status": "error", "message": f"Could not contact supervisor: {exc}"}

@app.get("/metrics")
async def metrics_endpoint():
    """Runtime latency and reliability metrics report (Phase 8R)."""
    from .metrics import MetricsCollector
    return MetricsCollector.get_instance().get_report()


@app.get("/devices")
async def devices_endpoint(
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    """Return all enrolled devices with their role, online status, and last_seen."""
    await _authenticate_http_headers(x_cortex_device, x_cortex_secret, cortex_session)

    enrolled = await list_devices()
    from .executor_registry import ExecutorRegistry
    er = ExecutorRegistry.get_instance()
    for d in enrolled:
        dev_id = d["device_id"]
        d["online"] = er.is_device_online(dev_id)
        d["capabilities"] = await get_device_capabilities(dev_id)
    return {"devices": enrolled}


@app.get("/devices/{device_id}")
async def device_detail_endpoint(
    device_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    """Return full device details, including capabilities and permissions."""
    await _authenticate_http_headers(x_cortex_device, x_cortex_secret, cortex_session)

    device = await get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found.")
    
    device["online"] = device_id in CONNECTED_DEVICES
    device["capabilities"] = await get_device_capabilities(device_id)
    device["permissions"] = await get_device_permissions(device_id)
    
    return {"device": device}

from pydantic import BaseModel
class PermissionUpdateRequest(BaseModel):
    capability: str
    decision: str

@app.post("/devices/{device_id}/permissions")
async def update_permission_endpoint(
    device_id: str,
    req: PermissionUpdateRequest,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    """Update a specific permission override for a device."""
    caller = await _authenticate_http_headers(x_cortex_device, x_cortex_secret, cortex_session)
    if caller["role"] not in ("admin", "primary_admin"):
        raise HTTPException(status_code=403, detail="Only admins can change permissions.")
    
    await set_device_permission(device_id, req.capability, req.decision)
    return {"status": "success"}


@app.post("/devices/{device_id}/disconnect")
async def disconnect_device_endpoint(
    device_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    """Admin-only endpoint to gracefully disconnect an active agent connection."""
    caller = await _authenticate_http_headers(x_cortex_device, x_cortex_secret, cortex_session)
    if caller["role"] not in ("admin", "primary_admin"):
        raise HTTPException(status_code=403, detail="Only admins can disconnect devices.")

    ws = CONNECTED_DEVICES.get(device_id)
    if not ws:
        return {"status": "not_connected", "device_id": device_id}

    await ws.close(code=4000, reason="Administrative disconnect")
    return {"status": "disconnected", "device_id": device_id}


@app.get("/logs")
async def recent_logs(
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    """Return the 20 most recent action log entries."""
    await _authenticate_http_headers(x_cortex_device, x_cortex_secret, cortex_session)

    rows = await get_recent_logs(20)
    return {"logs": rows}


@app.get("/pending")
async def pending_endpoint(
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    """Return all pending confirmations."""
    await _authenticate_http_headers(x_cortex_device, x_cortex_secret, cortex_session)

    rows = await get_pending_confirmations()
    return {"pending": rows}


class ResolveConfirmationRequest(BaseModel):
    decision: str  # "approve" or "deny"


@app.post("/pending/{command_id}/resolve")
async def resolve_pending_endpoint(
    command_id: str,
    req: ResolveConfirmationRequest,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    """Resolve a pending confirmation by command_id."""
    caller = await _authenticate_http_headers(x_cortex_device, x_cortex_secret, cortex_session)
    if caller["role"] not in ("admin", "primary_admin"):
        raise HTTPException(status_code=403, detail="Only admin can resolve confirmations.")

    if req.decision not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="Decision must be 'approve' or 'deny'.")

    success, msg = await _execute_resolve_confirmation(command_id, req.decision, caller["device_id"])
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg, "decision": req.decision}


# ---------------------------------------------------------------------------
# Bootstrap & Enrollment
# ---------------------------------------------------------------------------
@app.post("/bootstrap")
async def bootstrap_endpoint(req: BootstrapRequest, request: Request):
    """First-run only: create the admin device. Disabled after first admin exists.
    Restricted to localhost only (Phase 3)."""
    # Localhost-only check
    client_ip = request.client.host if request.client else None
    if not client_ip or not is_trusted_peer(client_ip):
        raise HTTPException(status_code=403, detail="Bootstrap is only allowed from localhost.")

    # Extra check: must be actual localhost, not just Tailscale
    import ipaddress as _ipa
    try:
        addr = _ipa.ip_address(client_ip)
        if isinstance(addr, _ipa.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if not addr.is_loopback:
            raise HTTPException(status_code=403, detail="Bootstrap is only allowed from localhost.")
    except ValueError:
        raise HTTPException(status_code=403, detail="Bootstrap is only allowed from localhost.")

    if await has_admin():
        raise HTTPException(status_code=403, detail="Admin already enrolled. Use /enroll instead.")

    secret = generate_device_secret()
    hashed = hash_secret(secret)
    await enroll_device(
        device_id=req.device_id,
        device_name=req.device_name or req.device_id,
        role="admin",
        secret_hash=hashed,
    )
    log.info(f"✦ BOOTSTRAP: Admin device '{req.device_id}' enrolled.")
    return EnrollResponse(
        device_id=req.device_id,
        device_name=req.device_name or req.device_id,
        role="admin",
        secret=secret,
    )


@app.post("/enroll")
async def enroll_endpoint(
    req: EnrollRequest,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    """Enroll a new device. Requires admin authentication via headers."""
    caller = await _authenticate_http_headers(x_cortex_device, x_cortex_secret, cortex_session)

    if caller["role"] not in ("admin", "primary_admin"):
        raise HTTPException(status_code=403, detail="Only admin devices can enroll new devices.")

    # Check if target device already exists
    existing = await get_device(req.device_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Device '{req.device_id}' is already enrolled as '{existing['role']}'.",
        )

    secret = generate_device_secret()
    hashed = hash_secret(secret)
    await enroll_device(
        device_id=req.device_id,
        device_name=req.device_name or req.device_id,
        role=req.role,
        secret_hash=hashed,
    )
    # Enrollment is immediately discoverable, but never implies an authenticated worker.
    from .executor_registry import ExecutorRegistry
    from .device_handle import DeviceHandle
    enrolled_row = await get_device(req.device_id)
    ExecutorRegistry.get_instance().register_device(DeviceHandle.from_db_row(
        enrolled_row, live_online=False, live_capabilities=[]))
    log.info(f"✦ ENROLLED: '{req.device_id}' as {req.role} (by {x_cortex_device})")
    return EnrollResponse(
        device_id=req.device_id,
        device_name=req.device_name or req.device_id,
        role=req.role,
        secret=secret,
    )


# ---------------------------------------------------------------------------
# Secret rotation (Phase 3)
# ---------------------------------------------------------------------------
@app.post("/rotate-secret")
async def rotate_secret_endpoint(
    req: RotateSecretRequest,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    caller = await _authenticate_http_headers(x_cortex_device, x_cortex_secret, cortex_session)

    if caller["role"] not in ("admin", "primary_admin"):
        raise HTTPException(status_code=403, detail="Only admin devices can rotate secrets.")

    target = await get_device(req.target_device_id)
    if not target:
        raise HTTPException(status_code=404, detail=f"Device '{req.target_device_id}' not enrolled.")

    new_secret = generate_device_secret()
    new_hash = hash_secret(new_secret)
    await rotate_device_secret(req.target_device_id, new_hash)

    log.info(f"✦ SECRET ROTATED: '{req.target_device_id}' (by {x_cortex_device})")
    return RotateSecretResponse(
        device_id=req.target_device_id,
        new_secret=new_secret,
    )


# ---------------------------------------------------------------------------
# Agent WebSocket — /ws/{device_id}
# ---------------------------------------------------------------------------
@app.websocket("/ws/{device_id}")
async def agent_endpoint(websocket: WebSocket, device_id: str):
    await websocket.accept()

    # ── Step 1: Authenticate ────────────────────────────────────────────
    device = await get_device(device_id)
    if not device:
        log.warning(f"Rejected unknown device: {device_id}")
        await websocket.send_text(
            AuthResult(success=False, message="Device not enrolled.").model_dump_json()
        )
        await websocket.close(code=4001, reason="Device not enrolled")
        return

    # Send challenge
    challenge = create_challenge()
    await websocket.send_text(
        AuthChallenge(challenge=challenge).model_dump_json()
    )

    # Wait for auth response
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT)
        data = json.loads(raw)
        auth_resp = AuthResponse(**data)
    except asyncio.TimeoutError:
        log.warning(f"Auth timeout for device: {device_id}")
        await websocket.send_text(
            AuthResult(success=False, message="Auth timeout.").model_dump_json()
        )
        await websocket.close(code=4002, reason="Auth timeout")
        return
    except Exception as exc:
        log.warning(f"Bad auth response from {device_id}: {exc}")
        await websocket.send_text(
            AuthResult(success=False, message="Invalid auth response.").model_dump_json()
        )
        await websocket.close(code=4003, reason="Invalid auth response")
        return

    # Verify: agent sends its raw secret, we bcrypt-check it
    if auth_resp.device_id != device_id or not verify_secret(auth_resp.response, device["secret_hash"]):
        log.warning(f"Auth FAILED for device: {device_id}")
        await websocket.send_text(
            AuthResult(success=False, message="Authentication failed.").model_dump_json()
        )
        await websocket.close(code=4004, reason="Authentication failed")
        return

    # A rejected advertisement must not replace or disconnect an existing executor.
    from .discovery import validate_advertisement
    try:
        validate_advertisement(device_id,auth_resp.discovery)
    except ValueError:
        await websocket.close(code=4003,reason='Invalid capability discovery')
        return

    # Phase A5: Register in ExecutorRegistry
    from .executor_registry import ExecutorRegistry
    ExecutorRegistry.get_instance().on_device_connected(device_id, websocket)
    ExecutorRegistry.get_instance().on_device_authenticated(
        device_id=device_id,
        capabilities=auth_resp.capabilities,
        platform=auth_resp.platform,
        device_type=getattr(auth_resp, "device_type", None) or device.get("device_type"),
        agent_version=getattr(auth_resp, "agent_version", None) or device.get("agent_version"),
        transports=getattr(auth_resp, "transports", None),
        role=device.get("role"),
        trust_level=device.get("trust_level"),
    )
    from .discovery import reconcile
    try:
        accepted = reconcile(ExecutorRegistry.get_instance(), device_id, auth_resp.discovery,
            legacy_names=auth_resp.capabilities)
        await update_device_capabilities(device_id, auth_resp.platform, accepted)
    except ValueError:
        ExecutorRegistry.get_instance().on_device_disconnected(device_id)
        await websocket.close(code=4003,reason='Invalid capability discovery')
        return
    await websocket.send_text(AuthResult(success=True,message='Authenticated.').model_dump_json())
    
    log.info(f"✓ Device authenticated: {device_id} (role={device['role']})")
    _er = ExecutorRegistry.get_instance()
    _last_hb = _er._last_heartbeat_time.get(device_id)
    _hb_age = _er.get_heartbeat_freshness(device_id)
    _is_onl = _er.is_device_online(device_id)
    log.info(
        f"[DIAGNOSTIC] executor_registered device_id={device_id} "
        f"websocket_connected=true "
        f"last_heartbeat={_last_hb} "
        f"heartbeat_age={_hb_age} "
        f"online={_is_onl}"
    )

    # ── Step 2: Handle duplicate connections ────────────────────────────
    old_ws = CONNECTED_DEVICES.get(device_id)
    if old_ws is not None:
        log.info(f"Replacing stale connection for {device_id}")
        try:
            await old_ws.close(code=4010, reason="Replaced by new connection")
        except Exception:
            pass  # old socket may already be dead

    CONNECTED_DEVICES[device_id] = websocket
    DEVICE_ROLES[device_id] = device["role"]
    await update_last_seen(device_id)
    await update_connection_state(device_id, "connected")
    log.info(f"Agent connected: {device_id}  (total: {len(CONNECTED_DEVICES)})")

    # Notify all CLI connections
    await _broadcast_cli({"event": "device_connected", "device_id": device_id, "role": device["role"]})

    # ── Step 3: Message loop ────────────────────────────────────────────
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                envelope = InboundEnvelope(**data)
            except Exception as exc:
                log.warning(f"[{device_id}] Malformed message: {exc}")
                continue

            try:
                if CONNECTED_DEVICES.get(device_id) is not websocket:
                    await websocket.close(code=4010,reason='Session replaced')
                    break
                if envelope.type == "heartbeat":
                    from .models import HeartbeatMessage
                    from .database import update_device_heartbeat
                    hb = HeartbeatMessage(**data)
                    if hb.device_id != device_id:
                        raise ValueError('Heartbeat identity mismatch')
                    log.info(f"[{device_id}] Heartbeat: state={hb.state} tasks={hb.active_task_count}")
                    await update_device_heartbeat(
                        device_id=device_id,
                        agent_version=hb.agent_version,
                        telemetry=hb.telemetry or ({"battery": hb.battery} if hb.battery else None),
                        workload={"active_task_count": hb.active_task_count},
                    )
                    ExecutorRegistry.get_instance().on_heartbeat(
                        device_id=device_id,
                        timestamp=hb.timestamp,
                        agent_version=hb.agent_version,
                        state=hb.state,
                        cpu_load=hb.cpu_load,
                        memory_load=hb.memory_load,
                        battery=hb.battery,
                        active_task_count=hb.active_task_count,
                        telemetry=hb.telemetry,
                    )
                elif envelope.type == "capability_advertisement":
                    from .models import CapabilityAdvertisementMessage
                    cap_msg = CapabilityAdvertisementMessage(**data)
                    if cap_msg.device_id != device_id:
                        raise ValueError('Advertisement identity mismatch')
                    log.info(f"[{device_id}] Capability advertisement: {len(cap_msg.capabilities)} caps")
                    accepted = reconcile(ExecutorRegistry.get_instance(),device_id,cap_msg.discovery,
                        legacy_names=cap_msg.capabilities)
                    await update_device_capabilities(device_id,cap_msg.platform or 'unknown',accepted)
                elif envelope.type == "status":
                    msg = StatusMessage(**data)
                    log.info(f"[{device_id}] Status: {msg.status}")
                    await update_last_seen(device_id)
                    ExecutorRegistry.get_instance().on_heartbeat(device_id, telemetry=msg.telemetry)
                    _er = ExecutorRegistry.get_instance()
                    _hb_age = _er.get_heartbeat_freshness(device_id)
                    _is_onl = _er.is_device_online(device_id)
                    log.info(
                        f"[DIAGNOSTIC] heartbeat_received device_id={device_id} "
                        f"timestamp={time.time()} "
                        f"heartbeat_age={_hb_age} "
                        f"online={_is_onl}"
                    )
                    if msg.telemetry:
                        await update_device_telemetry(device_id, msg.telemetry)
                        await _broadcast_cli({
                            "event": "telemetry_update",
                            "device_id": device_id,
                            "telemetry": msg.telemetry
                        })

                elif envelope.type == "result":
                    msg = ResultMessage(**data)
                    from .discovery import record_verified_result
                    await record_verified_result(device_id,msg)
                    icon = "✓" if msg.status == "success" else "✗"
                    log.info(
                        f"[{device_id}] {icon} {msg.command} → {msg.status}: {msg.message}"
                    )
                    await log_action(
                        device_id=device_id,
                        command=msg.command,
                        result=msg.status,
                        message=msg.message,
                        command_id=msg.command_id,
                    )
                    # Forward result to CLI listeners
                    await _broadcast_cli(
                        {
                            "event": "result",
                            "device_id": device_id,
                            "command_id": msg.command_id,
                            "command": msg.command,
                            "status": msg.status,
                            "message": msg.message,
                        }
                    )
                    
                    # Ground screenshot reference into active session
                    if (msg.command in ("screen.capture", "take_screenshot") or "screenshot" in msg.command.lower() or "Screenshot saved: " in msg.message or "Screenshot taken: " in msg.message) and msg.status == "success":
                        img_ref = None
                        if isinstance(msg.data, dict) and msg.data.get("image_ref"):
                            img_ref = msg.data["image_ref"]
                        elif "Screenshot saved: " in msg.message:
                            img_ref = msg.message.split("Screenshot saved: ", 1)[1].strip()
                        elif "Screenshot taken: " in msg.message:
                            img_ref = msg.message.split("Screenshot taken: ", 1)[1].strip()
                        if img_ref:
                            log.info(f"[{device_id}] Persisting screenshot reference from ResultMessage: {img_ref}")
                            from .session_context import SessionContextManager
                            await SessionContextManager.set_screenshot_ref(device_id, img_ref)

                    fut = PENDING_FUTURES.get(msg.command_id)
                    log.info(f"Hub ResultMessage handler | cmd_id={msg.command_id[:8]}... | fut_exists={bool(fut)} | fut_done={fut.done() if fut else False}")
                    if fut and not fut.done():
                        fut.set_result(msg)
                    else:
                        # Post-approval path: push a voice_response to the original voice requester
                        voice_cb = PENDING_VOICE_CALLBACKS.pop(msg.command_id, None)
                        if voice_cb:
                            voice_cmd_id, requester_ws = voice_cb
                            spoken = sanitize_for_speech(msg.message) or ("Done." if msg.status == "success" else "The command failed.")
                            resp = VoiceResponseMessage(
                                command_id=voice_cmd_id,
                                message=spoken,
                                summary=None,
                                status=msg.status,
                                is_post_approval=True,
                                latency_metrics={}
                            )
                            try:
                                await requester_ws.send_text(resp.model_dump_json())
                                log.info(f"[{device_id}] Post-approval voice reply sent | result={msg.status} | msg={spoken[:80]}")
                            except Exception as exc:
                                log.warning(f"Failed to push post-approval voice result: {exc}")
                elif envelope.type == "voice_partial":
                    msg = VoicePartialMessage(**data)
                    log.info(f"[{device_id}] Voice Partial (cmd_id: {msg.command_id}): {msg.text}")
                    # Future: implement speculative LLM generation here.
                elif envelope.type == "voice_request":
                    # Preparation must not block command results, approvals or heartbeats.
                    msg = VoiceRequestMessage(**data)
                    job = asyncio.create_task(_process_voice_request(websocket, device_id, msg))
                    _voice_request_handlers.add(job)
                    job.add_done_callback(_voice_request_done)
                elif envelope.type == "confirmation_response":
                    await _handle_confirmation_response(websocket, data, device_id)
                else:
                    log.warning(f"[{device_id}] Unknown message type: {envelope.type}")
            except Exception as e:
                if envelope.type == 'capability_advertisement':
                    log.warning('discovery_rejected device=%s reason=%s',device_id,type(e).__name__)
                    await websocket.send_text(json.dumps({'type':'discovery_rejected','reason':'invalid_advertisement'}))
                    continue
                import traceback
                log.error(f"HUB CRASH ROOT CAUSE | Exception in websocket message loop: {e}\n{traceback.format_exc()}")

    except WebSocketDisconnect:
        pass
    finally:
        if CONNECTED_DEVICES.get(device_id) is websocket:
            CONNECTED_DEVICES.pop(device_id, None)
            DEVICE_ROLES.pop(device_id, None)
            from .executor_registry import ExecutorRegistry
            ExecutorRegistry.get_instance().on_device_disconnected(device_id)
            from .task_orchestrator import TaskOrchestrator
            TaskOrchestrator(None, None).cancel_task_for_device(device_id)
            
            await update_connection_state(device_id, "disconnected")
            log.info(f"Agent disconnected: {device_id}  (total: {len(CONNECTED_DEVICES)})")
            await _broadcast_cli({"event": "device_disconnected", "device_id": device_id})


# ---------------------------------------------------------------------------
# CLI WebSocket — /ws-cli (authenticated since Phase 3)
# ---------------------------------------------------------------------------
_voice_request_handlers = set()


def _voice_request_done(job):
    _voice_request_handlers.discard(job)
    if not job.cancelled() and job.exception():
        log.warning("voice_request_delivery_failed reason=%s", type(job.exception()).__name__)


async def _process_voice_request(websocket, device_id, msg):
    from .context import ContextManager
    from .speakers import SpeakerManager
    from .request_lifecycle import RequestLifecycle
    async def prepare():
        speaker_hint = msg.speaker_id or msg.speaker_label or (msg.speaker_metadata or {}).get("speaker")
        speaker = await SpeakerManager.resolve_speaker(speaker_hint=speaker_hint, audio_metadata=msg.speaker_metadata)
        return await ContextManager.get_active_context(device_id=device_id, request=msg.text, speaker_info=speaker)
    async def deliver(task_id, result):
        target = CONNECTED_DEVICES.get(device_id) or websocket
        await target.send_text(VoiceResponseMessage(command_id=task_id, request_id=msg.command_id,
            task_id=task_id, is_task_result=True, status=result.status,
            message=sanitize_for_speech(result.message), data=result.data,
            latency_metrics=result.latency_metrics or {}).model_dump_json())
    async def audit(src, intent, status, detail):
        await log_action(device_id=src, command=intent, result=status, message=detail, command_id=msg.command_id)
    result = await RequestLifecycle.submit(device_id, msg.command_id, msg.text, prepare, _agent_harness,
        channel="voice", deliver=deliver, latency_metrics=msg.latency_metrics, log_action_callback=audit)
    await websocket.send_text(VoiceResponseMessage(command_id=msg.command_id, request_id=msg.command_id,
        task_id=(result.data or {}).get("task_id"), status=result.status,
        message=sanitize_for_speech(result.message), data=result.data,
        latency_metrics=result.latency_metrics or {}).model_dump_json())


@app.websocket("/ws-cli")
async def cli_endpoint(websocket: WebSocket):
    """
    The CLI (cli.py) connects here to send commands and receive live events.

    Phase 3: CLI must authenticate with device_id + secret before receiving
    any data or sending commands.  The role comes from the DB, not hard-coded.
    """
    await websocket.accept()

    # ── Step 1: Authenticate the CLI ────────────────────────────────────
    challenge = create_challenge()
    await websocket.send_text(
        AuthChallenge(challenge=challenge).model_dump_json()
    )

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT)
        data = json.loads(raw)
        auth_resp = AuthResponse(**data)
    except asyncio.TimeoutError:
        log.warning("CLI auth timeout")
        await websocket.send_text(
            AuthResult(success=False, message="Auth timeout.").model_dump_json()
        )
        await websocket.close(code=4002, reason="Auth timeout")
        return
    except Exception as exc:
        log.warning(f"CLI bad auth response: {exc}")
        await websocket.send_text(
            AuthResult(success=False, message="Invalid auth response.").model_dump_json()
        )
        await websocket.close(code=4003, reason="Invalid auth response")
        return

    device = await get_device(auth_resp.device_id)
    if not device:
        log.warning(f"CLI auth: unknown device '{auth_resp.device_id}'")
        await websocket.send_text(
            AuthResult(success=False, message="Device not enrolled.").model_dump_json()
        )
        await websocket.close(code=4001, reason="Device not enrolled")
        return

    if not verify_secret(auth_resp.response, device["secret_hash"]):
        log.warning(f"CLI auth FAILED for device: {auth_resp.device_id}")
        await websocket.send_text(
            AuthResult(success=False, message="Authentication failed.").model_dump_json()
        )
        await websocket.close(code=4004, reason="Authentication failed")
        return

    cli_device_id = auth_resp.device_id
    cli_role = device["role"]

    await websocket.send_text(
        AuthResult(success=True, message="Authenticated.", role=cli_role).model_dump_json()
    )
    log.info(f"CLI authenticated: {cli_device_id} (role={cli_role})")

    # ── Step 2: Send device list ────────────────────────────────────────
    CLI_CONNECTIONS.append((websocket, cli_device_id, cli_role))

    enrolled = await list_devices()
    online_ids = set(CONNECTED_DEVICES.keys())
    device_list = []
    for d in enrolled:
        device_list.append({
            "device_id": d["device_id"],
            "device_name": d["device_name"],
            "role": d["role"],
            "online": d["device_id"] in online_ids,
        })

    await websocket.send_text(
        json.dumps({
            "event": "device_list",
            "devices": device_list,
        })
    )

    # ── Step 3: Message loop ────────────────────────────────────────────
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps({"event": "error", "message": "Invalid JSON."})
                )
                continue

            # ── Handle confirmation responses ────────────────────────────
            msg_type = data.get("type", "")
            if msg_type == "confirmation_response":
                if cli_role not in ("admin", "primary_admin"):
                    await websocket.send_text(
                        json.dumps({"event": "error", "message": "Only admin can approve/deny confirmations."})
                    )
                    continue
                await _handle_confirmation_response(websocket, data, cli_device_id)
                continue

            # ── Handle command requests ──────────────────────────────────
            device_id = data.get("device_id", "")
            command = data.get("command", "")
            args = data.get("args", [])

            if not device_id or not command:
                await websocket.send_text(
                    json.dumps(
                        {"event": "error", "message": "Missing device_id or command."}
                    )
                )
                continue

            # Use the authenticated role from the DB
            sender_role = cli_role
            sender_id = cli_device_id

            # ── Permission check ─────────────────────────────────────────
            perm = await check_permission(sender_role, sender_id, device_id, command)

            if not perm.allowed:
                await websocket.send_text(
                    json.dumps({
                        "event": "error",
                        "message": f"Permission denied: {perm.reason}",
                    })
                )
                await log_action(
                    device_id=device_id,
                    command=command,
                    result="denied",
                    message=perm.reason,
                )
                continue

            # ── Needs confirmation? ──────────────────────────────────────
            if perm.needs_confirmation:
                command_id = uuid4().hex

                # Record in command_history (dedup + expiry)
                if not await record_command(command_id, command, device_id, sender_id):
                    await websocket.send_text(
                        json.dumps({"event": "error", "message": "Duplicate command_id."})
                    )
                    continue

                asyncio.create_task(_start_confirmation_flow(
                    websocket=websocket,
                    command_id=command_id,
                    target_device=device_id,
                    command=command,
                    args=args,
                    requested_by=sender_id,
                    tier=perm.tier,
                ))
                continue

            # ── Immediate execution (Tier 1) ─────────────────────────────
            target_ws = CONNECTED_DEVICES.get(device_id)
            if target_ws is None:
                await websocket.send_text(
                    json.dumps({
                        "event": "error",
                        "message": f"Device '{device_id}' is not connected.",
                    })
                )
                continue

            cmd_msg = CommandMessage(command=command, args=args)

            # Record in command_history (dedup + expiry)
            if not await record_command(cmd_msg.command_id, command, device_id, sender_id):
                await websocket.send_text(
                    json.dumps({"event": "error", "message": "Duplicate command_id."})
                )
                continue

            try:
                from .discovery import guard_dispatch
                await guard_dispatch(device_id,command)
                await target_ws.send_text(cmd_msg.model_dump_json())
                await update_command_status(cmd_msg.command_id, "dispatched")
                log.info(f"CLI[{sender_id}] → [{device_id}] command: {command} {args} (id={cmd_msg.command_id[:8]}…)")
                await websocket.send_text(
                    json.dumps({
                        "event": "sent",
                        "device_id": device_id,
                        "command": command,
                        "command_id": cmd_msg.command_id,
                        "args": args,
                    })
                )
            except Exception as exc:
                await websocket.send_text(
                    json.dumps({"event": "error", "message": str(exc)})
                )

    except WebSocketDisconnect:
        pass
    finally:
        CLI_CONNECTIONS[:] = [(ws, did, r) for ws, did, r in CLI_CONNECTIONS if ws is not websocket]
        log.info(f"CLI client disconnected: {cli_device_id}")


# ---------------------------------------------------------------------------
# Confirmation flow
# ---------------------------------------------------------------------------

async def _start_confirmation_flow(
    websocket: WebSocket,
    command_id: str,
    target_device: str,
    command: str,
    args: list[str],
    requested_by: str,
    tier: int,
) -> None:
    """Initiate a confirmation flow for a Tier 2/3 command."""
    # Store in DB
    await create_confirmation(
        command_id=command_id,
        target_device=target_device,
        command=command,
        args=args,
        requested_by=requested_by,
        tier=tier,
    )

    # Create a future for the response
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    PENDING_FUTURES[command_id] = future

    # Notify all admin CLI sessions
    confirmation_req = ConfirmationRequest(
        command_id=command_id,
        target_device=target_device,
        command=command,
        args=args,
        requested_by=requested_by,
        tier=tier,
        timeout_seconds=CONFIRMATION_TIMEOUT,
    )
    await _broadcast_cli({
        "event": "confirmation_request",
        **confirmation_req.model_dump(),
    })

    log.info(
        f"⏳ Confirmation required: {command} on {target_device} "
        f"(tier={tier}, id={command_id[:8]}…, timeout={CONFIRMATION_TIMEOUT}s)"
    )

    # Wait for the decision (with timeout)
    try:
        decision = await asyncio.wait_for(future, timeout=CONFIRMATION_TIMEOUT)
    except asyncio.TimeoutError:
        decision = "expired"
        await resolve_confirmation(command_id, "expired", "system")
        await update_command_status(command_id, "expired")
        log.info(f"⏰ Confirmation expired: {command_id[:8]}…")
    finally:
        PENDING_FUTURES.pop(command_id, None)

    if decision == "approve":
        # Check command expiry (hub receipt time)
        if await is_command_expired(command_id, COMMAND_TTL):
            await update_command_status(command_id, "expired")
            await log_action(
                device_id=target_device,
                command=command,
                result="expired",
                message=f"Approved but command expired (older than {COMMAND_TTL}s)",
                command_id=command_id,
            )
            await websocket.send_text(
                json.dumps({
                    "event": "error",
                    "message": f"Command expired (older than {COMMAND_TTL}s).",
                    "command_id": command_id,
                })
            )
            return

        # Execute the command
        target_ws = CONNECTED_DEVICES.get(target_device)
        if target_ws is None:
            await log_action(
                device_id=target_device,
                command=command,
                result="failure",
                message=f"Approved but device '{target_device}' no longer connected",
                command_id=command_id,
            )
            await websocket.send_text(
                json.dumps({
                    "event": "error",
                    "message": f"Approved, but device '{target_device}' is no longer connected.",
                })
            )
            return

        cmd_msg = CommandMessage(command_id=command_id, command=command, args=args)
        try:
            from .discovery import guard_dispatch
            await guard_dispatch(target_device,command)
            await target_ws.send_text(cmd_msg.model_dump_json())
            await update_command_status(command_id, "dispatched")
            log.info(f"✓ Confirmed → [{target_device}] {command} (id={command_id[:8]}…)")
            await websocket.send_text(
                json.dumps({
                    "event": "confirmed_and_sent",
                    "command_id": command_id,
                    "device_id": target_device,
                    "command": command,
                    "args": args,
                })
            )
        except Exception as exc:
            await websocket.send_text(
                json.dumps({"event": "error", "message": str(exc)})
            )

    elif decision == "deny":
        await log_action(
            device_id=target_device,
            command=command,
            result="denied",
            message="Confirmation denied by admin",
            command_id=command_id,
        )
        await websocket.send_text(
            json.dumps({
                "event": "confirmation_denied",
                "command_id": command_id,
                "command": command,
                "device_id": target_device,
            })
        )
        log.info(f"✗ Confirmation denied: {command} on {target_device} (id={command_id[:8]}…)")

    elif decision == "expired":
        await log_action(
            device_id=target_device,
            command=command,
            result="expired",
            message=f"Confirmation expired after {CONFIRMATION_TIMEOUT}s",
            command_id=command_id,
        )
        await websocket.send_text(
            json.dumps({
                "event": "confirmation_expired",
                "command_id": command_id,
                "command": command,
                "device_id": target_device,
                "message": f"No response within {CONFIRMATION_TIMEOUT}s. Command cancelled.",
            })
        )


async def _resolve_command_id(prefix: str) -> str | None:
    """Resolve a short command_id prefix to the full ID from PENDING_FUTURES or DB."""
    matches = [k for k in PENDING_FUTURES if k.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]

    pending_rows = await get_pending_confirmations()
    exact = [r["command_id"] for r in pending_rows if r["command_id"] == prefix]
    if exact:
        return exact[0]
    db_matches = [r["command_id"] for r in pending_rows if r["command_id"].startswith(prefix)]
    if len(db_matches) == 1:
        return db_matches[0]
    return None


async def _execute_resolve_confirmation(command_id: str, decision: str, resolver_id: str) -> tuple[bool, str]:
    """Execute approval or denial of a pending confirmation."""
    full_id = command_id
    from .database import get_confirmation
    confirmation = await get_confirmation(full_id)
    if not confirmation and len(command_id) < 32:
        full_id = await _resolve_command_id(command_id) or command_id
        confirmation = await get_confirmation(full_id)
    if confirmation and confirmation.get("command") == "model.share":
        if confirmation.get("requested_by") != resolver_id:
            return False, "Only the requesting device can approve this cloud-sharing request."
        if full_id not in PENDING_FUTURES or PENDING_FUTURES[full_id].done():
            await resolve_confirmation(full_id, "expired", "system")
            return False, "This cloud-sharing request is no longer active."
        decision = {"approve": "approved", "deny": "denied"}.get(decision, decision)
        if decision not in ("approved", "denied"):
            return False, "Choose approve or deny."
    row = await resolve_confirmation(full_id, decision, resolver_id)
    if not row and len(command_id) < 32:
        resolved = await _resolve_command_id(command_id)
        if resolved:
            full_id = resolved
            row = await resolve_confirmation(full_id, decision, resolver_id)

    if not row:
        return False, f"No pending confirmation with id '{full_id[:8]}…' (may have expired or already resolved)."

    future = PENDING_FUTURES.get(full_id)
    if future and not future.done():
        future.set_result(decision)
        log.info(f"Confirmation {decision}d (future signaled): {full_id[:8]}…")
    else:
        log.info(f"Confirmation {decision}d (recorded in DB): {full_id[:8]}…")

    return True, f"Confirmation {decision}d successfully."


async def _handle_confirmation_response(websocket: WebSocket, data: dict, resolver_id: str) -> None:
    """Process an approve/deny response from the CLI."""
    try:
        resp = ConfirmationResponse(**data)
    except Exception as exc:
        await websocket.send_text(
            json.dumps({"event": "error", "message": f"Invalid confirmation response: {exc}"})
        )
        return

    success, msg = await _execute_resolve_confirmation(resp.command_id, resp.decision, resolver_id)
    if not success:
        await websocket.send_text(
            json.dumps({"event": "error", "message": msg})
        )


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------
async def _broadcast_cli(payload: dict) -> None:
    """Send a JSON payload to all connected CLI sessions."""
    text = json.dumps(payload)
    dead: list[tuple[WebSocket, str, str]] = []
    for entry in CLI_CONNECTIONS:
        ws, _, _ = entry
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(entry)
    for entry in dead:
        CLI_CONNECTIONS.remove(entry)

from .api_tasks import router as tasks_router
from .api_memory import router as memory_router
from .api_integrations import router as integrations_router
from .api_continuity import router as continuity_router
from .api_conversation import router as conversation_router
from .api_people import router as people_router
from .api_speakers import router as speakers_router

app.include_router(tasks_router)
app.include_router(memory_router)
app.include_router(integrations_router)
app.include_router(continuity_router)
app.include_router(conversation_router)
app.include_router(people_router)
app.include_router(speakers_router)

# Phase A9: Control Center Router & WebSocket
from .api_control import router as control_router, control_websocket_endpoint
app.include_router(control_router)
app.add_api_websocket_route("/ws-control", control_websocket_endpoint)

from .api_workspace import router as workspace_router
from .api_discovery import router as discovery_router
app.include_router(discovery_router)
app.include_router(workspace_router)

