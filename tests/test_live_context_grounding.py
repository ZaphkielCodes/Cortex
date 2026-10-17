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
async def test_1_screenshot_continuity_same_screenshot_analyzed(tmp_path):
    """
    Test 1:
    'Take a screenshot' -> screen.capture succeeds and persists screenshot ref.
    'Read that screenshot' -> inspects the exact existing screenshot ref without taking a new one.
    """
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    screenshot_file = tmp_path / "screenshot_20261014_120000.png"
    screenshot_file.write_bytes(b"FAKE_PNG_BYTES_FOR_TEST")
    sc_ref_str = str(screenshot_file.resolve())

    # 1. Take a screenshot: executor returns success with data & message
    executor_result = {
        "status": "success",
        "message": f"Screenshot saved: {sc_ref_str}",
        "data": {
            "image_ref": sc_ref_str,
            "width": 1920,
            "height": 1080,
            "display_index": 0,
        },
    }

    # Simulate command dispatch and completion
    await SessionContextManager.set_screenshot_ref("asus", sc_ref_str)
    await SessionContextManager.update_session(
        device_id="asus",
        request="Take a screenshot",
        response=executor_result,
        action_plan={"action": "screen.capture", "arguments": {}, "intent": "capture_screen"},
    )

    # Verify session has the screenshot reference
    sess = await SessionContextManager.get_session("asus")
    assert sess["references"].get("last_screenshot_ref") == sc_ref_str

    # 2. Follow-up: "Read that screenshot"
    # Mock vision provider to verify it receives the EXACT screenshot path
    mock_obs = ScreenObservation(
        observation_id="obs_test_1",
        device_id="asus",
        captured_at="2026-10-14T12:00:00Z",
        summary="You have Google Chrome open with the Cortex Dashboard.",
        visible_apps=["Google Chrome"],
        source_image_reference=sc_ref_str,
    )

    with patch("hub.vision_provider.get_vision_provider") as mock_get_vp:
        mock_vp = MagicMock()
        mock_vp.provider_name = "mock_vision"
        mock_vp.model_name = "mock_model"
        mock_vp.analyze_image = AsyncMock(return_value=mock_obs)
        mock_get_vp.return_value = mock_vp

        resp = await harness.process_request("asus", "Read that screenshot and tell me what you see.", context={})
        assert resp.status == "success"
        assert "Google Chrome" in resp.message or "dashboard" in resp.message.lower()

        # Verify that analyze_image was called with the exact screenshot reference
        mock_vp.analyze_image.assert_awaited_once()
        call_kwargs = mock_vp.analyze_image.call_args.kwargs
        assert call_kwargs["image_ref"] == sc_ref_str


# ===========================================================================
# Test 2: Foreground app = Discord -> "close the app I'm using" -> app.close Discord
# ===========================================================================
@pytest.mark.asyncio
async def test_2_close_the_app_im_using_resolves_foreground_app():
    """
    Test 2:
    Foreground app = Discord in world state.
    'close the app I'm using' -> dispatches app.close(app='Discord').
    """
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    context = {
        "world_state": {
            "foreground_app": "Discord",
        }
    }

    with patch.object(ctrl, "execute_and_observe", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"status": "success", "message": "Closed Discord."}

        resp = await harness.process_request("asus", "close the app I'm using", context=context)

        assert resp.status == "success"
        assert "Discord" in resp.message
        mock_exec.assert_called_once()
        _, kwargs = mock_exec.call_args
        args_dict = mock_exec.call_args[0][2]
        assert args_dict["app"] == "Discord"


# ===========================================================================
# Test 3: Foreground app = ChatGPT -> "close it" after explicit confirmation
# ===========================================================================
@pytest.mark.asyncio
async def test_3_close_it_after_explicit_confirmation():
    """
    Test 3:
    Foreground app = ChatGPT.
    Turn 1: 'What app am I currently using?' -> Cortex confirms ChatGPT.
    Turn 2: 'close it' -> pronoun 'it' resolves to ChatGPT.
    """
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    context = {
        "world_state": {
            "foreground_app": "ChatGPT",
        }
    }

    # Turn 1: Ask current app
    resp1 = await harness.process_request("asus", "What app am I currently using?", context=context)
    assert resp1.status == "success"
    assert "ChatGPT" in resp1.message

    # Session references now record ChatGPT as confirmed current app
    sess = await SessionContextManager.get_session("asus")
    assert sess["references"].get("last_confirmed_app") == "ChatGPT"

    # Turn 2: "close it"
    with patch.object(ctrl, "execute_and_observe", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"status": "success", "message": "Closed ChatGPT."}

        resp2 = await harness.process_request("asus", "close it", context=context)

        assert resp2.status == "success"
        assert "ChatGPT" in resp2.message
        mock_exec.assert_called_once()
        args_dict = mock_exec.call_args[0][2]
        assert args_dict["app"] == "ChatGPT"


# ===========================================================================
# Test 4: Stale session target = Once Human, foreground = Discord
# ===========================================================================
@pytest.mark.asyncio
async def test_4_stale_target_cannot_override_live_foreground_app():
    """
    Test 4:
    Stale session target = Once Human in historical references.
    Foreground app = Discord in active world state.
    'close the app that I'm using right now' -> chooses Discord, never Once Human.
    """
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    # Inject stale Once Human target into session references
    sess = await SessionContextManager.get_session("asus")
    sess["references"]["last_app"] = "Once Human"
    sess["references"]["last_action_target"] = "Once Human"
    sess["references"]["grounded_target"] = {
        "target_name": "Once Human",
        "target_kind": "filesystem_item",
        "path": "c:/games/once_human.exe",
    }
    await SessionContextManager.set_grounded_target("asus", sess["references"]["grounded_target"])

    context = {
        "session": sess,
        "world_state": {
            "foreground_app": "Discord",
        },
    }

    with patch.object(ctrl, "execute_and_observe", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"status": "success", "message": "Closed Discord."}

        resp = await harness.process_request(
            "asus", "Close the app that I'm using right now.", context=context
        )

        assert resp.status == "success"
        assert "Discord" in resp.message
        assert "Once Human" not in resp.message
        mock_exec.assert_called_once()
        args_dict = mock_exec.call_args[0][2]
        assert args_dict["app"] == "Discord"


# ===========================================================================
# Test 5: Recent explicit target = Notepad, then foreground-current = ChatGPT
# ===========================================================================
@pytest.mark.asyncio
async def test_5_contextual_precedence_explicit_vs_foreground():
    """
    Test 5:
    Recent explicit target = Notepad.
    Foreground app = ChatGPT.
    1. 'close the app I'm using' -> semantic foreground takes precedence -> ChatGPT.
    2. 'close Notepad' -> explicit target in current utterance takes highest precedence -> Notepad.
    """
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    sess = await SessionContextManager.get_session("asus")
    sess["references"]["last_app"] = "Notepad"
    sess["references"]["last_action_target"] = "Notepad"

    context = {
        "session": sess,
        "world_state": {
            "foreground_app": "ChatGPT",
        },
    }

    # 1. Semantic foreground reference selects ChatGPT
    with patch.object(ctrl, "execute_and_observe", new_callable=AsyncMock) as mock_exec1:
        mock_exec1.return_value = {"status": "success", "message": "Closed ChatGPT."}
        resp1 = await harness.process_request("asus", "close the app I'm using", context=context)
        assert resp1.status == "success"
        assert "ChatGPT" in resp1.message
        mock_exec1.assert_called_once()
        assert mock_exec1.call_args[0][2]["app"] == "ChatGPT"

    # 2. Explicit utterance target selects Notepad despite foreground ChatGPT
    with patch.object(ctrl, "execute_and_observe", new_callable=AsyncMock) as mock_exec2:
        mock_exec2.return_value = {"status": "success", "message": "Closed Notepad."}
        resp2 = await harness.process_request("asus", "close Notepad", context=context)
        assert resp2.status == "success"
        assert "Notepad" in resp2.message
        mock_exec2.assert_called_once()
        assert mock_exec2.call_args[0][2]["app"] == "Notepad"


# ===========================================================================
# Test 6: "What am I doing right now?" -> grounded summary from world/screen state
# ===========================================================================
@pytest.mark.asyncio
async def test_6_what_am_i_doing_right_now_synthesis():
    """
    Test 6:
    'What am I doing right now?'
    Case A: foreground_app = ChatGPT, screen summary = Cortex development conversation
            -> "You're currently using ChatGPT and working on Cortex."
    Case B: foreground_app = Discord, no task or screen
            -> "You are currently using Discord."
    Case C: No observations
            -> conservative answer without hallucination.
    """
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    # Case A: ChatGPT + Cortex development conversation
    context_a = {
        "world_state": {
            "foreground_app": "ChatGPT",
            "screen_summary": "Cortex development conversation",
        }
    }
    resp_a = await harness.process_request("asus", "What am I doing right now?", context=context_a)
    assert resp_a.status == "success"
    assert "ChatGPT" in resp_a.message
    assert "Cortex" in resp_a.message

    # Case B: Discord only
    context_b = {
        "world_state": {
            "foreground_app": "Discord",
        }
    }
    resp_b = await harness.process_request("asus", "What am I doing right now?", context=context_b)
    assert resp_b.status == "success"
    assert "Discord" in resp_b.message

    # Case C: Empty observations
    context_c = {
        "world_state": {}
    }
    resp_c = await harness.process_request("asus", "What am I doing right now?", context=context_c)
    assert resp_c.status == "success"
    assert "don't have enough" in resp_c.message.lower() or "not recorded" in resp_c.message.lower()


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
