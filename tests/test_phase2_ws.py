"""
tests/test_phase2_ws.py
-----------------------
WebSocket integration tests for Phase 2 confirmation workflow.

These tests exercise the ACTUAL WebSocket endpoints, verifying:
  - Single admin session can approve its own Tier 2/3 commands
  - Denial is audit-logged (action_logs row with result="denied")
  - Expiration is audit-logged (action_logs row with result="expired")
  - Resolver identity is the authenticated admin device_id (not "cli")
  - Tier 1 commands execute immediately without confirmation

Run with:
    cd Cortex
    pytest tests/test_phase2_ws.py -v
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

# Add hub/ to path so we can import hub modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from hub.auth import generate_device_secret, hash_secret
import hub.database as database



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------




@pytest.fixture()
def admin_secret():
    """Generate an admin secret and enroll the admin device in the DB."""
    secret = generate_device_secret()
    return secret


@pytest.fixture()
def app_client(admin_secret):
    """Create a FastAPI TestClient with a short confirmation timeout.

    We also patch the database path inside main's transitive imports so
    all reads/writes hit the test DB, and shorten CONFIRMATION_TIMEOUT
    so expiry tests don't take 60 seconds.

    The PeerFilterMiddleware rejects Starlette's TestClient because its
    peer address is "testclient" (not a real IP).  We patch is_trusted_peer
    to always return True during tests.
    """
    import hub.config as config
    # Shorten confirmation timeout for tests
    original_timeout = config.CONFIRMATION_TIMEOUT
    config.CONFIRMATION_TIMEOUT = 3  # 3 seconds instead of 60

    # Patch is_trusted_peer so the PeerFilterMiddleware lets TestClient through
    original_is_trusted = config.is_trusted_peer
    config.is_trusted_peer = lambda ip: True

    # Import app AFTER patching config
    import importlib
    import hub.main as main_mod
    importlib.reload(main_mod)

    from starlette.testclient import TestClient
    with TestClient(main_mod.app) as client:
        yield client

    config.CONFIRMATION_TIMEOUT = original_timeout
    config.is_trusted_peer = original_is_trusted


@pytest_asyncio.fixture()
async def enrolled_admin(admin_secret):
    """Enroll the admin device in the DB and return (device_id, secret)."""
    hashed = hash_secret(admin_secret)
    await database.enroll_device("asus", "ASUS Laptop", "admin", hashed)
    return "asus", admin_secret


@pytest_asyncio.fixture()
async def enrolled_sub_admin():
    """Enroll a sub_admin device and return (device_id, secret)."""
    secret = generate_device_secret()
    hashed = hash_secret(secret)
    await database.enroll_device("lenovo", "Lenovo Laptop", "sub_admin", hashed)
    return "lenovo", secret


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _authenticate_cli(ws, device_id: str, secret: str) -> dict:
    """Perform the CLI auth handshake. Returns the auth_result payload."""
    # Receive challenge
    msg = json.loads(ws.receive_text())
    assert msg["type"] == "auth_challenge"

    # Send auth response
    ws.send_text(json.dumps({
        "type": "auth_response",
        "device_id": device_id,
        "response": secret,
    }))

    # Receive auth result
    result = json.loads(ws.receive_text())
    assert result["type"] == "auth_result"
    return result


def _authenticate_agent(ws, device_id: str, secret: str) -> dict:
    """Perform the agent auth handshake. Returns the auth_result payload."""
    # Receive challenge
    msg = json.loads(ws.receive_text())
    assert msg["type"] == "auth_challenge"

    # Send auth response (agent sends raw secret)
    ws.send_text(json.dumps({
        "type": "auth_response",
        "device_id": device_id,
        "response": secret,
        "platform": "test_platform",
        "capabilities": ["screenshot.read", "open_app", "fs.move", "fs.delete", "shell.run", "remote_shutdown", "remote_wipe"],
    }))

    # Receive auth result
    result = json.loads(ws.receive_text())
    assert result["type"] == "auth_result"
    return result


def _drain_device_list(ws) -> dict:
    """Read and return the initial device_list event sent after CLI auth."""
    for _ in range(10):
        msg = json.loads(ws.receive_text())
        if msg.get("event") == "device_list":
            return msg
    pytest.fail("Did not receive device_list event")


# ---------------------------------------------------------------------------
# Tests: CLI authentication
# ---------------------------------------------------------------------------

class TestCLIAuth:
    def test_cli_auth_success(self, app_client, enrolled_admin):
        """CLI authenticates successfully with valid credentials."""
        device_id, secret = enrolled_admin
        with app_client.websocket_connect("/ws-cli") as ws:
            result = _authenticate_cli(ws, device_id, secret)
            assert result["success"] is True
            assert result["role"] == "admin"

    def test_cli_auth_wrong_secret(self, app_client, enrolled_admin):
        """CLI is rejected with wrong secret."""
        device_id, _ = enrolled_admin
        with app_client.websocket_connect("/ws-cli") as ws:
            result = _authenticate_cli(ws, device_id, "wrong_secret_here")
            assert result["success"] is False

    def test_cli_auth_unknown_device(self, app_client, enrolled_admin):
        """CLI is rejected for an unregistered device."""
        _, secret = enrolled_admin
        with app_client.websocket_connect("/ws-cli") as ws:
            result = _authenticate_cli(ws, "nonexistent", secret)
            assert result["success"] is False


# ---------------------------------------------------------------------------
# Tests: Tier 1 immediate execution
# ---------------------------------------------------------------------------

class TestTier1Execution:
    def test_tier1_command_sent_immediately(self, app_client, enrolled_admin, enrolled_sub_admin):
        """Tier 1 command from admin is dispatched without confirmation."""
        admin_id, admin_secret = enrolled_admin
        agent_id, agent_secret = enrolled_sub_admin

        # Connect agent
        with app_client.websocket_connect(f"/ws/{agent_id}") as agent_ws:
            agent_result = _authenticate_agent(agent_ws, agent_id, agent_secret)
            assert agent_result["success"] is True

            # Connect CLI
            with app_client.websocket_connect("/ws-cli") as cli_ws:
                cli_result = _authenticate_cli(cli_ws, admin_id, admin_secret)
                assert cli_result["success"] is True
                _drain_device_list(cli_ws)

                # Send Tier 1 command
                cli_ws.send_text(json.dumps({
                    "device_id": agent_id,
                    "command": "screenshot.read",
                    "args": [],
                }))

                # CLI should get a "sent" event (no confirmation needed)
                msg = None
                for _ in range(10):
                    try:
                        m = json.loads(cli_ws.receive_text())
                        if m.get("event") == "error":
                            pytest.fail(f"Received error instead of sent: {m}")
                        if m.get("event") == "sent":
                            msg = m
                            break
                    except Exception as e:
                        pytest.fail(f"Error receiving text: {e}")
                
                assert msg is not None, "Never received sent event"
                assert msg["event"] == "sent"
                assert msg["command"] == "screenshot.read"
                assert msg["device_id"] == agent_id

                # Agent should receive the command
                agent_msg = json.loads(agent_ws.receive_text())
                assert agent_msg["type"] == "command"
                assert agent_msg["command"] == "screenshot.read"

    def test_tier1_to_offline_device_errors(self, app_client, enrolled_admin, enrolled_sub_admin):
        """Tier 1 command to an offline device returns an error."""
        admin_id, admin_secret = enrolled_admin
        agent_id, _ = enrolled_sub_admin

        # Manually set capability so permission check passes, but online check fails
        asyncio.get_event_loop().run_until_complete(
            database.update_device_capabilities(agent_id, "test_platform", ["screenshot.read"])
        )

        with app_client.websocket_connect("/ws-cli") as cli_ws:
            _authenticate_cli(cli_ws, admin_id, admin_secret)
            _drain_device_list(cli_ws)

            cli_ws.send_text(json.dumps({
                "device_id": agent_id,
                "command": "screenshot.read",
                "args": [],
            }))

            msg = json.loads(cli_ws.receive_text())
            assert msg["event"] == "error"
            assert "not connected" in msg["message"]


# ---------------------------------------------------------------------------
# Tests: Confirmation flow (self-approval)
# ---------------------------------------------------------------------------

class TestConfirmationSelfApproval:
    def test_admin_can_approve_own_request(self, app_client, enrolled_admin, enrolled_sub_admin):
        """A single admin CLI session can approve its own Tier 2 command.

        This was previously impossible because _start_confirmation_flow was
        awaited inline, blocking the CLI's message loop.
        """
        admin_id, admin_secret = enrolled_admin
        agent_id, agent_secret = enrolled_sub_admin

        # Connect agent
        with app_client.websocket_connect(f"/ws/{agent_id}") as agent_ws:
            _authenticate_agent(agent_ws, agent_id, agent_secret)

            # Connect CLI
            with app_client.websocket_connect("/ws-cli") as cli_ws:
                _authenticate_cli(cli_ws, admin_id, admin_secret)
                _drain_device_list(cli_ws)

                # Drain the "device_connected" event from the agent connecting
                # (may or may not be present depending on timing)
                cli_ws.send_text(json.dumps({
                    "device_id": agent_id,
                    "command": "fs.move",
                    "args": ["a.txt", "b.txt"],
                }))

                # Read messages until we get the confirmation request
                confirmation_req = None
                for _ in range(10):
                    msg = json.loads(cli_ws.receive_text())
                    if msg.get("event") == "confirmation_request":
                        confirmation_req = msg
                        break
                    # Skip other events like device_connected

                assert confirmation_req is not None, "Never received confirmation_request"
                assert confirmation_req["command"] == "fs.move"
                assert confirmation_req["tier"] == 2

                command_id = confirmation_req["command_id"]

                # The same CLI session approves it (this would have timed out before the fix)
                cli_ws.send_text(json.dumps({
                    "type": "confirmation_response",
                    "command_id": command_id,
                    "decision": "approve",
                }))

                # Should get confirmed_and_sent event
                confirmed_msg = None
                for _ in range(10):
                    msg = json.loads(cli_ws.receive_text())
                    if msg.get("event") == "confirmed_and_sent":
                        confirmed_msg = msg
                        break

                assert confirmed_msg is not None, "Never received confirmed_and_sent"
                assert confirmed_msg["command_id"] == command_id
                assert confirmed_msg["command"] == "fs.move"

                # Agent should receive the command
                agent_msg = json.loads(agent_ws.receive_text())
                assert agent_msg["type"] == "command"
                assert agent_msg["command"] == "fs.move"
                assert agent_msg["args"] == ["a.txt", "b.txt"]


# ---------------------------------------------------------------------------
# Tests: Confirmation denial + audit logging
# ---------------------------------------------------------------------------

class TestConfirmationDenial:
    @pytest.mark.asyncio
    async def test_denial_is_audit_logged(self, app_client, enrolled_admin, enrolled_sub_admin):
        """Denying a confirmation writes a 'denied' row to action_logs."""
        admin_id, admin_secret = enrolled_admin
        agent_id, agent_secret = enrolled_sub_admin

        with app_client.websocket_connect(f"/ws/{agent_id}") as agent_ws:
            _authenticate_agent(agent_ws, agent_id, agent_secret)

            with app_client.websocket_connect("/ws-cli") as cli_ws:
                _authenticate_cli(cli_ws, admin_id, admin_secret)
                _drain_device_list(cli_ws)

                cli_ws.send_text(json.dumps({
                    "device_id": agent_id,
                    "command": "fs.delete",
                    "args": ["/tmp/dangerous"],
                }))

                # Find the confirmation request
                confirmation_req = None
                for _ in range(10):
                    msg = json.loads(cli_ws.receive_text())
                    if msg.get("event") == "confirmation_request":
                        confirmation_req = msg
                        break

                assert confirmation_req is not None
                command_id = confirmation_req["command_id"]

                # Deny it
                cli_ws.send_text(json.dumps({
                    "type": "confirmation_response",
                    "command_id": command_id,
                    "decision": "deny",
                }))

                # Wait for denial confirmation event
                denied_msg = None
                for _ in range(10):
                    msg = json.loads(cli_ws.receive_text())
                    if msg.get("event") == "confirmation_denied":
                        denied_msg = msg
                        break

                assert denied_msg is not None
                assert denied_msg["command_id"] == command_id

                # Check the audit log
                logs = await database.get_recent_logs(10)
                denied_logs = [l for l in logs if l["result"] == "denied" and l["command_id"] == command_id]
                assert len(denied_logs) == 1
                assert denied_logs[0]["command"] == "fs.delete"

    @pytest.mark.asyncio
    async def test_resolver_identity_is_admin_device_id(self, app_client, enrolled_admin, enrolled_sub_admin):
        """The resolver in pending_confirmations is the admin's device_id, not 'cli'."""
        admin_id, admin_secret = enrolled_admin
        agent_id, agent_secret = enrolled_sub_admin

        with app_client.websocket_connect(f"/ws/{agent_id}") as agent_ws:
            _authenticate_agent(agent_ws, agent_id, agent_secret)

            with app_client.websocket_connect("/ws-cli") as cli_ws:
                _authenticate_cli(cli_ws, admin_id, admin_secret)
                _drain_device_list(cli_ws)

                cli_ws.send_text(json.dumps({
                    "device_id": agent_id,
                    "command": "shell.run",
                    "args": ["echo hello"],
                }))

                confirmation_req = None
                for _ in range(10):
                    msg = json.loads(cli_ws.receive_text())
                    if msg.get("event") == "confirmation_request":
                        confirmation_req = msg
                        break

                assert confirmation_req is not None
                command_id = confirmation_req["command_id"]

                # Deny it
                cli_ws.send_text(json.dumps({
                    "type": "confirmation_response",
                    "command_id": command_id,
                    "decision": "deny",
                }))

                # Wait for denial event
                for _ in range(10):
                    msg = json.loads(cli_ws.receive_text())
                    if msg.get("event") == "confirmation_denied":
                        break

                # Verify the resolver in the DB is the admin device_id
                conf = await database.get_confirmation(command_id)
                assert conf is not None
                assert conf["resolved_by"] == admin_id  # not "cli"
                assert conf["status"] == "deny"


# ---------------------------------------------------------------------------
# Tests: Confirmation expiration + audit logging
# ---------------------------------------------------------------------------

class TestConfirmationExpiration:
    @pytest.mark.asyncio
    async def test_expiration_is_audit_logged(self, app_client, enrolled_admin, enrolled_sub_admin):
        """An expired confirmation writes an 'expired' row to action_logs.

        Uses a 3-second CONFIRMATION_TIMEOUT (set in the app_client fixture).
        """
        admin_id, admin_secret = enrolled_admin
        agent_id, agent_secret = enrolled_sub_admin

        with app_client.websocket_connect(f"/ws/{agent_id}") as agent_ws:
            _authenticate_agent(agent_ws, agent_id, agent_secret)

            with app_client.websocket_connect("/ws-cli") as cli_ws:
                _authenticate_cli(cli_ws, admin_id, admin_secret)
                _drain_device_list(cli_ws)

                cli_ws.send_text(json.dumps({
                    "device_id": agent_id,
                    "command": "remote_shutdown",
                    "args": [],
                }))

                # Find the confirmation request
                confirmation_req = None
                for _ in range(10):
                    msg = json.loads(cli_ws.receive_text())
                    if msg.get("event") == "confirmation_request":
                        confirmation_req = msg
                        break

                assert confirmation_req is not None
                command_id = confirmation_req["command_id"]

                # DON'T approve or deny — let it expire (3s timeout)
                expired_msg = None
                for _ in range(20):
                    try:
                        msg = json.loads(cli_ws.receive_text())
                        if msg.get("event") == "confirmation_expired":
                            expired_msg = msg
                            break
                    except Exception:
                        break

                assert expired_msg is not None, "Never received confirmation_expired"
                assert expired_msg["command_id"] == command_id

                # Check the audit log
                logs = await database.get_recent_logs(10)
                expired_logs = [l for l in logs if l["result"] == "expired" and l["command_id"] == command_id]
                assert len(expired_logs) == 1
                assert expired_logs[0]["command"] == "remote_shutdown"

                # Check the confirmation record
                conf = await database.get_confirmation(command_id)
                assert conf["status"] == "expired"
                assert conf["resolved_by"] == "system"


# ---------------------------------------------------------------------------
# Tests: Permission enforcement over WebSocket
# ---------------------------------------------------------------------------

class TestWSPermissions:
    def test_sub_admin_cannot_target_other_device(self, app_client, enrolled_admin, enrolled_sub_admin):
        """Sub-admin CLI is denied when targeting another device."""
        _, admin_secret = enrolled_admin
        agent_id, agent_secret = enrolled_sub_admin

        with app_client.websocket_connect("/ws-cli") as cli_ws:
            result = _authenticate_cli(cli_ws, agent_id, agent_secret)
            assert result["success"] is True
            assert result["role"] == "sub_admin"
            _drain_device_list(cli_ws)

            # Try to target the admin's device
            cli_ws.send_text(json.dumps({
                "device_id": "asus",
                "command": "screenshot.read",
                "args": [],
            }))

            msg = json.loads(cli_ws.receive_text())
            assert msg["event"] == "error"
            assert "Permission denied" in msg["message"]

    def test_sub_admin_cannot_approve_confirmations(self, app_client, enrolled_admin, enrolled_sub_admin):
        """Sub-admin CLI is denied when trying to approve a confirmation."""
        _, admin_secret = enrolled_admin
        agent_id, agent_secret = enrolled_sub_admin

        with app_client.websocket_connect("/ws-cli") as cli_ws:
            _authenticate_cli(cli_ws, agent_id, agent_secret)
            _drain_device_list(cli_ws)

            cli_ws.send_text(json.dumps({
                "type": "confirmation_response",
                "command_id": "some_id",
                "decision": "approve",
            }))

            msg = json.loads(cli_ws.receive_text())
            assert msg["event"] == "error"
            assert "Only admin" in msg["message"]
