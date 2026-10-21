import pytest
import httpx
from hub.main import app
from hub.database import enroll_device
from hub.auth import hash_secret


@pytest.mark.asyncio
async def test_global_controls_require_credentials_and_admin_role():
    await enroll_device("worker", "Worker", "trusted_worker", hash_secret("test-worker-secret"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/control/state")).status_code == 401
        assert (await client.post("/api/control/autonomy", json={"enabled": False})).status_code == 401
        headers = {"X-Cortex-Device": "worker", "X-Cortex-Secret": "test-worker-secret"}
        assert (await client.get("/api/control/state", headers=headers)).status_code == 403
        assert (await client.post("/api/control/autonomy", json={"enabled": False}, headers=headers)).status_code == 403
        assert (await client.post("/api/control/message", json={"text": "hello", "device_id": "asus"}, headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_actual_enrolled_admin_can_read_control_state():
    await enroll_device("reviewer", "Reviewer", "admin", hash_secret("test-admin-secret"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/control/state", headers={"X-Cortex-Device": "reviewer", "X-Cortex-Secret": "test-admin-secret"})
        assert response.status_code == 200


def test_unauthenticated_control_websocket_is_rejected():
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws-control"):
                raise AssertionError("Unauthenticated control socket was accepted")
