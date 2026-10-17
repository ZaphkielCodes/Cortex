"""
hub/api_memory.py
-----------------
REST API for managing memory items.
"""

import uuid
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Cookie
from pydantic import BaseModel
from .database import get_memory_items, add_memory_item, update_memory_item


def _auth_wrapper(*args, **kwargs):
    from hub.main import _auth_wrapper
    return _auth_wrapper(*args, **kwargs)

router = APIRouter(prefix="/memory", tags=["memory"])

class MemoryCreate(BaseModel):
    category: str
    key: str
    value: str
    source: str = "user"
    confidence: float = 1.0
    relationship_entity_id: str = ""

class MemoryUpdate(BaseModel):
    value: Optional[str] = None
    confidence: Optional[float] = None

@router.get("/")
async def list_memory(
    category: Optional[str] = None,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    return {"memory": await get_memory_items(category)}

@router.post("/")
async def create_memory(
    item: MemoryCreate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    mem_id = uuid.uuid4().hex
    
    mem_data = item.model_dump()
    mem_data["id"] = mem_id
    
    await add_memory_item(mem_data)
    return {"id": mem_id, "status": "success"}

@router.put("/{mem_id}")
async def update_memory(
    mem_id: str,
    updates: MemoryUpdate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    
    update_data = {k: v for k, v in updates.model_dump().items() if v is not None}
    if not update_data:
        return {"status": "no changes"}
        
    success = await update_memory_item(mem_id, update_data)
    if not success:
        raise HTTPException(status_code=404, detail="Memory item not found")
    return {"status": "success"}
