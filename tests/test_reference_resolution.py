import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from hub.session_context import SessionContextManager
from hub.context import ContextManager
from hub.database import get_session_context, save_session_context

@pytest.mark.asyncio
async def test_session_context_bounding():
    device_id = "test_dev_1"
    # Seed with 15 turns
    turns = [{"request": f"req_{i}", "response_status": "success", "response_message": "ok"} for i in range(15)]
    await save_session_context(device_id, turns, {})
    
    # Add one more via manager
    await SessionContextManager.update_session(device_id, "new_req", {"status": "success", "message": "done"})
    
    session = await SessionContextManager.get_session(device_id)
    # Should be bounded to MAX_TURNS (10)
    assert len(session["turns"]) == 10
    assert session["turns"][-1]["request"] == "new_req"
    assert session["turns"][0]["request"] == "req_6" # 15 + 1 = 16, keep last 10, so 6 to 15 (which is 15th turn = "new_req")

@pytest.mark.asyncio
async def test_session_references():
    device_id = "test_dev_2"
    await save_session_context(device_id, [], {})
    
    plan = {
        "intent": "open_app",
        "action": "open_app",
        "arguments": {"app": "calculator"}
    }
    
    await SessionContextManager.update_session(device_id, "Open calculator", {"status": "success", "message": "ok"}, plan)
    
    session = await SessionContextManager.get_session(device_id)
    assert session["references"].get("last_app") == "calculator"
    assert session["references"].get("last_action_target") == "calculator"
    
@pytest.mark.asyncio
async def test_format_context():
    device_id = "test_dev_3"
    await save_session_context(device_id, [], {})
    plan = {
        "intent": "open_app",
        "action": "open_app",
        "arguments": {"app": "calculator"}
    }
    await SessionContextManager.update_session(device_id, "Open calculator", {"status": "success", "message": "ok"}, plan)
    
    session = await SessionContextManager.get_session(device_id)
    formatted = SessionContextManager.format_context_for_prompt(session)
    assert "User: Open calculator" in formatted
    assert "Cortex: [Executed open_app with args" in formatted
    assert "last_app: calculator" in formatted
