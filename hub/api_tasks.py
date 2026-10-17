"""
hub/api_tasks.py
----------------
REST API for managing tasks.
"""

import uuid
from typing import List, Optional
from fastapi import APIRouter, Header, HTTPException, Depends, Cookie
from pydantic import BaseModel
from .database import get_tasks, get_task, add_task, update_task


def _auth_wrapper(*args, **kwargs):
    from hub.main import _auth_wrapper
    return _auth_wrapper(*args, **kwargs)


def _auth_wrapper(*args, **kwargs):
    from hub.main import _auth_wrapper
    return _auth_wrapper(*args, **kwargs)

router = APIRouter(prefix="/tasks", tags=["tasks"])

class TaskCreate(BaseModel):
    title: str
    type: str
    trigger_time: str = ""
    cron_expression: str = ""
    timezone: str = "UTC"
    assigned_device: str = ""
    command: str
    args: list = []
    required_capability: str = ""
    reroutable: bool = False
    retry_count: int = 0
    max_retries: int = 0

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    assigned_device: Optional[str] = None

@router.get("/")
async def list_tasks(
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    return {"tasks": await get_tasks()}

@router.post("/")
async def create_task(
    task: TaskCreate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    task_id = uuid.uuid4().hex
    
    task_data = task.model_dump()
    task_data["id"] = task_id
    task_data["status"] = "pending"
    
    await add_task(task_data)
    return {"id": task_id, "status": "success"}

@router.get("/{task_id}")
async def retrieve_task(
    task_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@router.put("/{task_id}")
async def update_task_api(
    task_id: str,
    updates: TaskUpdate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    
    update_data = {k: v for k, v in updates.model_dump().items() if v is not None}
    if not update_data:
        return {"status": "no changes"}
        
    success = await update_task(task_id, update_data)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "success"}

@router.post("/{task_id}/pause")
async def pause_task(
    task_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    success = await update_task(task_id, {"status": "paused"})
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "success"}

@router.post("/{task_id}/resume")
async def resume_task(
    task_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    success = await update_task(task_id, {"status": "pending"})
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "success"}
