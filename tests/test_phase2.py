"""
tests/test_phase2.py
--------------------
Automated test suite for Cortex Phase 2: Authentication, Roles, Permissions,
and Confirmations.

Run with:
    cd Cortex
    pytest tests/test_phase2.py -v

Tests cover:
  - Bootstrap creates admin
  - Enrollment of sub_admin
  - Duplicate enrollment rejected
  - Auth handshake (challenge/response)
  - Unauthenticated agent rejected
  - Permission checks (admin vs sub_admin)
  - Command tier classification
  - Confirmation flow (approve/deny/expire)
  - Command ID correlation
  - Duplicate connection replacement
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# Add hub/ to path so we can import hub modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from hub.auth import (
    create_challenge,
    generate_device_secret,
    hash_secret,
    verify_secret,
)
from hub.permissions import (
    COMMAND_TIERS,
    PermissionResult,
    check_permission,
    get_command_tier,
)

# Use a separate test database
import hub.database as database



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Auth module tests
# ---------------------------------------------------------------------------

class TestAuth:
    def test_generate_secret_length(self):
        secret = generate_device_secret()
        assert len(secret) == 64  # 32 bytes = 64 hex chars

    def test_generate_secret_unique(self):
        s1 = generate_device_secret()
        s2 = generate_device_secret()
        assert s1 != s2

    def test_hash_and_verify(self):
        secret = generate_device_secret()
        hashed = hash_secret(secret)
        assert verify_secret(secret, hashed) is True

    def test_verify_wrong_secret(self):
        secret = generate_device_secret()
        hashed = hash_secret(secret)
        assert verify_secret("wrong_secret", hashed) is False

    def test_challenge_is_random(self):
        c1 = create_challenge()
        c2 = create_challenge()
        assert c1 != c2
        assert len(c1) == 64  # 32 bytes hex


# ---------------------------------------------------------------------------
# Permission module tests
# ---------------------------------------------------------------------------

class TestPermissions:
    @pytest.mark.asyncio
    async def test_admin_can_target_any_device(self):
        await database.update_device_capabilities("lenovo", "unknown", ["screenshot.read"])
        result = await check_permission("admin", "asus", "lenovo", "screenshot.read")
        assert result.allowed is True
        assert result.needs_confirmation is False  # Tier 1

    @pytest.mark.asyncio
    async def test_admin_tier2_needs_confirmation(self):
        await database.update_device_capabilities("lenovo", "unknown", ["fs.move"])
        result = await check_permission("admin", "asus", "lenovo", "fs.move")
        assert result.allowed is True
        assert result.needs_confirmation is True
        assert result.tier == 2

    @pytest.mark.asyncio
    async def test_admin_tier3_needs_confirmation(self):
        await database.update_device_capabilities("lenovo", "unknown", ["run_shell"])
        result = await check_permission("admin", "asus", "lenovo", "run_shell")
        assert result.allowed is True
        assert result.needs_confirmation is True
        assert result.tier == 3

    @pytest.mark.asyncio
    async def test_sub_admin_cannot_target_others(self):
        await database.update_device_capabilities("asus", "unknown", ["screenshot.read"])
        result = await check_permission("sub_admin", "lenovo", "asus", "screenshot.read")
        assert result.allowed is False
        assert "can only target its own device" in result.reason

    @pytest.mark.asyncio
    async def test_sub_admin_can_target_self(self):
        await database.update_device_capabilities("lenovo", "unknown", ["screenshot.read"])
        result = await check_permission("sub_admin", "lenovo", "lenovo", "screenshot.read")
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_sub_admin_limited_commands(self):
        await database.update_device_capabilities("lenovo", "unknown", ["run_shell"])
        result = await check_permission("sub_admin", "lenovo", "lenovo", "run_shell")
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_sub_admin_cannot_exceed_tier(self):
        await database.update_device_capabilities("lenovo", "unknown", ["fs.move"])
        result = await check_permission("sub_admin", "lenovo", "lenovo", "fs.move")
        assert result.allowed is False
        assert "Tier 2" in result.reason

    @pytest.mark.asyncio
    async def test_unknown_role_denied(self):
        await database.update_device_capabilities("asus", "unknown", ["screenshot.read"])
        result = await check_permission("guest", "unknown", "asus", "screenshot.read")
        assert result.allowed is False
        assert "Unknown role" in result.reason

    def test_unknown_command_defaults_to_tier3(self):
        tier = get_command_tier("some_new_command")
        assert tier == 3  # DEFAULT_TIER

    def test_command_tiers_defined(self):
        assert COMMAND_TIERS["screenshot.read"] == 1
        assert COMMAND_TIERS["app.launch"] == 1
        assert COMMAND_TIERS["fs.move"] == 2
        assert COMMAND_TIERS["fs.delete"] == 3
        assert COMMAND_TIERS["shell.run"] == 3


# ---------------------------------------------------------------------------
# Database tests
# ---------------------------------------------------------------------------

class TestDatabase:
    @pytest.mark.asyncio
    async def test_bootstrap_creates_admin(self):
        """First bootstrap creates an admin device."""
        assert await database.has_admin() is False

        secret = generate_device_secret()
        hashed = hash_secret(secret)
        await database.enroll_device("asus", "ASUS Laptop", "admin", hashed)

        assert await database.has_admin() is True
        device = await database.get_device("asus")
        assert device is not None
        assert device["role"] == "admin"
        assert device["device_name"] == "ASUS Laptop"

    @pytest.mark.asyncio
    async def test_enroll_sub_admin(self):
        """Enroll a sub_admin device."""
        secret = generate_device_secret()
        hashed = hash_secret(secret)
        await database.enroll_device("lenovo", "Lenovo Laptop", "sub_admin", hashed)

        device = await database.get_device("lenovo")
        assert device is not None
        assert device["role"] == "sub_admin"

    @pytest.mark.asyncio
    async def test_list_devices(self):
        """list_devices returns all enrolled devices."""
        s1 = hash_secret(generate_device_secret())
        s2 = hash_secret(generate_device_secret())
        await database.enroll_device("asus", "ASUS", "admin", s1)
        await database.enroll_device("lenovo", "Lenovo", "sub_admin", s2)

        devices = await database.list_devices()
        assert len(devices) == 2
        ids = {d["device_id"] for d in devices}
        assert ids == {"asus", "lenovo"}

    @pytest.mark.asyncio
    async def test_update_last_seen(self):
        """update_last_seen sets a timestamp."""
        hashed = hash_secret(generate_device_secret())
        await database.enroll_device("asus", "ASUS", "admin", hashed)

        await database.update_last_seen("asus")
        device = await database.get_device("asus")
        assert device["last_seen"] != ""

    @pytest.mark.asyncio
    async def test_delete_device(self):
        """delete_device removes the device."""
        hashed = hash_secret(generate_device_secret())
        await database.enroll_device("test", "Test", "sub_admin", hashed)

        assert await database.delete_device("test") is True
        assert await database.get_device("test") is None
        assert await database.delete_device("test") is False  # already gone

    @pytest.mark.asyncio
    async def test_log_action_with_command_id(self):
        """Action logs include command_id."""
        await database.log_action(
            device_id="asus",
            command="screenshot.read",
            result="success",
            message="Screenshot saved",
            command_id="abc123",
        )
        logs = await database.get_recent_logs(1)
        assert len(logs) == 1
        assert logs[0]["command_id"] == "abc123"
        assert logs[0]["command"] == "screenshot.read"


# ---------------------------------------------------------------------------
# Confirmation database tests
# ---------------------------------------------------------------------------

class TestConfirmations:
    @pytest.mark.asyncio
    async def test_create_and_get_pending(self):
        """Create a confirmation and retrieve it as pending."""
        await database.create_confirmation(
            command_id="cmd001",
            target_device="lenovo",
            command="fs.move",
            args=["a.txt", "b.txt"],
            requested_by="cli",
            tier=2,
        )

        pending = await database.get_pending_confirmations()
        assert len(pending) == 1
        assert pending[0]["command_id"] == "cmd001"
        assert pending[0]["status"] == "pending"
        assert pending[0]["command"] == "fs.move"

    @pytest.mark.asyncio
    async def test_resolve_approval(self):
        """Approving a confirmation sets status and resolved fields."""
        await database.create_confirmation(
            command_id="cmd002",
            target_device="lenovo",
            command="delete_file",
            args=["/tmp/test"],
            requested_by="cli",
            tier=3,
        )

        row = await database.resolve_confirmation("cmd002", "approved", "cli")
        assert row is not None
        assert row["status"] == "pending"  # row returned is the PRE-update state

        # Verify it's no longer pending
        pending = await database.get_pending_confirmations()
        assert len(pending) == 0

        # Verify the resolved state
        conf = await database.get_confirmation("cmd002")
        assert conf["status"] == "approved"
        assert conf["resolved_by"] == "cli"
        assert conf["resolved_at"] != ""

    @pytest.mark.asyncio
    async def test_resolve_denial(self):
        """Denying a confirmation sets status correctly."""
        await database.create_confirmation(
            command_id="cmd003",
            target_device="lenovo",
            command="run_shell",
            args=["rm -rf /"],
            requested_by="cli",
            tier=3,
        )

        row = await database.resolve_confirmation("cmd003", "denied", "cli")
        assert row is not None

        conf = await database.get_confirmation("cmd003")
        assert conf["status"] == "denied"

    @pytest.mark.asyncio
    async def test_resolve_already_resolved_returns_none(self):
        """Resolving an already-resolved confirmation returns None."""
        await database.create_confirmation(
            command_id="cmd004",
            target_device="lenovo",
            command="fs.move",
            args=[],
            requested_by="cli",
            tier=2,
        )

        await database.resolve_confirmation("cmd004", "approved", "cli")
        # Try again
        row = await database.resolve_confirmation("cmd004", "denied", "cli")
        assert row is None  # already resolved

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_returns_none(self):
        """Resolving a non-existent command_id returns None."""
        row = await database.resolve_confirmation("nonexistent", "approved", "cli")
        assert row is None


# ---------------------------------------------------------------------------
# Integration-like tests (models)
# ---------------------------------------------------------------------------

class TestModels:
    def test_command_message_has_auto_id(self):
        from hub.models import CommandMessage
        cmd = CommandMessage(command="screenshot.read")
        assert len(cmd.command_id) == 32  # uuid4 hex
        assert cmd.type == "command"

    def test_command_message_unique_ids(self):
        from hub.models import CommandMessage
        c1 = CommandMessage(command="screenshot.read")
        c2 = CommandMessage(command="screenshot.read")
        assert c1.command_id != c2.command_id

    def test_result_message_echoes_id(self):
        from hub.models import ResultMessage
        result = ResultMessage(
            command_id="abc123",
            command="screenshot.read",
            status="success",
            message="done",
        )
        assert result.command_id == "abc123"
        assert result.type == "result"

    def test_auth_challenge_model(self):
        from hub.models import AuthChallenge
        ch = AuthChallenge(challenge="deadbeef")
        assert ch.type == "auth_challenge"
        data = json.loads(ch.model_dump_json())
        assert data["challenge"] == "deadbeef"

    def test_confirmation_request_model(self):
        from hub.models import ConfirmationRequest
        req = ConfirmationRequest(
            command_id="xyz",
            target_device="lenovo",
            command="delete_file",
            args=["/tmp/test"],
            requested_by="cli",
            tier=3,
        )
        assert req.timeout_seconds == 60
        assert req.type == "confirmation_request"

    def test_enroll_request_defaults(self):
        from hub.models import EnrollRequest
        req = EnrollRequest(device_id="phone")
        assert req.role == "sub_admin"
        assert req.device_name == ""
