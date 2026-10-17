"""
hub/api_speakers.py
-------------------
Phase 7B: REST API for speaker profiles and multi-speaker session context.
"""

from fastapi import APIRouter, Header, Cookie, HTTPException
from typing import Optional, List, Dict, Any
from pydantic import BaseModel

from .models import SpeakerProfile, SpeakerProfileCreate, SpeakerProfileUpdate
from .speakers import SpeakerManager

router = APIRouter(prefix="/speakers", tags=["speakers"])


def _auth_wrapper(*args, **kwargs):
    from hub.main import _auth_wrapper
    return _auth_wrapper(*args, **kwargs)


class LinkPersonRequest(BaseModel):
    person_id: Optional[str] = None


@router.get("/current", response_model=Dict[str, Any])
async def get_current_speaker(
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    caller = await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    device_id = caller["device_id"]
    spk = await SpeakerManager.get_current_speaker(device_id)
    return spk


@router.get("/recent", response_model=List[Dict[str, Any]])
async def list_recent_speakers(
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    caller = await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    device_id = caller["device_id"]
    return await SpeakerManager.list_recent_speakers(device_id)


@router.get("/", response_model=List[Dict[str, Any]])
async def list_speakers(
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    return await SpeakerManager.list_speakers()


@router.post("/", response_model=Dict[str, Any])
async def create_speaker(
    req: SpeakerProfileCreate,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    spk = await SpeakerManager.create_speaker(
        label=req.label,
        is_primary=req.is_primary_user,
        person_id=req.person_id,
        confidence=req.confidence,
        metadata=req.metadata,
        speaker_id=req.speaker_id
    )
    return spk


@router.get("/{speaker_id}", response_model=Dict[str, Any])
async def get_speaker(
    speaker_id: str,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    spk = await SpeakerManager.get_speaker(speaker_id)
    if not spk:
        raise HTTPException(status_code=404, detail="Speaker profile not found")
    return spk


@router.post("/{speaker_id}/link", response_model=Dict[str, Any])
async def link_speaker_to_person(
    speaker_id: str,
    req: LinkPersonRequest,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    updated = await SpeakerManager.link_person(speaker_id, req.person_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Speaker profile or person not found")
    return updated
