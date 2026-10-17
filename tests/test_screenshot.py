import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
import sys

# Mock pyautogui if needed for headless test environments
if 'pyautogui' not in sys.modules:
    try:
        import pyautogui
    except ImportError:
        sys.modules['pyautogui'] = MagicMock()

# Hub imports
from hub.database import update_device_capabilities
from hub.agent_harness import AgentHarness
from hub.control_layer import CortexControlLayer
from hub.intent_schema import ActionPlan, ModelResult
import hub.main as hub_main

# Agent imports
from agent.executor import execute, get_capabilities

@pytest.mark.asyncio
async def test_agent_advertises_screen_capture():
    caps = get_capabilities()
    assert "screen.capture" in caps
    assert "screenshot.read" in caps # backward compat
    assert "ui.screenshot" in caps

def test_executor_successful_capture():
    mock_img = MagicMock()
    mock_img.size = (1920, 1080)
    with patch("PIL.ImageGrab.grab", return_value=mock_img):
        res = execute("take_screenshot", [])
        assert res["status"] == "success"
        assert "Screenshot saved" in res["message"]

def test_executor_exception_fails_safely():
    with patch("pyautogui.screenshot", side_effect=Exception("Disk full")):
        with patch("PIL.ImageGrab.grab", side_effect=Exception("Disk full PIL")):
            res = execute("take_screenshot", [])
            assert res["status"] == "failure"
            assert "Disk full" in res["message"]

@pytest.mark.asyncio
async def test_agent_harness_unsupported_capability():
    control = CortexControlLayer({"dev1": MagicMock()})
    harness = AgentHarness(control)
    
    control.resolve_target_device = AsyncMock(return_value="dev1")
    with patch.object(control, "validate_target_and_capability", return_value={"valid": False, "reason": "missing_capability"}):
        res = await harness.process_request("dev1", "take a screenshot", {"device": {"capabilities": ["screen.capture"]}}, None)
        assert res.status == "error"
        assert "does not support" in res.message
