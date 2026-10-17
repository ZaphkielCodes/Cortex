import asyncio
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import hub.database as database
from hub.auth import generate_device_secret, hash_secret
from hub.main import app
from hub.permissions import check_permission




@pytest.mark.asyncio
async def test_capability_database():
    """Test updating and retrieving device capabilities in the database."""
    await database.enroll_device("test_dev", "Test", "admin", "hash")
    
    # Update capabilities
    await database.update_device_capabilities("test_dev", "Windows", ["open_app", "take_screenshot"])
    
    # Retrieve capabilities
    caps = await database.get_device_capabilities("test_dev")
    assert "open_app" in caps
    assert "take_screenshot" in caps
    assert "move_file" not in caps

@pytest.mark.asyncio
async def test_permission_overrides():
    """Test setting and getting permission overrides."""
    await database.enroll_device("test_dev", "Test", "admin", "hash")
    
    await database.set_device_permission("test_dev", "take_screenshot", "deny")
    await database.set_device_permission("test_dev", "run_shell", "ask")
    
    override1 = await database.get_device_permission("test_dev", "take_screenshot")
    assert override1 == "deny"
    
    override2 = await database.get_device_permission("test_dev", "run_shell")
    assert override2 == "ask"
    
    override3 = await database.get_device_permission("test_dev", "move_file")
    assert override3 is None

@pytest.mark.asyncio
async def test_permission_engine_unsupported_capability():
    """Permission engine denies actions if capability is not supported."""
    await database.enroll_device("test_dev", "Test", "sub_admin", "hash")
    # Device only supports 'open_app'
    await database.update_device_capabilities("test_dev", "Windows", ["open_app"])
    
    res = await check_permission("admin", "admin_dev", "test_dev", "take_screenshot")
    assert res.allowed is False
    assert "does not support capability" in res.reason

@pytest.mark.asyncio
async def test_permission_engine_allow_override():
    """'allow' override skips confirmation even for Tier 3."""
    await database.enroll_device("test_dev", "Test", "sub_admin", "hash")
    await database.update_device_capabilities("test_dev", "Windows", ["run_shell"])
    await database.set_device_permission("test_dev", "run_shell", "allow")
    
    # run_shell is Tier 3, normally needs confirmation
    res = await check_permission("admin", "admin_dev", "test_dev", "run_shell")
    assert res.allowed is True
    assert res.needs_confirmation is False

@pytest.mark.asyncio
async def test_permission_engine_deny_override():
    """'deny' override blocks action outright."""
    await database.enroll_device("test_dev", "Test", "sub_admin", "hash")
    await database.update_device_capabilities("test_dev", "Windows", ["take_screenshot"])
    await database.set_device_permission("test_dev", "take_screenshot", "deny")
    
    res = await check_permission("admin", "admin_dev", "test_dev", "take_screenshot")
    assert res.allowed is False

@pytest.mark.asyncio
async def test_permission_engine_ask_override():
    """'ask' override forces confirmation even for Tier 1."""
    await database.enroll_device("test_dev", "Test", "sub_admin", "hash")
    await database.update_device_capabilities("test_dev", "Windows", ["take_screenshot"])
    await database.set_device_permission("test_dev", "take_screenshot", "ask")
    
    # take_screenshot is Tier 1, normally allowed without confirmation
    res = await check_permission("admin", "admin_dev", "test_dev", "take_screenshot")
    assert res.allowed is True
    assert res.needs_confirmation is True

@pytest.mark.asyncio
async def test_rest_devices_detail():
    """GET /devices/{device_id} returns full detail."""
    secret = generate_device_secret()
    hashed = hash_secret(secret)
    await database.enroll_device("test_dev", "Test", "admin", hashed)
    await database.update_device_capabilities("test_dev", "Windows", ["open_app"])
    await database.set_device_permission("test_dev", "open_app", "ask")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/devices/test_dev", headers={
            "X-Cortex-Device": "test_dev",
            "X-Cortex-Secret": secret,
        })
        assert resp.status_code == 200
        data = resp.json()["device"]
        assert data["platform"] == "Windows"
        assert data["capabilities"] == ["open_app"]
        assert data["permissions"]["open_app"] == "ask"
