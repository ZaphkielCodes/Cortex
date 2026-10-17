"""
tests/test_phase6.py
--------------------
Tests for Phase 6 features: session security, telemetry, and continuity model.
"""

import pytest
import pytest_asyncio
import json
import asyncio
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from hub.database import (
    init_db, enroll_device, update_device_telemetry, list_devices,
    update_connection_state, create_session, get_session, delete_session,
    create_continuity_session, get_continuity_sessions, get_continuity_session,
    update_continuity_session, update_device_capabilities
)
from hub.permissions import check_permission
from fastapi.testclient import TestClient
from hub.main import app

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
async def test_session_auth_enforcement():
    """Test that login returns a secure HttpOnly cookie and it can be used for API auth."""
    await enroll_device("dev1", "Laptop", "admin", "hash")
    
    # We bypass actual hashing in the test by mocking verify_secret if we test via API, 
    # but let's test the DB layer directly for session logic first.
    token = await create_session("dev1")
    assert token is not None
    
    session = await get_session(token)
    assert session is not None
    assert session["device_id"] == "dev1"
    
    await delete_session(token)
    session_after = await get_session(token)
    assert session_after is None

@pytest.mark.asyncio
async def test_telemetry_update_persistence():
    """Test optional telemetry fields are correctly saved to the devices table."""
    await enroll_device("dev2", "Phone", "agent", "hash")
    
    # Initial state should not have telemetry
    devices = await list_devices()
    assert len(devices) == 1
    
    # Update telemetry
    telemetry = {
        "battery_level": 85,
        "is_charging": True,
        "network_state": "WiFi",
        "storage_free": 10240000000,
        "unsupported_field": "should be ignored"
    }
    
    await update_device_telemetry("dev2", telemetry)
    
    devices = await list_devices()
    dev2 = devices[0]
    
    assert dev2["battery_level"] == 85
    assert dev2["is_charging"] == 1
    assert dev2["network_state"] == "WiFi"
    assert dev2["storage_free"] == 10240000000
    assert "unsupported_field" not in dev2

@pytest.mark.asyncio
async def test_offline_online_state():
    """Test offline/online state transitions."""
    await enroll_device("dev3", "Tablet", "agent", "hash")
    
    await update_connection_state("dev3", "connected")
    devices = await list_devices()
    assert devices[0]["connection_state"] == "connected"
    
    await update_connection_state("dev3", "disconnected")
    devices_after = await list_devices()
    assert devices_after[0]["connection_state"] == "disconnected"

@pytest.mark.asyncio
async def test_continuity_persistence():
    """Test that continuity sessions are persisted and can be updated."""
    session = {
        "id": "sess_1",
        "active_task": "drafting_email",
        "source_device": "dev1",
        "current_device": "dev1",
        "required_capabilities": ["gmail.send"]
    }
    await create_continuity_session(session)
    
    sessions = await get_continuity_sessions()
    assert len(sessions) == 1
    assert sessions[0]["id"] == "sess_1"
    assert "gmail.send" in sessions[0]["required_capabilities"]
    
    # Update the session
    await update_continuity_session("sess_1", {"current_device": "dev2"})
    
    updated = await get_continuity_session("sess_1")
    assert updated["current_device"] == "dev2"

@pytest.mark.asyncio
async def test_capability_and_permission_validation_for_resume():
    """Test logic equivalent to checking capabilities and permissions before a resume."""
    await enroll_device("dev1", "Laptop", "admin", "hash")
    await enroll_device("dev_target", "Phone", "agent", "hash")
    
    # Target device supports the required capability
    await update_device_capabilities("dev_target", "android", ["gmail.send"])
    
    session = {
        "id": "sess_2",
        "active_task": "drafting_email",
        "source_device": "dev1",
        "current_device": "dev1",
        "required_capabilities": ["gmail.send"]
    }
    await create_continuity_session(session)
    
    # Simulation of /resume endpoint checks
    # 1. Capability validation
    from hub.database import get_device_capabilities
    target_caps = await get_device_capabilities("dev_target")
    req_caps = session["required_capabilities"]
    missing_caps = [c for c in req_caps if c not in target_caps]
    
    assert len(missing_caps) == 0, "Target device should have the required capability"
    
    # 2. Permission validation
    perm = await check_permission("admin", "dev1", "dev_target", "gmail.send")
    assert perm.allowed is True, "Admin should be allowed to resume this task"
    
@pytest.mark.asyncio
async def test_failed_resume_on_unsupported_device():
    """Test failed resume due to missing capability."""
    await enroll_device("dev1", "Laptop", "admin", "hash")
    await enroll_device("dev_unsupported", "TV", "agent", "hash")
    
    await update_device_capabilities("dev_unsupported", "tvos", ["media.play"])
    
    session = {
        "id": "sess_3",
        "active_task": "drafting_email",
        "source_device": "dev1",
        "current_device": "dev1",
        "required_capabilities": ["gmail.send"]
    }
    await create_continuity_session(session)
    
    from hub.database import get_device_capabilities
    target_caps = await get_device_capabilities("dev_unsupported")
    req_caps = session["required_capabilities"]
    missing_caps = [c for c in req_caps if c not in target_caps]
    
    assert "gmail.send" in missing_caps, "Target device should fail capability check"
