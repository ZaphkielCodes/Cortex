"""Explicit model decisions for wiring tests, never prompt-based fake inference."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from hub.intent_schema import ModelResult
from hub.agent_harness import AgentHarness
from test_agent_turn_runtime import ScriptedModel, call, control


def scripted_harness(decisions, results=(), answer="The verified result is available."):
    model = ScriptedModel(decisions, answer=answer)
    ctrl = control(list(results))
    return AgentHarness(ctrl, model), ctrl, model


class EvidenceModel:
    locality = "local_device"
    name = "grounded_model_fixture"
    model = "explicit-decisions"
    def __init__(self, capability, arguments=None):
        self.capability, self.arguments, self.calls = capability, arguments or {}, []
    async def generate_result(self, text, context, **kwargs):
        self.calls.append(context)
        if context.get("mode") == "result_interpretation":
            # Echo structured evidence for assertions; this does not test grammar.
            return ModelResult(type="RESPONSE", response_text="Evidence: " + json.dumps(context["tool_result"], default=str))
        return ModelResult(type="RESPONSE", response_text=json.dumps({"decision":"CALL_CAPABILITY","capability":self.capability,"arguments":self.arguments}))


async def runtime_harness():
    from hub.database import enroll_device
    from hub.control_layer import CortexControlLayer
    await enroll_device("asus", "Test source", "admin", "test-only-hash")
    model = EvidenceModel("system.runtime_state")
    return AgentHarness(CortexControlLayer(), model), model


class PlanModel:
    """Predetermined plans test validation/persistence, never language understanding."""
    locality = "local_device"
    name = "explicit_plan_fixture"
    def __init__(self, *plans):
        self.plans, self.calls = list(plans), []
    async def generate_result(self, text, context, **kwargs):
        self.calls.append((text, context))
        return ModelResult(type="PLAN", plan={"steps": self.plans.pop(0)})


def plan_step(capability, **arguments):
    return {"description": capability, "capability": capability, "arguments": arguments}


async def native_harness(monkeypatch, model):
    """Actual control/executor boundary with explicit local execution opt-in."""
    from hub.database import enroll_device, save_device_capabilities
    from hub.capability_registry import CanonicalCapabilityRegistry
    from hub.control_layer import CortexControlLayer
    monkeypatch.setenv("CORTEX_HUB_DEVICE_ID", "asus")
    monkeypatch.setenv("CORTEX_ENABLE_HUB_DESKTOP", "1")
    caps = list(CanonicalCapabilityRegistry.get_instance().get_all())
    await enroll_device("asus", "Isolated test source", "admin", "test-hash")
    await save_device_capabilities("asus", caps)
    control([])  # Enrolled, online, canonical executor handle.
    ctrl = CortexControlLayer()
    return AgentHarness(ctrl, model), ctrl


class SequenceEvidenceModel(EvidenceModel):
    def __init__(self, decisions):
        super().__init__(None)
        self.decisions = list(decisions)
    async def generate_result(self, text, context, **kwargs):
        if context.get("mode") == "result_interpretation":
            return await super().generate_result(text, context, **kwargs)
        self.calls.append(context)
        return ModelResult(type="RESPONSE",response_text=json.dumps(self.decisions.pop(0)))
