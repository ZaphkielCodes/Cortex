"""
tests/test_phase6_intent.py
---------------------------
Tests for Phase 6 Conversational Intent Pipeline.
"""

import pytest
import pytest_asyncio
import json
from unittest.mock import patch, AsyncMock

from hub.database import (
    init_db, enroll_device, update_device_capabilities,
    update_connection_state, set_device_permission
)
import sys
from hub.intent_schema import ActionPlan, ModelResult
from hub.agent_harness import AgentHarness
from hub.control_layer import CortexControlLayer
import hub.main

import tempfile
import os


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
async def test_resolve_device_reference():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    await enroll_device("dev2", "Living Room TV", "agent", "hash")
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    
    # "this laptop" resolves to source device
    res = await control.resolve_target_device("dev1", "this laptop")
    assert res == "dev1"
    
    # Exact name match
    res = await control.resolve_target_device("dev1", "living room tv")
    assert res == "dev2"
    
    # Role / fuzzy match
    res = await control.resolve_target_device("dev1", "tv")
    assert res == "dev2"
    
    # Ambiguous / Unknown
    res = await control.resolve_target_device("dev1", "spaceship")
    assert res is None

@pytest.mark.asyncio
async def test_valid_intent_mapping():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    await update_device_capabilities("dev1", "windows", ["app.launch"])
    await update_connection_state("dev1", "connected")
    await set_device_permission("dev1", "app.launch", "allow")
    hub.main.CONNECTED_DEVICES["dev1"] = AsyncMock()
    
    plan = ActionPlan(
        intent="open_app",
        source_device="dev1",
        target_device_reference="this laptop",
        capability="app.launch",
        action="launch",
        arguments={"app": "notepad"},
        confidence=0.9,
        user_facing_summary="opening notepad"
    )
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type='ACTION', action_plan=plan))
    
    result = await harness.process_request("dev1", "Open notepad here", {}, None)
    
    assert result.status == "success"
    assert result.action_plan.target_device == "dev1"
    assert result.command_id is not None
    
    hub.main.CONNECTED_DEVICES["dev1"].send_text.assert_called_once()

@pytest.mark.asyncio
async def test_ambiguous_device():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    
    plan = ActionPlan(
        intent="open_app",
        source_device="dev1",
        target_device_reference="unknown phone",
        capability="app.launch",
        action="launch",
        arguments={},
        confidence=0.9,
        user_facing_summary="opening app"
    )
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type='ACTION', action_plan=plan))
    
    result = await harness.process_request("dev1", "initiate the app on unknown phone", {}, None)
    assert result.status == "clarification_needed"
    assert "don't know which device" in result.message

@pytest.mark.asyncio
async def test_unsupported_capability():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    await update_device_capabilities("dev1", "windows", []) # No capabilities
    await update_connection_state("dev1", "connected")
    hub.main.CONNECTED_DEVICES["dev1"] = "mock_ws"
    
    plan = ActionPlan(
        intent="show_notification",
        source_device="dev1",
        target_device_reference="this laptop",
        capability="ui.notify", # unsupported
        action="notify",
        arguments={"msg": "hello"},
        confidence=0.9,
        user_facing_summary="showing notification"
    )
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type='ACTION', action_plan=plan))
    
    result = await harness.process_request("dev1", "trigger a notification for me", {}, None)
    assert result.status == "error"
    assert "does not support 'ui.notify'" in result.message

@pytest.mark.asyncio
async def test_permission_deny():
    await enroll_device("dev1", "Guest Phone", "sub_admin", "hash")
    await update_device_capabilities("dev1", "android", ["app.launch"])
    await update_connection_state("dev1", "connected")
    hub.main.CONNECTED_DEVICES["dev1"] = "mock_ws"
    
    # Deny app.launch
    await set_device_permission("dev1", "app.launch", "deny")
    
    plan = ActionPlan(
        intent="open_app",
        source_device="dev1",
        target_device_reference="this",
        capability="app.launch",
        action="launch",
        arguments={"app": "calc"},
        confidence=0.9,
        user_facing_summary="opening calc"
    )
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type='ACTION', action_plan=plan))
    
    result = await harness.process_request("dev1", "open calc", {}, None)
    assert result.status == "error"
    assert "don't have permission" in result.message

@pytest.mark.asyncio
async def test_permission_ask():
    await enroll_device("dev1", "Admin Phone", "admin", "hash")
    await update_device_capabilities("dev1", "android", ["app.launch"])
    await update_connection_state("dev1", "connected")
    hub.main.CONNECTED_DEVICES["dev1"] = "mock_ws"
    
    await set_device_permission("dev1", "app.launch", "ask")
    
    plan = ActionPlan(
        intent="open_app",
        source_device="dev1",
        target_device_reference="this",
        capability="app.launch",
        action="launch",
        arguments={"app": "calc"},
        confidence=0.9,
        user_facing_summary="opening calc"
    )
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type='ACTION', action_plan=plan))
    
    result = await harness.process_request("dev1", "execute calculation tool", {}, None)
    assert result.status == "approval_required"
    assert result.command_id is not None
    
    from hub.database import get_pending_confirmations
    pending = await get_pending_confirmations()
    assert len(pending) > 0
    req = pending[0]
    assert req["command_id"] == result.command_id
    assert req["command"] == "launch"

@pytest.mark.asyncio
async def test_malformed_llm_output():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(side_effect=ValueError("Bad JSON"))
    
    result = await harness.process_request("dev1", "execute some generic query", {}, None)
    assert result.status == "error"
    assert "Failed to" in result.message

@pytest.mark.asyncio
async def test_offline_target():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    await enroll_device("dev2", "TV", "agent", "hash")
    await update_device_capabilities("dev2", "tvos", ["ui.notify"])
    
    # dev1 is online, dev2 is offline
    await update_connection_state("dev1", "connected")
    hub.main.CONNECTED_DEVICES["dev1"] = "mock_ws"
    
    plan = ActionPlan(
        intent="show_notification",
        source_device="dev1",
        target_device_reference="tv",
        capability="ui.notify",
        action="notify",
        arguments={"msg": "hi"},
        confidence=0.9,
        user_facing_summary="notifying tv"
    )
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type='ACTION', action_plan=plan))
    
    result = await harness.process_request("dev1", "notify the tv", {}, None)
    assert result.status == "error"
    assert "is currently offline" in result.message

@pytest.mark.asyncio
async def test_approval_binding_and_generic_yes():
    # Test that approval is bound to the exact immutable action plan
    # and a generic 'yes' cannot bypass validation.
    await enroll_device("dev1", "Laptop", "admin", "hash")
    hub.main.CONNECTED_DEVICES["dev1"] = "mock_ws"
    
    plan = ActionPlan(
        intent="open_app", # Valid intent
        source_device="dev1",
        target_device_reference="this",
        capability="shell.execute", # Invalid capability for open_app!
        action="rm -rf /",
        arguments={},
        confidence=0.9,
        user_facing_summary="deleting everything"
    )
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type='ACTION', action_plan=plan))
    
    result = await harness.process_request("dev1", "yes do it", {}, None)
    # The pipeline should fail because intent "open_app" requires capability "app.launch"
    assert result.status == "error"
    assert "does not support 'app.launch'" in result.message

@pytest.mark.asyncio
async def test_relevant_context_filtering():
    from hub.context import ContextManager
    from hub.database import add_memory_item
    
    await add_memory_item({"id": "mem1", "category": "preference", "key": "color", "value": "blue"})
    await add_memory_item({"id": "mem2", "category": "preference", "key": "food", "value": "pizza"})
    
    await enroll_device("dev1", "Laptop", "admin", "hash")
    
    ctx = await ContextManager.get_active_context("dev1", request="what is my favorite food?")
    mem_keys = [m["key"] for m in ctx["memory"]]
    
    assert "food" in mem_keys
    assert "color" not in mem_keys

@pytest.mark.asyncio
async def test_no_direct_execution_bypass():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    
    plan = ActionPlan(
        intent="execute_arbitrary_shell",
        source_device="dev1",
        target_device_reference="this",
        capability="shell.execute",
        action="echo hacked",
        arguments={},
        confidence=0.9,
        user_facing_summary="hacking"
    )
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type='ACTION', action_plan=plan))
    
    result = await harness.process_request("dev1", "execute this command", {}, None)
    assert result.status == "error"
    assert "not supported" in result.message

@pytest.mark.asyncio
async def test_provider_timeout():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(side_effect=TimeoutError("Timed out"))
    
    result = await harness.process_request("dev1", "hello", {}, None)
    assert result.status == "error"
    assert "Timed out" in result.message

@pytest.mark.asyncio
async def test_provider_error():
    await enroll_device("dev1", "Laptop", "admin", "hash")
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(side_effect=Exception("API failure"))
    
    result = await harness.process_request("dev1", "hello", {}, None)
    assert result.status == "error"
    assert "API failure" in result.message

@pytest.mark.asyncio
async def test_extra_fields_in_action_plan():
    raw_json = {
        "intent": "open_app",
        "action": "launch",
        "capability": "app.launch",
        "source_device": "dev1",
        "target_device_reference": "this",
        "confidence": 0.9,
        "arguments": {"app": "notepad"},
        "user_facing_summary": "opening notepad",
        "hallucinated_field": "some_value"
    }
    
    plan = ActionPlan.model_validate(raw_json)
    assert not hasattr(plan, "hallucinated_field")
    assert plan.intent == "open_app"


@pytest.mark.asyncio
async def test_response_mode():
    await enroll_device('dev1', 'Laptop', 'admin', 'hash')
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    
    res = ModelResult(type='RESPONSE', response_text='Recursion is fun!')
    harness.router.generate_result = AsyncMock(return_value=res)
    
    result = await harness.process_request('dev1', 'what is recursion?', {}, None)
    assert result.status == 'success'
    assert result.message == 'Recursion is fun!'
    assert result.action_plan is None

@pytest.mark.asyncio
async def test_task_mode_implemented():
    await enroll_device('dev1', 'Laptop', 'admin', 'hash')
    
    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)
    
    res = ModelResult(type='TASK')
    harness.router.generate_result = AsyncMock(return_value=res)
    
    result = await harness.process_request('dev1', 'execute a multi step task', {}, None)
    assert result.status == 'success'

@pytest.mark.asyncio
async def test_deterministic_routing_app_launch():
    await enroll_device('dev1', 'Laptop', 'admin', 'hash')
    await update_device_capabilities('dev1', 'windows', ['app.launch'])
    await update_connection_state('dev1', 'connected')
    hub.main.CONNECTED_DEVICES['dev1'] = 'mock_ws'

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    control.execute_command = AsyncMock(return_value=None)
    harness = AgentHarness(control)
    # Mock LLM to throw exception to prove deterministic path didn't use it
    harness.router.generate_result = AsyncMock(side_effect=RuntimeError('LLM should not be called'))

    result = await harness.process_request('dev1', 'open calc', {}, None)
    
    assert result.status == 'success'
    control.execute_command.assert_called_once()
    args, kwargs = control.execute_command.call_args
    assert args[0] == 'dev1'
    assert args[1] == 'open_app'
    assert args[2]['app'] == 'calc'




@pytest.mark.asyncio
async def test_deterministic_routing_permission_ask():
    await enroll_device('dev1', 'Admin Phone', 'admin', 'hash')
    await update_device_capabilities('dev1', 'android', ['app.launch'])
    await update_connection_state('dev1', 'connected')
    hub.main.CONNECTED_DEVICES['dev1'] = 'mock_ws'
    
    await set_device_permission('dev1', 'app.launch', 'ask')

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    control.execute_command = AsyncMock()
    harness = AgentHarness(control)
    harness.router.generate_result = AsyncMock(side_effect=RuntimeError('LLM should not be called'))

    result = await harness.process_request('dev1', 'launch calc', {}, None)
    
    assert result.status == 'approval_required'
    assert result.command_id is not None
    control.execute_command.assert_not_called()
    
    from hub.database import get_pending_confirmations
    pending = await get_pending_confirmations()
    assert len(pending) > 0
    req = pending[0]
    assert req['command_id'] == result.command_id
    assert req['command'] == 'open_app'
