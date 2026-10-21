"""
tests/test_live_context_grounding.py
------------------------------------
Side Repair Test Suite: Live Context Grounding & Screenshot Continuity.

Verifies:
- Test 1: Capture screenshot -> read that screenshot -> same screenshot analyzed
- Test 2: Foreground app = Discord -> "close the app I'm using" -> app.close Discord
- Test 3: Foreground app = ChatGPT -> "close it" after explicit confirmation -> ChatGPT selected
- Test 4: Stale session target = Once Human, foreground app = Discord -> current-app reference chooses Discord, never Once Human
- Test 5: Recent explicit target = Notepad, then foreground-current reference = ChatGPT -> contextual precedence behaves correctly
- Test 6: "What am I doing right now?" -> grounded summary from world/screen state
- Test 7: Production DB isolation
"""

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from hub.agent_harness import AgentHarness
from hub.control_layer import CortexControlLayer
from hub.session_context import SessionContextManager
from hub.target_resolver import TargetResolver
from hub.world_state import WorldStateManager, Signal
from hub.screen_perception import ScreenPerceptionManager, ScreenObservation


# ===========================================================================
# Test 1: Capture screenshot -> read that screenshot -> same screenshot analyzed
# ===========================================================================
@pytest.mark.asyncio
async def test_1_screenshot_continuity_same_screenshot_analyzed(tmp_path, monkeypatch):
    """Persisted screenshot identity reaches the model and actual analysis provider."""
    from hub.vision_provider import MockVisionProvider, set_vision_provider_override
    from model_contract_fixtures import native_harness, EvidenceModel
    image = str(tmp_path / "original.png")
    await SessionContextManager.set_screenshot_ref("asus",image)
    sess = await SessionContextManager.get_session("asus")
    provider = MockVisionProvider(summary="Google Chrome showing the dashboard")
    provider.locality = "local_device"
    provider.analyze_image = AsyncMock(wraps=provider.analyze_image)
    set_vision_provider_override(provider)
    model = EvidenceModel("screen.analyze",{"image_ref":image,"question":"What do you see?"})
    harness, ctrl = await native_harness(monkeypatch,model)
    ctrl.execute_and_observe = AsyncMock(wraps=ctrl.execute_and_observe)
    resp = await harness.process_request("asus","Read that screenshot and tell me what you see.",{"session":sess})
    assert resp.status == "success" and "Google Chrome" in resp.message
    state=json.loads(model.calls[0]["system_prompt"].split("\nSTATE:\n")[1])
    assert state["context"]["session_references"]["last_screenshot_ref"] == image
    assert provider.analyze_image.await_args.kwargs["image_ref"] == image
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list] == ["screen.analyze"]



# ===========================================================================
# Test 2: Foreground app = Discord -> "close the app I'm using" -> app.close Discord
# ===========================================================================
@pytest.mark.asyncio
async def test_2_close_the_app_im_using_resolves_foreground_app():
    """Fresh foreground evidence precedes the model's close, with exact dispatch target."""
    harness, ctrl, model = foreground_close_harness("Discord")
    resp = await harness.process_request("asus","close the app I'm using",{})
    assert resp.status == "success"
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list] == ["ui.observe","app.close"]
    assert ctrl.execute_and_observe.await_args.args[2]["app"] == "Discord"
    assert "Discord" in model.calls[1]["system_prompt"]



# ===========================================================================
# Test 3: Foreground app = ChatGPT -> "close it" after explicit confirmation
# ===========================================================================
@pytest.mark.asyncio
async def test_3_close_it_after_explicit_confirmation():
    """Pronoun turns receive durable prior conversation, not a regex-confirmed-app tag."""
    from model_contract_fixtures import scripted_harness, call, SequenceEvidenceModel
    harness, ctrl, model = scripted_harness([call("ui.observe",include_tree=False)],
        [{"status":"success","data":{"foreground_app":"ChatGPT"}}],answer="You're using ChatGPT.")
    first = await harness.process_request("asus","What app am I currently using?",{})
    assert first.status == "success" and first.data["foreground_app"] == "ChatGPT"
    await SessionContextManager.update_session("asus","What app am I currently using?",first.model_dump())
    sess = await SessionContextManager.get_session("asus")
    harness, ctrl, model = scripted_harness([call("app.close",app="ChatGPT")],[{"status":"success","data":{"closed":True}}])
    final = await harness.process_request("asus","close it",{"session":sess})
    assert final.status == "success"
    assert ctrl.execute_and_observe.await_args.args[2]["app"] == "ChatGPT"
    assert "ChatGPT" in json.dumps(model.calls[0][1]["conversation_history"])



# ===========================================================================
# Test 4: Stale session target = Once Human, foreground = Discord
# ===========================================================================
@pytest.mark.asyncio
async def test_4_stale_target_cannot_override_live_foreground_app():
    """A stale session target remains contextual data; only the fresh model target executes."""
    harness, ctrl, model = foreground_close_harness("Discord")
    resp = await harness.process_request("asus","Close the app that I'm using right now.",
        {"session":{"references":{"last_app":"Once Human","last_action_target":"Once Human"}}})
    assert resp.status == "success"
    assert "Once Human" in model.calls[0]["system_prompt"]
    assert "Discord" in model.calls[1]["system_prompt"]
    closes=[c for c in ctrl.execute_and_observe.await_args_list if c.args[1]=="app.close"]
    assert len(closes)==1 and closes[0].args[2]["app"]=="Discord"



# ===========================================================================
# Test 5: Recent explicit target = Notepad, then foreground-current = ChatGPT
# ===========================================================================
@pytest.mark.asyncio
async def test_5_contextual_precedence_explicit_vs_foreground():
    """Exact explicit model targets are not overwritten by stale references or snapshots."""
    from model_contract_fixtures import scripted_harness, call
    for request,target in [("close the app I'm using","ChatGPT"),("close Notepad","Notepad")]:
        harness,ctrl,model=scripted_harness([call("app.close",app=target)],[{"status":"success","data":{"closed":True}}])
        resp=await harness.process_request("asus",request,{"world_state":{"foreground_app":"ChatGPT"},"session":{"references":{"last_app":"Notepad"}}})
        assert resp.status == "success"
        assert ctrl.execute_and_observe.await_count == 1
        assert ctrl.execute_and_observe.await_args.args[2]["app"] == target
        assert model.calls[0][0] == request
        assert "ChatGPT" in model.calls[0][1]["system_prompt"] and "Notepad" in model.calls[0][1]["system_prompt"]



# ===========================================================================
# Test 6: "What am I doing right now?" -> grounded summary from world/screen state
# ===========================================================================
@pytest.mark.asyncio
async def test_6_what_am_i_doing_right_now_synthesis():
    """Grounded snapshots reach model answers; absent observations require clarification."""
    from model_contract_fixtures import scripted_harness
    cases=[({"foreground_app":"ChatGPT","screen_summary":"Cortex development conversation"},
            "RESPOND","The snapshot shows ChatGPT with a Cortex development conversation."),
           ({"foreground_app":"Discord"},"RESPOND","The snapshot reports Discord."),
           ({},"ASK_CLARIFICATION","What would you like me to inspect?")]
    for facts,kind,answer in cases:
        harness,ctrl,model=scripted_harness([{"decision":kind,"response_text":answer}])
        resp=await harness.process_request("asus","What am I doing right now?",{"world_state":facts})
        assert resp.status == ("success" if facts else "clarification_needed")
        assert resp.message == answer
        state=json.loads(model.calls[0][1]["system_prompt"].split("\nSTATE:\n")[1])
        assert state["context"].get("world_snapshot",{}) == facts
        ctrl.execute_and_observe.assert_not_awaited()


# ===========================================================================
# Test 7: Production DB isolation check
# ===========================================================================
def test_7_production_db_isolation():
    """Verify tests run strictly against isolated DB and production DB is never targeted."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_db = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_db, f"FATAL: Tests are targeting production DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_db.exists(), "Production DB data/cortex.db must exist."


def foreground_close_harness(app):
    from model_contract_fixtures import scripted_harness, call, SequenceEvidenceModel
    harness,ctrl,_=scripted_harness([], [
        {"status":"success","data":{"foreground_app":app}},
        {"status":"success","data":{"closed":True}}])
    model=SequenceEvidenceModel([
        dict(call("ui.observe",include_tree=False),continue_after_result=True),
        call("app.close",app=app),
        {"decision":"RESPOND","response_text":"The requested application was closed."}])
    harness.router=model
    return harness,ctrl,model

import json
