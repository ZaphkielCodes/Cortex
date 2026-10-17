"""
hub/api_continuity.py
---------------------
REST API for managing Phase 6 continuity sessions.
"""

import uuid
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Cookie
from pydantic import BaseModel
from .database import (
    create_continuity_session,
    get_continuity_sessions,
    get_continuity_session,
    update_continuity_session,
    get_device_capabilities,
    get_device
)
from .models import ContinuitySessionCreate, ContinuitySessionUpdate
import json


def _auth_wrapper(*args, **kwargs):
    from hub.main import _auth_wrapper
    return _auth_wrapper(*args, **kwargs)

router = APIRouter(prefix="/continuity", tags=["continuity"])

@router.get("/")
async def list_continuity_sessions(
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    sessions = await get_continuity_sessions()
    # parse JSON fields
    for s in sessions:
        s["required_capabilities"] = json.loads(s["required_capabilities"])
        s["last_known_state"] = json.loads(s["last_known_state"])
        s["continuation_metadata"] = json.loads(s["continuation_metadata"])
    return {"sessions": sessions}

@router.post("/")
async def create_session(
    session: ContinuitySessionCreate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    session_id = uuid.uuid4().hex
    
    session_data = session.model_dump()
    session_data["id"] = session_id
    
    await create_continuity_session(session_data)
    return {"id": session_id, "status": "success"}

@router.get("/{session_id}")
async def retrieve_session(
    session_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    session = await get_continuity_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Continuity session not found")
        
    session["required_capabilities"] = json.loads(session["required_capabilities"])
    session["last_known_state"] = json.loads(session["last_known_state"])
    session["continuation_metadata"] = json.loads(session["continuation_metadata"])
    
    return session

@router.put("/{session_id}")
async def update_session_api(
    session_id: str,
    updates: ContinuitySessionUpdate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    
    update_data = {k: v for k, v in updates.model_dump().items() if v is not None}
    if not update_data:
        return {"status": "no changes"}
        
    success = await update_continuity_session(session_id, update_data)
    if not success:
        raise HTTPException(status_code=404, detail="Continuity session not found")
    return {"status": "success"}

class ResumeRequest(BaseModel):
    target_device: str

@router.post("/{session_id}/resume")
async def resume_session(
    session_id: str,
    req: ResumeRequest,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    caller = await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    
    # 1. Fetch session
    session = await get_continuity_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    # 2. Check target device exists
    target_dev = await get_device(req.target_device)
    if not target_dev:
        raise HTTPException(status_code=404, detail="Target device not found")
        
    # 3. Capability check
    req_caps = json.loads(session["required_capabilities"])
    target_caps = await get_device_capabilities(req.target_device)
    
    missing_caps = [cap for cap in req_caps if cap not in target_caps]
    if missing_caps:
        raise HTTPException(
            status_code=400, 
            detail=f"Target device '{req.target_device}' missing capabilities: {missing_caps}"
        )
        
    # 4. In a real system, we would also check permissions using check_permission() 
    # here to ensure caller has rights to resume this on the target device.
    
    # 5. Update session's current device
    success = await update_continuity_session(session_id, {"current_device": req.target_device})
    
    # Optional: Send WS CommandMessage to the target device to actually trigger the UI/app
    from .models import CommandMessage
    from .main import CONNECTED_DEVICES
    target_ws = CONNECTED_DEVICES.get(req.target_device)
    if target_ws:
        cmd = CommandMessage(
            command="resume_session",
            args=[session_id, session["active_task"], session["active_project"], session["active_file"]]
        )
        await target_ws.send_text(cmd.model_dump_json())
    
    return {"status": "success", "message": f"Resumed on {req.target_device}"}
