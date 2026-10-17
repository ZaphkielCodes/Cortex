import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from hub.agent_harness import AgentHarness
from hub.control_layer import CortexControlLayer
from hub.intent_schema import ConversationalResponse
from hub.model_router import ModelResult, ActionPlan
import time

class MockRouter:
    async def generate_result(self, natural_request, context, on_chunk=None):
        req = natural_request.lower()
        if context.get("force_response_only"):
            return ModelResult(type="RESPONSE", response_text="Fast response text")
        
        # Fake a structured response
        if "delete" in req:
            plan = ActionPlan(
                intent="delete_file",
                source_device="test-device",
                target_device="test-device",
                target_device_reference="this device",
                capability="fs.delete",
                action="delete",
                arguments={"file": "old screenshot"},
                requires_confirmation=True,
                confidence=0.9,
                user_facing_summary="deleting file"
            )
            return ModelResult(type="ACTION", action_plan=plan)
        if "send" in req and "laptop" in req:
            plan = ActionPlan(
                intent="send_file",
                source_device="test-device",
                target_device="test-device",
                target_device_reference="laptop",
                capability="fs.send",
                action="send",
                arguments={"file": "this file"},
                requires_confirmation=False,
                confidence=0.9,
                user_facing_summary="sending file"
            )
            return ModelResult(type="ACTION", action_plan=plan)
            
        return ModelResult(type="RESPONSE", response_text="I don't know")

class MockControlLayer:
    def __init__(self):
        self.executed_commands = []
        
    async def resolve_target_device(self, src, ref):
        return "resolved-device"
        
    async def validate_target_and_capability(self, dev, cap):
        return {"valid": True, "reason": None}
        
    async def check_permissions(self, src, dev, cap):
        class Perm:
            allowed = True
            needs_confirmation = False
            tier = 1
        return Perm()
        
    async def request_approval(self, dev, action, args, src, tier):
        return "cmd-123"
        
    async def execute_command(self, dev, action, args, latency=None, **kwargs):
        self.executed_commands.append(action)
        return "cmd-exec-123"

@pytest.fixture
def harness():
    control = MockControlLayer()
    harness = AgentHarness(control)
    harness.router = MockRouter()
    return harness, control

@pytest.mark.asyncio
async def test_router_local_fast(harness):
    harness_obj, control = harness
    metrics = {}
    res = await harness_obj.process_request("test", "open Calculator", {}, None, metrics)
    assert res.status == "success"
    assert metrics.get("routing_path") == "LOCAL_FAST"
    assert "open_app" in control.executed_commands
    
    # check that it passed through control layer (executed_commands appended)
    assert len(control.executed_commands) == 1

@pytest.mark.asyncio
async def test_router_local_fast_normalized(harness):
    harness_obj, control = harness
    metrics = {}
    res = await harness_obj.process_request("test", "open git hub", {}, None, metrics)
    assert res.status == "success"
    assert metrics.get("routing_path") == "LOCAL_FAST"
    assert res.message == "Okay, I am opening github."

@pytest.mark.asyncio
async def test_router_local_fast_punctuation(harness):
    harness_obj, control = harness
    metrics = {}
    res = await harness_obj.process_request("test", "Open calculator.", {}, None, metrics)
    assert res.status == "success"
    assert metrics.get("routing_path") == "LOCAL_FAST"
    assert "calculator" == res.action_plan.arguments["app"]

@pytest.mark.asyncio
async def test_router_local_fast_wrapper(harness):
    harness_obj, control = harness
    metrics = {}
    res = await harness_obj.process_request("test", "I said open YouTube!", {}, None, metrics)
    assert res.status == "success"
    assert metrics.get("routing_path") == "LOCAL_FAST"
    assert "youtube" == res.action_plan.arguments["app"]

@pytest.mark.asyncio
async def test_router_conversational_question(harness):
    harness_obj, control = harness
    metrics = {}
    res = await harness_obj.process_request("test", "what is Spotify?", {}, None, metrics)
    assert res.status == "success"
    assert metrics.get("routing_path") == "FAST_RESPONSE"
    assert "Fast response" in res.message
    assert len(control.executed_commands) == 0

@pytest.mark.asyncio
async def test_router_bare_app_name(harness):
    harness_obj, control = harness
    metrics = {}
    res = await harness_obj.process_request("test", "Spotify", {}, None, metrics)
    assert metrics.get("routing_path") == "FAST_RESPONSE"
    assert len(control.executed_commands) == 0

@pytest.mark.asyncio
async def test_router_complex_command(harness):
    harness_obj, control = harness
    metrics = {}
    res = await harness_obj.process_request("test", "send this file to my laptop", {}, None, metrics)
    assert metrics.get("routing_path") == "STRUCTURED_MODEL"
    # FS capabilities might not be mapped in map_intent_to_capability correctly in the mock?
    # agent_harness map: move_file: fs.move. We mocked 'send_file'. Let's check harness.
    pass

@pytest.mark.asyncio
async def test_router_destructive_verb(harness):
    harness_obj, control = harness
    metrics = {}
    res = await harness_obj.process_request("test", "delete the old screenshot", {}, None, metrics)
    assert metrics.get("routing_path") == "STRUCTURED_MODEL"

@pytest.mark.asyncio
async def test_router_messaging_verb(harness):
    harness_obj, control = harness
    metrics = {}
    res = await harness_obj.process_request("test", "email John and tell him I'm running late", {}, None, metrics)
    assert metrics.get("routing_path") == "STRUCTURED_MODEL"

@pytest.mark.asyncio
async def test_capability_aware_routing(harness):
    harness_obj, control = harness
    metrics = {}
    context = {"device": {"id": "test", "capabilities": ["device.status"]}}
    res = await harness_obj.process_request("test", "what is my battery percentage", context, None, metrics)
    assert metrics.get("routing_path") == "DETERMINISTIC_CAPABILITY"
    assert res.action_plan is not None
    assert res.action_plan.capability == "device.status"
    assert res.action_plan.action == "get_status"

@pytest.mark.asyncio
async def test_compound_command_routing(harness):
    harness_obj, control = harness
    metrics = {}
    context = {"device": {"id": "test", "capabilities": ["app.launch", "device.status"]}}
    
    # "open calculator" should be LOCAL_FAST
    res1 = await harness_obj.process_request("test", "open calculator", context, None, metrics)
    assert metrics.get("routing_path") == "LOCAL_FAST"
    
    # "open calculator and then tell me my battery" should be STRUCTURED_MODEL (TASK)
    metrics2 = {}
    res2 = await harness_obj.process_request("test", "open calculator and then tell me my battery percentage", context, None, metrics2)
    assert metrics2.get("routing_path") == "STRUCTURED_MODEL"
    
    # "open calculator and get battery" should be STRUCTURED_MODEL
    metrics3 = {}
    res3 = await harness_obj.process_request("test", "open calculator and get battery", context, None, metrics3)
    assert metrics3.get("routing_path") == "STRUCTURED_MODEL"
