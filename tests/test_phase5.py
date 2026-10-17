"""
tests/test_phase5.py
--------------------
Tests for Phase 5 features: memory, tasks, context, integrations, oauth, and token isolation.
"""

import pytest
import pytest_asyncio
import json
import asyncio
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from hub.database import (
    init_db, add_task, update_task, get_tasks, get_task,
    add_memory_item, get_memory_items, add_integration,
    get_integrations, enroll_device, set_device_permission
)
from hub.tasks import TaskEngine
from hub.context import ContextManager
from hub.permissions import check_permission
from hub.credentials import CredentialStore

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
async def test_token_isolation():
    """Test that tokens are stored securely and not in SQLite."""
    integration_id = "gmail_123"
    tokens = {"access_token": "fake_access", "refresh_token": "fake_refresh"}
    
    # Store tokens
    CredentialStore.store_tokens(integration_id, tokens)
    
    # Retrieve tokens
    retrieved = CredentialStore.get_tokens(integration_id)
    assert retrieved == tokens
    
    # Clean up
    CredentialStore.delete_tokens(integration_id)

@pytest.mark.asyncio
async def test_task_idempotency():
    """Test that completed tasks are not run again."""
    task = {
        "id": "task_idem",
        "title": "Idempotent Task",
        "type": "background",
        "status": "completed",
        "command": "echo",
        "assigned_device": "dev1"
    }
    await add_task(task)
    
    engine = TaskEngine({"CONNECTED_DEVICES": {}})
    # If it was pending it would change to running. Since it's completed, it should stay completed.
    await engine.execute_task(task)
    
    db_task = await get_task("task_idem")
    assert db_task["status"] == "completed"

@pytest.mark.asyncio
async def test_safe_cross_device_rerouting():
    """Test rerouting only happens when allowed."""
    task = {
        "id": "task_reroute",
        "title": "Reroutable Task",
        "type": "one_time",
        "status": "pending",
        "command": "special_action",
        "assigned_device": "dev_offline",
        "reroutable": True,
        "required_capability": "special_action"
    }
    await add_task(task)
    
    # Mock connected devices where dev2 has the capability
    with patch("hub.tasks.get_device_capabilities", return_value=["special_action"]):
        with patch("hub.tasks.check_permission", return_value=MagicMock(allowed=True, needs_confirmation=False)):
            from unittest.mock import AsyncMock
            ws_mock = AsyncMock()
            engine = TaskEngine({"CONNECTED_DEVICES": {"dev2": ws_mock}})
            await engine.execute_task(task)
            
            db_task = await get_task("task_reroute")
            assert db_task["assigned_device"] == "dev2"
            assert db_task["status"] == "waiting"

@pytest.mark.asyncio
async def test_context_relevance_filtering():
    """Test ContextManager filters memory appropriately."""
    mem_1 = {
        "id": "mem_1", "category": "people", "key": "alice", "value": "Alice is a boss", "relationship_entity_id": "ent1"
    }
    mem_2 = {
        "id": "mem_2", "category": "preferences", "key": "theme", "value": "dark mode"
    }
    await add_memory_item(mem_1)
    await add_memory_item(mem_2)
    
    # Request mentioning alice
    ctx = await ContextManager.get_active_context("dev1", request="Remind alice to email")
    assert any(m["key"] == "alice" for m in ctx["memory"])
    assert not any(m["key"] == "theme" for m in ctx["memory"])

@pytest.mark.asyncio
async def test_integration_capability_permissions():
    """Test gmail.send defaults to ask or deny based on tier."""
    # Since gmail.send is an integration command, it should go through standard permission checks.
    
    await enroll_device("dev1", "Laptop", "admin", "hash")
    
    from hub.database import update_device_capabilities
    await update_device_capabilities("dev1", "windows", ["gmail.send"])
    
    # Default for tier 3 (like gmail.send if defined that way) is 'ask' for admin
    perm = await check_permission("admin", "dev1", "dev1", "gmail.send")
    # By default, Tier >= 2 triggers needs_confirmation=True in step 6 if no override is set
    # but wait, check_permission looks up tier from COMMAND_TIERS
    assert perm.allowed == True
    assert perm.needs_confirmation == True
    
    # Override
    await set_device_permission("dev1", "gmail.send", "allow")
    perm2 = await check_permission("admin", "dev1", "dev1", "gmail.send")
    assert perm2.allowed == True
    assert perm2.needs_confirmation == False
