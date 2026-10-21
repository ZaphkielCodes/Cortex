import pytest
import pytest_asyncio
import os
import json
from unittest.mock import patch, AsyncMock

from hub.database import init_db, enroll_device, update_device_capabilities, update_connection_state, set_device_permission
import hub.main
from hub.intent_schema import ActionPlan, ModelResult
from hub.agent_harness import AgentHarness
from hub.control_layer import CortexControlLayer
from hub.direct_provider import DirectLLMProvider

import tempfile


@pytest_asyncio.fixture(autouse=True)
async def clear_state():
    import hub.main
    hub.main.CONNECTED_DEVICES.clear()
    import hub.database as database
    import aiosqlite
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute("DELETE FROM devices")
        await db.execute("DELETE FROM device_capabilities")
        await db.execute("DELETE FROM device_permissions")
        await db.execute("DELETE FROM memory_items")
        await db.commit()
@pytest.mark.asyncio
async def test_fallback_malformed_backup_json():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    await update_device_capabilities("dev1", "windows", ["app.launch", "ui.notify"])
    await update_connection_state("dev1", "connected")
    await set_device_permission("dev1", "app.launch", "allow")
    
    hub.main.CONNECTED_DEVICES["dev1"] = AsyncMock()
    
    # We test the Backup provider directly
    provider = DirectLLMProvider(api_key="dummy", model_name="qwen/qwen3.8-27b")
    
    # Mock httpx to return malformed json
    class MockResponse:
        status_code = 200
        headers = {}
        def json(self):
            return {"choices": [{"message": {"content": "User Safety: safe"}}]}
            
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MockResponse()
        
        with pytest.raises(ValueError, match="LLM returned invalid JSON"):
            await provider.generate_result("open calculator", {"device": {"id": "dev1"}})

@pytest.mark.asyncio
async def test_fallback_schema_violation():
    provider = DirectLLMProvider(api_key="dummy", model_name="qwen/qwen3.8-27b")
    
    # Missing type field
    class MockResponse:
        status_code = 200
        headers = {}
        def json(self):
            return {"choices": [{"message": {"content": '{"action_plan": {"intent": "open_app"}}'}}]}
            
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MockResponse()
        
        with pytest.raises(ValueError, match="LLM output does not match ModelResult schema"):
            await provider.generate_result("open calculator", {"device": {"id": "dev1"}})

@pytest.mark.asyncio
async def test_fallback_circuit_breaker():
    from hub.llm_provider import CircuitBreakerProvider, MockLLMProvider
    
    class FailingPrimary(MockLLMProvider):
        async def generate_result(self, *args, **kwargs):
            raise Exception("503 Service Unavailable")
            
    class SuccessFallback(MockLLMProvider):
        async def generate_result(self, *args, **kwargs):
            return ModelResult(type="RESPONSE", response_text="Fallback success")
            
    primary = FailingPrimary()
    fallback = SuccessFallback()
    cb = CircuitBreakerProvider(primary, fallback)
    
    # Needs a small sleep patch for the 1s retry
    with patch("asyncio.sleep", new_callable=AsyncMock):
        res = await cb.generate_result("hello", {})
        assert res.type == "RESPONSE"
        assert res.response_text == "Fallback success"
        assert cb.failures == 1 # 1 overall failed attempt (which had 1 retry inside)

@pytest.mark.asyncio
async def test_fallback_routing_pipeline():
    from hub.agent_harness import AgentHarness
    from hub.control_layer import CortexControlLayer
    from hub.model_router import ProviderRouterAdapter
    from hub.llm_provider import CircuitBreakerProvider, MockLLMProvider
    from google.genai.errors import APIError
    import hub.main
    
    await enroll_device("dev1", "Laptop", "admin", "hash")
    await update_device_capabilities("dev1", "windows", ["app.launch", "ui.notify"])
    await update_connection_state("dev1", "connected")
    await set_device_permission("dev1", "app.launch", "allow")
    
    # Mock the connection so we don't actually try to send WS traffic
    hub.main.CONNECTED_DEVICES["dev1"] = AsyncMock()
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    
    class MockGemini(MockLLMProvider):
        def __init__(self):
            self.calls = 0
            self.name_prop = "gemini"
        @property
        def name(self) -> str:
            return self.name_prop
        async def generate_result(self, user_text, context, on_chunk=None):
            self.calls += 1
            # Raise a simulated 429 quota exhaustion.
            # Using APIError because ClientError requires a request object in some versions,
            # or just Exception if we want to be safe. We'll use a generic Exception matching the prompt.
            class QuotaError(Exception):
                pass
            raise QuotaError("429 RESOURCE_EXHAUSTED")

    class MockBackup(MockLLMProvider):
        def __init__(self):
            self.calls = 0
            self.name_prop = "backup"
        @property
        def name(self) -> str:
            return self.name_prop
        async def generate_result(self, user_text, context, on_chunk=None):
            self.calls += 1
            return ModelResult(type="RESPONSE", response_text=json.dumps({
                "decision": "RESPOND", "response_text": "Recursion solves a problem through smaller instances."}))

    gemini = MockGemini()
    backup = MockBackup()
    
    # Needs to bypass the 1s sleep in circuit breaker
    with patch("asyncio.sleep", new_callable=AsyncMock):
        cb = CircuitBreakerProvider(gemini, backup)
        harness.router = ProviderRouterAdapter(cb)
        
        # --- SCENARIO 1: Gemini fails -> Backup called ---
        res = await harness.process_request("dev1", "Explain recursion", {"device": {"id": "dev1"}}, None)
        
        assert gemini.calls >= 1, "Gemini should be attempted first"
        assert backup.calls == 1, "Backup should be invoked exactly once"
        assert res.status == "success", "Original Gemini exception should not crash request"
        
        # --- SCENARIO 2: Gemini success -> Backup NOT called ---
        gemini.calls = 0
        backup.calls = 0
        
        class SuccessGemini(MockLLMProvider):
            def __init__(self):
                self.calls = 0
            @property
            def name(self) -> str:
                return "gemini"
            async def generate_result(self, user_text, context, on_chunk=None):
                self.calls += 1
                return ModelResult(type="RESPONSE", response_text=json.dumps({"decision":"RESPOND","response_text":"Recursion uses smaller instances."}))
                
        harness.router = ProviderRouterAdapter(CircuitBreakerProvider(SuccessGemini(), backup))
        res2 = await harness.process_request("dev1", "Explain recursion", {"device": {"id": "dev1"}}, None)
        
        assert harness.router.provider.primary.calls == 1, "Gemini should be called"
        assert backup.calls == 0, "Backup should NOT be called on Gemini success"
        assert res2.status == "success"
        
        # --- SCENARIO 3: Gemini fails + Backup fails -> Structured failure ---
        class FailAllGemini(MockGemini):
            pass
        class FailAllBackup(MockBackup):
            async def generate_result(self, user_text, context, on_chunk=None):
                self.calls += 1
                raise Exception("Backup down")
                
        fail_gemini = FailAllGemini()
        fail_backup = FailAllBackup()
        
        harness.router = ProviderRouterAdapter(CircuitBreakerProvider(fail_gemini, fail_backup))
        res3 = await harness.process_request("dev1", "Explain recursion", {"device": {"id": "dev1"}}, None)
        
        assert fail_gemini.calls >= 1
        assert fail_backup.calls == 1
        assert res3.status == "error"
        assert "usage limit" in res3.message.lower() and "backup service also failed" in res3.message.lower()

