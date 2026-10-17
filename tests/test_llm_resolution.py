import pytest
import asyncio
import json
from unittest.mock import patch, MagicMock
from hub.llm_provider import GeminiLLMProvider
from hub.session_context import SessionContextManager
from google.genai.errors import APIError, ClientError, ServerError

# ---------------------------------------------------------------------------
# Deterministic mock-based tests (Unit layer)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_genai_client():
    with patch('google.genai.Client') as MockClient:
        mock_instance = MockClient.return_value
        mock_models = MagicMock()
        mock_instance.models = mock_models
        yield mock_models

@pytest.mark.asyncio
async def test_deterministic_llm_reference_resolution_do_it(mock_genai_client):
    provider = GeminiLLMProvider()
    
    context = {
        "device": {"id": "test_dev", "name": "Test PC", "capabilities": [{"capability": "screenshot.read", "enabled": True}]},
        "session": {
            "turns": [
                {"request": "Can you take screenshots?", "response_message": "Yes, I can take screenshots on this device."},
            ],
            "references": {}
        }
    }
    
    # Configure mock to return expected JSON
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "type": "ACTION",
        "action_plan": {
            "intent": "take_screenshot",
            "source_device": "test_dev",
            "capability": "screenshot.read",
            "action": "take_screenshot",
            "arguments": {},
            "requires_confirmation": False,
            "confidence": 0.95,
            "user_facing_summary": "Taking a screenshot"
        }
    })
    mock_genai_client.generate_content.return_value = mock_response
    
    result = await provider.generate_result("Do it.", context)
    
    # Verify the prompt contained the context correctly
    call_args = mock_genai_client.generate_content.call_args
    assert call_args is not None
    user_message = call_args.kwargs["contents"]
    assert "Can you take screenshots?" in user_message
    
    assert result.type == "ACTION"
    assert result.action_plan.action == "take_screenshot"

@pytest.mark.asyncio
async def test_deterministic_llm_reference_resolution_close_it(mock_genai_client):
    provider = GeminiLLMProvider()
    
    context = {
        "device": {"id": "test_dev", "name": "Test PC", "capabilities": [{"capability": "app.launch", "enabled": True}]},
        "session": {
            "turns": [
                {"request": "Open Calculator.", "action": "open_app", "arguments": {"app": "calculator"}, "response_message": "Opening calculator."}
            ],
            "references": {"last_app": "calculator"}
        }
    }
    
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "type": "ACTION",
        "action_plan": {
            "intent": "close_app",
            "source_device": "test_dev",
            "capability": "app.launch",
            "action": "close_app",
            "arguments": {"app": "calculator"},
            "requires_confirmation": False,
            "confidence": 0.95,
            "user_facing_summary": "Closing calculator"
        }
    })
    mock_genai_client.generate_content.return_value = mock_response
    
    result = await provider.generate_result("Close it.", context)
    
    call_args = mock_genai_client.generate_content.call_args
    user_message = call_args.kwargs["contents"]
    assert "last_app" in user_message or "calculator" in user_message
    
    assert result.type == "ACTION"
    assert result.action_plan.action == "close_app"
    assert result.action_plan.arguments.get("app") == "calculator"

@pytest.mark.asyncio
async def test_deterministic_llm_reference_resolution_open_it_again(mock_genai_client):
    provider = GeminiLLMProvider()
    
    context = {
        "device": {"id": "test_dev", "name": "Test PC", "capabilities": [{"capability": "app.launch", "enabled": True}]},
        "session": {
            "turns": [
                {"request": "Open YouTube.", "action": "open_app", "arguments": {"app": "youtube"}, "response_message": "Opening YouTube."}
            ],
            "references": {"last_app": "youtube"}
        }
    }
    
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "type": "ACTION",
        "action_plan": {
            "intent": "open_app",
            "source_device": "test_dev",
            "capability": "app.launch",
            "action": "open_app",
            "arguments": {"app": "youtube"},
            "requires_confirmation": False,
            "confidence": 0.95,
            "user_facing_summary": "Opening YouTube"
        }
    })
    mock_genai_client.generate_content.return_value = mock_response
    
    result = await provider.generate_result("Open it again.", context)
    assert result.type == "ACTION"
    assert result.action_plan.action == "open_app"
    assert result.action_plan.arguments.get("app") == "youtube"

@pytest.mark.asyncio
async def test_deterministic_llm_reference_resolution_ambiguous(mock_genai_client):
    provider = GeminiLLMProvider()
    
    context = {
        "device": {"id": "test_dev", "name": "Test PC", "capabilities": [{"capability": "app.launch", "enabled": True}]},
        "session": {
            "turns": [
                {"request": "Open Calculator and Notepad.", "response_message": "Opening Calculator and Notepad."}
            ],
            "references": {}
        }
    }
    
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "type": "RESPONSE",
        "response_text": "Which one would you like me to close, Calculator or Notepad?"
    })
    mock_genai_client.generate_content.return_value = mock_response
    
    result = await provider.generate_result("Close it.", context)
    assert result.type == "RESPONSE"

# ---------------------------------------------------------------------------
# Live API Integration Tests (marked live_llm)
# ---------------------------------------------------------------------------

def handle_quota_errors(func):
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ClientError as e:
            if e.code == 429:
                pytest.skip("Live API test skipped: Quota exhausted (HTTP 429)")
            elif e.code == 404:
                pytest.skip("Live API test skipped: Model not found (HTTP 404)")
            raise
        except ServerError as e:
            if e.code in [503, 500]:
                pytest.skip(f"Live API test skipped: Server unavailable (HTTP {e.code})")
            raise
    return wrapper

@pytest.mark.asyncio
@pytest.mark.live_llm
@handle_quota_errors
async def test_live_llm_reference_resolution_do_it():
    provider = GeminiLLMProvider()
    
    context = {
        "device": {"id": "test_dev", "name": "Test PC", "capabilities": [{"capability": "screenshot.read", "enabled": True}]},
        "session": {
            "turns": [
                {"request": "Can you take screenshots?", "response_message": "Yes, I can take screenshots on this device."},
            ],
            "references": {}
        }
    }
    
    result = await provider.generate_result("Do it.", context)
    assert result.type == "ACTION"
    assert result.action_plan.action == "take_screenshot"

@pytest.mark.asyncio
@pytest.mark.live_llm
@handle_quota_errors
async def test_live_llm_reference_resolution_close_it():
    provider = GeminiLLMProvider()
    
    context = {
        "device": {"id": "test_dev", "name": "Test PC", "capabilities": [{"capability": "app.launch", "enabled": True}]},
        "session": {
            "turns": [
                {"request": "Open Calculator.", "action": "open_app", "arguments": {"app": "calculator"}, "response_message": "Opening calculator."}
            ],
            "references": {"last_app": "calculator"}
        }
    }
    
    result = await provider.generate_result("Close it.", context)
    assert result.type == "ACTION"
    assert result.action_plan.action == "close_app"
    assert result.action_plan.arguments.get("app") == "calculator"

@pytest.mark.asyncio
@pytest.mark.live_llm
@handle_quota_errors
async def test_live_llm_reference_resolution_open_it_again():
    provider = GeminiLLMProvider()
    
    context = {
        "device": {"id": "test_dev", "name": "Test PC", "capabilities": [{"capability": "app.launch", "enabled": True}]},
        "session": {
            "turns": [
                {"request": "Open YouTube.", "action": "open_app", "arguments": {"app": "youtube"}, "response_message": "Opening YouTube."}
            ],
            "references": {"last_app": "youtube"}
        }
    }
    
    result = await provider.generate_result("Open it again.", context)
    assert result.type == "ACTION"
    assert result.action_plan.action == "open_app"
    assert result.action_plan.arguments.get("app") == "youtube"

@pytest.mark.asyncio
@pytest.mark.live_llm
@handle_quota_errors
async def test_live_llm_reference_resolution_ambiguous():
    provider = GeminiLLMProvider()
    
    context = {
        "device": {"id": "test_dev", "name": "Test PC", "capabilities": [{"capability": "app.launch", "enabled": True}]},
        "session": {
            "turns": [
                {"request": "Open Calculator and Notepad.", "response_message": "Opening Calculator and Notepad."}
            ],
            "references": {}
        }
    }
    
    result = await provider.generate_result("Close it.", context)
    assert result.type == "RESPONSE"
