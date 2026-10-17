"""
hub/api_conversation.py
-----------------------
Phase 6: Conversational API endpoint.
"""

from fastapi import APIRouter, Header, Cookie
from pydantic import BaseModel
from typing import Optional
from .context import ContextManager
from .agent_harness import AgentHarness
from .control_layer import CortexControlLayer
from .database import log_action


def _auth_wrapper(*args, **kwargs):
    from hub.main import _auth_wrapper
    return _auth_wrapper(*args, **kwargs)

router = APIRouter(prefix="/conversation", tags=["conversation"])

class ConversationRequest(BaseModel):
    natural_text: str
    active_task_id: Optional[str] = None
    related_entities: Optional[list[str]] = None
    speaker_id: Optional[str] = None
    speaker_label: Optional[str] = None

@router.post("/")
async def handle_conversation(
    req: ConversationRequest,
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    caller = await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    source_device_id = caller["device_id"]
    
    # Phase 7B: Resolve speaker
    from .speakers import SpeakerManager
    speaker_hint = req.speaker_id or req.speaker_label
    resolved_speaker = await SpeakerManager.resolve_speaker(speaker_hint=speaker_hint)

    # 1. Context Assembly
    context = await ContextManager.get_active_context(
        device_id=source_device_id,
        request=req.natural_text,
        active_task_id=req.active_task_id,
        related_entities=req.related_entities,
        speaker_info=resolved_speaker
    )
    
    import hub.main as hub_main
    harness = hub_main._agent_harness
    if harness is None:
        control_layer = CortexControlLayer(hub_main.CONNECTED_DEVICES)
        harness = AgentHarness(control_layer)

    
    async def _log_action_cb(src, intent, status, detail):
        await log_action(device_id=src, command=intent, result=status, message=detail, command_id="")

    # 2. Conversational Pipeline -> Agent Harness
    result = await harness.process_request(
        source_device_id=source_device_id,
        natural_request=req.natural_text,
        context=context,
        log_action_callback=_log_action_cb
    )
    
    from .session_context import SessionContextManager
    await SessionContextManager.update_session(
        source_device_id,
        req.natural_text,
        {"status": result.status, "message": result.message},
        result.action_plan.model_dump() if result.action_plan else None,
        speaker_info=resolved_speaker
    )

    # We return the action plan along with the conversational message
    return result.model_dump()
