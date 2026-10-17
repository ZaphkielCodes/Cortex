import pytest
import json
from hub.task_orchestrator import TaskState, TaskOrchestrator
from hub.control_layer import CortexControlLayer
from hub.intent_schema import ModelResult, ActionPlan
from hub.model_router import ModelRouter

class MockRouterForTask(ModelRouter):
    def __init__(self, steps):
        self.steps = steps
        self.call_count = 0
        
    @property
    def name(self): return "mock_task"

    async def generate_result(self, user_text, context, on_chunk=None):
        if self.call_count < len(self.steps):
            res = self.steps[self.call_count]
            self.call_count += 1
            return res
        return ModelResult(type="TASK_STEP", step_decision={"decision": "FAIL", "response_text": "Out of steps in mock."})

class MockControlForTask(CortexControlLayer):
    def __init__(self):
        pass
    async def resolve_target_device(self, src, ref):
        return src
    async def validate_target_and_capability(self, dev, cap):
        return {"valid": True, "reason": "ok"}
    async def check_permissions(self, src, tgt, cap):
        class Perm:
            allowed = True
        return Perm()
    async def execute_command(self, tgt, act, args, lat=None, **kwargs):
        import uuid
        return uuid.uuid4().hex

@pytest.mark.asyncio
async def test_task_orchestrator_multi_step():
    steps = [
        ModelResult(type="TASK_STEP", step_decision={
            "decision": "NEXT_ACTION",
            "action_plan": {
                "intent": "open_app",
                "source_device": "dev1",
                "capability": "app.launch",
                "action": "open_app",
                "arguments": {"app": "calculator"},
                "confidence": 1.0,
                "user_facing_summary": "open calc",
                "requires_confirmation": False
            }
        }),
        ModelResult(type="TASK_STEP", step_decision={
            "decision": "NEXT_ACTION",
            "action_plan": {
                "intent": "get_device_status",
                "source_device": "dev1",
                "capability": "device.info",
                "action": "get_status",
                "arguments": {},
                "confidence": 1.0,
                "user_facing_summary": "get bat",
                "requires_confirmation": False
            }
        }),
        ModelResult(type="TASK_STEP", step_decision={
            "decision": "FINAL_RESPONSE",
            "response_text": "Battery is 100% and calc is open."
        })
    ]
    router = MockRouterForTask(steps)
    control = MockControlForTask()
    
    orch = TaskOrchestrator(router, control)
    import asyncio
    
    # We must patch wait_for so we don't actually wait for the physical PENDING_FUTURE
    async def fake_wait_for(fut, timeout):
        class FakeRes:
            status = "success"
            message = "mock success"
            data = None
        return FakeRes()
        
    orig_wait_for = asyncio.wait_for
    asyncio.wait_for = fake_wait_for
    
    try:
        res = await orch.execute_task("open calc and get battery", "dev1", {}, None)
        assert res.type == "TASK_STARTED"
        assert len(orch.active_tasks) == 1
        task_id = list(orch.active_tasks.keys())[-1]
        
        import asyncio
        while orch.active_tasks[task_id].status == "RUNNING":
            await asyncio.sleep(0.01)
        
        state = orch.active_tasks[task_id]
        assert state.status == "COMPLETED"
        assert state.current_step == 2
    finally:
        asyncio.wait_for = orig_wait_for

@pytest.mark.asyncio
async def test_task_orchestrator_loop_protection():
    steps = [
        ModelResult(type="TASK_STEP", step_decision={
            "decision": "NEXT_ACTION",
            "action_plan": {
                "intent": "open_app",
                "source_device": "dev1",
                "capability": "app.launch",
                "action": "open_app",
                "arguments": {"app": "calc"},
                "confidence": 1.0,
                "user_facing_summary": "open calc",
                "requires_confirmation": False
            }
        }),
        ModelResult(type="TASK_STEP", step_decision={
            "decision": "NEXT_ACTION",
            "action_plan": {
                "intent": "open_app",
                "source_device": "dev1",
                "capability": "app.launch",
                "action": "open_app",
                "arguments": {"app": "calc"},
                "confidence": 1.0,
                "user_facing_summary": "open calc",
                "requires_confirmation": False
            }
        })
    ]
    router = MockRouterForTask(steps)
    control = MockControlForTask()
    orch = TaskOrchestrator(router, control)
    
    import asyncio
    async def fake_wait_for(fut, timeout):
        class FakeRes:
            status = "success"
            message = "mock success"
            data = None
        return FakeRes()
    
    orig_wait_for = asyncio.wait_for
    asyncio.wait_for = fake_wait_for
    try:
        res = await orch.execute_task("open calc infinitely", "dev1", {}, None)
        assert res.type == "TASK_STARTED"
        
        task_id = list(orch.active_tasks.keys())[-1]
        import asyncio
        while orch.active_tasks[task_id].status == "RUNNING":
            await asyncio.sleep(0.01)
        
        state = orch.active_tasks[task_id]
        assert state.status == "FAILED"
        assert state.current_step == 1
    finally:
        asyncio.wait_for = orig_wait_for

def test_task_step_parsing():
    import json
    from hub.intent_schema import ModelResult

    # Test NEXT_ACTION parsing
    raw_action = '''
    {
      "type": "TASK_STEP",
      "step_decision": {
        "decision": "NEXT_ACTION",
        "response_text": null,
        "action_plan": {
          "intent": "open_app",
          "source_device": "asus",
          "target_device": null,
          "target_device_reference": "this device",
          "capability": "app.launch",
          "action": "open_app",
          "arguments": {"app": "calculator"},
          "requires_confirmation": false,
          "confidence": 1.0,
          "user_facing_summary": "opening calculator"
        }
      }
    }
    '''
    res = ModelResult(**json.loads(raw_action))
    assert res.type == "TASK_STEP"
    assert res.step_decision["decision"] == "NEXT_ACTION"
    assert res.step_decision["action_plan"]["intent"] == "open_app"

    # Test FINAL_RESPONSE parsing
    raw_response = '''
    {
      "type": "TASK_STEP",
      "step_decision": {
        "decision": "FINAL_RESPONSE",
        "response_text": "I have opened the calculator.",
        "action_plan": null
      }
    }
    '''
    res2 = ModelResult(**json.loads(raw_response))
    assert res2.type == "TASK_STEP"
    assert res2.step_decision["decision"] == "FINAL_RESPONSE"
    assert res2.step_decision["response_text"] == "I have opened the calculator."

