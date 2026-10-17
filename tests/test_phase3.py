"""
tests/test_phase3.py
--------------------
Automated test suite for Cortex Phase 3: Secure Cross-Network Connectivity.

Run with:
    cd Cortex
    pytest tests/test_phase3.py -v

Tests cover:
  - Peer-address filtering (default-deny, localhost, Tailscale, LAN rejection)
  - CLI authentication (required, wrong secret, correct auth, role from DB)
  - HTTP endpoint authentication
  - Bootstrap localhost-only restriction
  - Command duplicate protection (persisted, survives restart)
  - Command expiry (hub receipt time, survives restart)
  - Command history pruning
  - Secret rotation (admin-only, invalidates old secret)
  - Connection state tracking
  - Configuration module
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
from hub.config import is_trusted_peer

# Use a separate test database
import hub.database as database



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------





async def _enroll_admin(device_id="asus", name="ASUS Laptop"):
    """Helper: enroll an admin device and return (device_id, secret, hash)."""
    secret = generate_device_secret()
    hashed = hash_secret(secret)
    await database.enroll_device(device_id, name, "admin", hashed)
    return device_id, secret, hashed


async def _enroll_sub_admin(device_id="lenovo", name="Lenovo Laptop"):
    """Helper: enroll a sub_admin device and return (device_id, secret, hash)."""
    secret = generate_device_secret()
    hashed = hash_secret(secret)
    await database.enroll_device(device_id, name, "sub_admin", hashed)
    return device_id, secret, hashed


# ---------------------------------------------------------------------------
# Peer-address filtering tests
# ---------------------------------------------------------------------------

class TestPeerFilter:
    def test_localhost_ipv4_allowed(self):
        assert is_trusted_peer("127.0.0.1") is True

    def test_localhost_ipv6_allowed(self):
        assert is_trusted_peer("::1") is True

    def test_localhost_127_x_allowed(self):
        assert is_trusted_peer("127.0.0.2") is True

    def test_tailscale_ip_allowed(self):
        """100.100.50.3 is within 100.64.0.0/10 (Tailscale CGNAT)."""
        assert is_trusted_peer("100.100.50.3") is True

    def test_tailscale_range_start_allowed(self):
        """100.64.0.1 is the start of the Tailscale range."""
        assert is_trusted_peer("100.64.0.1") is True

    def test_tailscale_range_end_allowed(self):
        """100.127.255.254 is near the end of the Tailscale range."""
        assert is_trusted_peer("100.127.255.254") is True

    def test_lan_peer_rejected_by_default(self):
        """192.168.x.x should be rejected when ALLOW_LAN is False."""
        assert is_trusted_peer("192.168.1.50") is False

    def test_lan_10_rejected_by_default(self):
        """10.0.0.x should be rejected when ALLOW_LAN is False."""
        assert is_trusted_peer("10.0.0.1") is False

    def test_lan_172_rejected_by_default(self):
        """172.16.x.x should be rejected when ALLOW_LAN is False."""
        assert is_trusted_peer("172.16.0.1") is False

    def test_public_ip_rejected(self):
        """Public IPs should always be rejected."""
        assert is_trusted_peer("8.8.8.8") is False

    def test_public_ip_rejected_2(self):
        assert is_trusted_peer("203.0.113.1") is False

    def test_invalid_ip_rejected(self):
        assert is_trusted_peer("not-an-ip") is False

    def test_empty_string_rejected(self):
        assert is_trusted_peer("") is False

    def test_ipv4_mapped_ipv6_localhost(self):
        """::ffff:127.0.0.1 should be treated as localhost."""
        assert is_trusted_peer("::ffff:127.0.0.1") is True

    def test_ipv4_mapped_ipv6_tailscale(self):
        """::ffff:100.100.50.3 should be treated as Tailscale."""
        assert is_trusted_peer("::ffff:100.100.50.3") is True

    def test_ipv4_mapped_ipv6_lan_rejected(self):
        """::ffff:192.168.1.50 should be rejected."""
        assert is_trusted_peer("::ffff:192.168.1.50") is False

    def test_allow_lan_override(self):
        """With ALLOW_LAN, LAN peers should be accepted."""
        import hub.config as config
        original = config.TRUSTED_NETWORKS
        try:
            config.ALLOW_LAN = True
            config.TRUSTED_NETWORKS = config._build_trusted_networks()
            assert is_trusted_peer("192.168.1.50") is True
            assert is_trusted_peer("10.0.0.1") is True
            assert is_trusted_peer("172.16.0.1") is True
            # Public IP should still be rejected
            assert is_trusted_peer("8.8.8.8") is False
        finally:
            config.ALLOW_LAN = False
            config.TRUSTED_NETWORKS = original

    def test_outside_tailscale_cgnat_rejected(self):
        """100.128.0.1 is outside 100.64.0.0/10 — should be rejected."""
        assert is_trusted_peer("100.128.0.1") is False

    def test_100_63_rejected(self):
        """100.63.255.255 is just below the Tailscale range."""
        assert is_trusted_peer("100.63.255.255") is False


# ---------------------------------------------------------------------------
# Command duplicate protection tests
# ---------------------------------------------------------------------------

class TestCommandDuplicate:
    @pytest.mark.asyncio
    async def test_record_command_success(self):
        """First record of a command_id succeeds."""
        result = await database.record_command(
            "cmd_001", "take_screenshot", "lenovo", "cli"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_duplicate_command_rejected(self):
        """Second record of the same command_id is rejected."""
        await database.record_command("cmd_002", "take_screenshot", "lenovo", "cli")
        result = await database.record_command("cmd_002", "open_app", "asus", "cli")
        assert result is False

    @pytest.mark.asyncio
    async def test_is_duplicate_command(self):
        """is_duplicate_command returns True for existing command_ids."""
        await database.record_command("cmd_003", "take_screenshot", "lenovo", "cli")
        assert await database.is_duplicate_command("cmd_003") is True
        assert await database.is_duplicate_command("cmd_999") is False

    @pytest.mark.asyncio
    async def test_duplicate_survives_restart(self):
        """Duplicate protection persists across DB re-initialization (simulated restart)."""
        await database.record_command("cmd_004", "take_screenshot", "lenovo", "cli")

        # Simulate hub restart — re-init DB (but don't delete the file)
        await database.init_db()

        # The command should still be detected as duplicate
        assert await database.is_duplicate_command("cmd_004") is True
        result = await database.record_command("cmd_004", "open_app", "asus", "cli")
        assert result is False

    @pytest.mark.asyncio
    async def test_command_recorded_with_hub_timestamp(self):
        """Recorded commands have a hub-generated received_at timestamp."""
        await database.record_command("cmd_005", "take_screenshot", "lenovo", "cli")
        received_at = await database.get_command_received_at("cmd_005")
        assert received_at is not None
        # Verify it's a valid ISO timestamp
        dt = datetime.fromisoformat(received_at)
        assert dt.year >= 2024


# ---------------------------------------------------------------------------
# Command expiry tests
# ---------------------------------------------------------------------------

class TestCommandExpiry:
    @pytest.mark.asyncio
    async def test_fresh_command_not_expired(self):
        """A just-recorded command should not be expired."""
        await database.record_command("cmd_fresh", "take_screenshot", "lenovo", "cli")
        assert await database.is_command_expired("cmd_fresh", 300) is False

    @pytest.mark.asyncio
    async def test_old_command_expired(self):
        """A command older than TTL should be expired."""
        import aiosqlite

        # Insert a command with a received_at 6 minutes ago
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO command_history
                    (command_id, command, target_device, requested_by, received_at, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                ("cmd_old", "take_screenshot", "lenovo", "cli", old_time),
            )
            await db.commit()

        # With 300s (5min) TTL, a 6-min-old command should be expired
        assert await database.is_command_expired("cmd_old", 300) is True

    @pytest.mark.asyncio
    async def test_expiry_survives_restart(self):
        """Expiry checks work after DB re-initialization (simulated restart)."""
        import aiosqlite

        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO command_history
                    (command_id, command, target_device, requested_by, received_at, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                ("cmd_old_restart", "take_screenshot", "lenovo", "cli", old_time),
            )
            await db.commit()

        # Simulate restart
        await database.init_db()

        # Should still be expired
        assert await database.is_command_expired("cmd_old_restart", 300) is True

    @pytest.mark.asyncio
    async def test_nonexistent_command_not_expired(self):
        """A command that doesn't exist should not be reported as expired."""
        assert await database.is_command_expired("nonexistent", 300) is False


# ---------------------------------------------------------------------------
# Command history pruning tests
# ---------------------------------------------------------------------------

class TestPruning:
    @pytest.mark.asyncio
    async def test_prune_removes_old_entries(self):
        """Commands older than retention period are pruned."""
        import aiosqlite

        # Insert a command from 25 hours ago
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO command_history
                    (command_id, command, target_device, requested_by, received_at, status)
                VALUES (?, ?, ?, ?, ?, 'dispatched')
                """,
                ("cmd_ancient", "take_screenshot", "lenovo", "cli", old_time),
            )
            await db.commit()

        deleted = await database.prune_command_history(24)
        assert deleted >= 1
        assert await database.is_duplicate_command("cmd_ancient") is False

    @pytest.mark.asyncio
    async def test_prune_keeps_recent_entries(self):
        """Commands within the retention period are kept."""
        await database.record_command("cmd_recent", "take_screenshot", "lenovo", "cli")

        deleted = await database.prune_command_history(24)
        # The recent command should still exist
        assert await database.is_duplicate_command("cmd_recent") is True

    @pytest.mark.asyncio
    async def test_command_status_update(self):
        """update_command_status changes the status field."""
        await database.record_command("cmd_status", "take_screenshot", "lenovo", "cli")
        await database.update_command_status("cmd_status", "dispatched")

        import aiosqlite
        async with aiosqlite.connect(database.DB_PATH) as db:
            async with db.execute(
                "SELECT status, dispatched_at FROM command_history WHERE command_id = ?",
                ("cmd_status",),
            ) as cursor:
                row = await cursor.fetchone()
        assert row[0] == "dispatched"
        assert row[1] != ""  # dispatched_at should be set


# ---------------------------------------------------------------------------
# Secret rotation tests
# ---------------------------------------------------------------------------

class TestSecretRotation:
    @pytest.mark.asyncio
    async def test_rotate_success(self):
        """Rotating a secret replaces the hash and invalidates the old secret."""
        _, old_secret, _ = await _enroll_admin()

        new_secret = generate_device_secret()
        new_hash = hash_secret(new_secret)
        result = await database.rotate_device_secret("asus", new_hash)
        assert result is True

        device = await database.get_device("asus")
        # Old secret should fail
        assert verify_secret(old_secret, device["secret_hash"]) is False
        # New secret should pass
        assert verify_secret(new_secret, device["secret_hash"]) is True

    @pytest.mark.asyncio
    async def test_rotate_nonexistent_device(self):
        """Rotating a non-existent device returns False."""
        new_hash = hash_secret(generate_device_secret())
        result = await database.rotate_device_secret("ghost", new_hash)
        assert result is False

    @pytest.mark.asyncio
    async def test_rotate_invalidates_old_immediately(self):
        """After rotation, the old secret must fail verification immediately."""
        _, old_secret, old_hash = await _enroll_admin()

        # Verify old secret works before rotation
        assert verify_secret(old_secret, old_hash) is True

        new_secret = generate_device_secret()
        new_hash = hash_secret(new_secret)
        await database.rotate_device_secret("asus", new_hash)

        # Fetch the updated device
        device = await database.get_device("asus")
        # Old secret against new hash → fail
        assert verify_secret(old_secret, device["secret_hash"]) is False


# ---------------------------------------------------------------------------
# Connection state tests
# ---------------------------------------------------------------------------

class TestConnectionState:
    @pytest.mark.asyncio
    async def test_initial_state_disconnected(self):
        """New devices start as 'disconnected'."""
        await _enroll_admin()
        state = await database.get_connection_state("asus")
        assert state == "disconnected"

    @pytest.mark.asyncio
    async def test_update_to_connected(self):
        """update_connection_state changes to 'connected'."""
        await _enroll_admin()
        await database.update_connection_state("asus", "connected")
        state = await database.get_connection_state("asus")
        assert state == "connected"

    @pytest.mark.asyncio
    async def test_update_to_disconnected(self):
        """update_connection_state changes back to 'disconnected'."""
        await _enroll_admin()
        await database.update_connection_state("asus", "connected")
        await database.update_connection_state("asus", "disconnected")
        state = await database.get_connection_state("asus")
        assert state == "disconnected"

    @pytest.mark.asyncio
    async def test_nonexistent_device_state_is_none(self):
        """get_connection_state returns None for non-enrolled devices."""
        state = await database.get_connection_state("ghost")
        assert state is None


# ---------------------------------------------------------------------------
# Database migration tests
# ---------------------------------------------------------------------------

class TestMigration:
    @pytest.mark.asyncio
    async def test_migration_is_idempotent(self):
        """Running init_db multiple times does not error."""
        await database.init_db()
        await database.init_db()
        await database.init_db()
        # If we got here without error, migrations are idempotent

    @pytest.mark.asyncio
    async def test_existing_devices_preserved_after_migration(self):
        """Devices enrolled before migration retain all data."""
        _, secret, _ = await _enroll_admin()

        # Re-run init (simulating upgrade)
        await database.init_db()

        device = await database.get_device("asus")
        assert device is not None
        assert device["role"] == "admin"
        assert device["device_name"] == "ASUS Laptop"
        assert verify_secret(secret, device["secret_hash"]) is True


# ---------------------------------------------------------------------------
# Integration tests with FastAPI TestClient
# ---------------------------------------------------------------------------

class TestHTTPAuth:
    """Test that HTTP endpoints require authentication (Phase 3)."""

    @pytest.mark.asyncio
    async def test_devices_requires_auth(self):
        """GET /devices without headers returns 401."""
        from httpx import AsyncClient, ASGITransport
        from hub.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            resp = await client.get("/devices")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_logs_requires_auth(self):
        """GET /logs without headers returns 401."""
        from httpx import AsyncClient, ASGITransport
        from hub.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            resp = await client.get("/logs")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_pending_requires_auth(self):
        """GET /pending without headers returns 401."""
        from httpx import AsyncClient, ASGITransport
        from hub.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            resp = await client.get("/pending")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_devices_with_valid_auth(self):
        """GET /devices with valid auth headers returns 200."""
        from httpx import AsyncClient, ASGITransport
        from hub.main import app

        _, secret, _ = await _enroll_admin()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            resp = await client.get(
                "/devices",
                headers={"X-Cortex-Device": "asus", "X-Cortex-Secret": secret},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "devices" in data

    @pytest.mark.asyncio
    async def test_devices_with_wrong_secret(self):
        """GET /devices with wrong secret returns 401."""
        from httpx import AsyncClient, ASGITransport
        from hub.main import app

        await _enroll_admin()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            resp = await client.get(
                "/devices",
                headers={"X-Cortex-Device": "asus", "X-Cortex-Secret": "wrong"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_rotate_admin_only(self):
        """POST /rotate-secret by non-admin returns 403."""
        from httpx import AsyncClient, ASGITransport
        from hub.main import app

        await _enroll_admin()
        _, sub_secret, _ = await _enroll_sub_admin()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            resp = await client.post(
                "/rotate-secret",
                json={"target_device_id": "asus"},
                headers={"X-Cortex-Device": "lenovo", "X-Cortex-Secret": sub_secret},
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_rotate_success(self):
        """POST /rotate-secret by admin succeeds and returns new secret."""
        from httpx import AsyncClient, ASGITransport
        from hub.main import app

        _, admin_secret, _ = await _enroll_admin()
        await _enroll_sub_admin()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            resp = await client.post(
                "/rotate-secret",
                json={"target_device_id": "lenovo"},
                headers={"X-Cortex-Device": "asus", "X-Cortex-Secret": admin_secret},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["device_id"] == "lenovo"
            assert len(data["new_secret"]) == 64  # 32 bytes hex

            # Verify new secret works
            device = await database.get_device("lenovo")
            assert verify_secret(data["new_secret"], device["secret_hash"]) is True

    @pytest.mark.asyncio
    async def test_health_no_auth_needed(self):
        """GET / (health check) does not require auth."""
        from httpx import AsyncClient, ASGITransport
        from hub.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            resp = await client.get("/")
            assert resp.status_code == 200
            data = resp.json()
            assert data["version"] == "0.3.0"


# ---------------------------------------------------------------------------
# Config module tests
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_command_ttl(self):
        from hub.config import COMMAND_TTL
        assert COMMAND_TTL == 300

    def test_default_tailscale_cidr(self):
        from hub.config import TAILSCALE_CIDR
        assert TAILSCALE_CIDR == "100.64.0.0/10"

    def test_default_allow_lan_false(self):
        from hub.config import ALLOW_LAN
        assert ALLOW_LAN is False

    def test_trusted_networks_include_localhost(self):
        import ipaddress
        from hub.config import TRUSTED_NETWORKS
        localhost = ipaddress.ip_address("127.0.0.1")
        assert any(localhost in net for net in TRUSTED_NETWORKS
                    if isinstance(net, (ipaddress.IPv4Network,)))

    def test_trusted_networks_include_tailscale(self):
        import ipaddress
        from hub.config import TRUSTED_NETWORKS
        ts_ip = ipaddress.ip_address("100.100.50.3")
        assert any(ts_ip in net for net in TRUSTED_NETWORKS
                    if isinstance(net, (ipaddress.IPv4Network,)))
