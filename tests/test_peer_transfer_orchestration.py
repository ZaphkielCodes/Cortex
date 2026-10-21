import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.peer_worker import PeerWorker
from hub.device_handle import DeviceHandle
from hub.executor_registry import ExecutorRegistry
from hub.peer_transfer import copy_between_workers


@pytest.mark.asyncio
async def test_coordinator_uses_workers_and_real_tls_without_reading_source(tmp_path, monkeypatch):
    import agent.peer_worker as worker_module
    from shared.peer_identity import load_identity
    monkeypatch.setenv("CORTEX_RECEIVE_ROOT", str(tmp_path / "receiver"))
    monkeypatch.setenv("CORTEX_TRANSFER_BIND", "127.0.0.1")
    monkeypatch.setenv("CORTEX_TRANSFER_ADVERTISE_HOST", "127.0.0.1")
    monkeypatch.setattr(worker_module, "load_identity", lambda _: load_identity(tmp_path / "identity"))
    workers = {"source": PeerWorker(), "phone": PeerWorker()}
    registry = ExecutorRegistry.get_instance()
    for device in workers:
        registry.register_device(DeviceHandle(device_id=device, display_name=device,
            role="trusted_worker", trust_level="trusted", capabilities=["transfer.inspect", "transfer.send", "transfer.prepare"]))
        registry.set_device_online(device, True)
    source = tmp_path / "source.bin"
    source.write_bytes(b"real worker bytes" * 30000)
    calls = []
    async def execute(device, operation, args, **kwargs):
        calls.append((device, operation))
        return await workers[device].execute(operation, args)
    control = SimpleNamespace(check_permissions=AsyncMock(return_value=SimpleNamespace(allowed=True)), execute_and_observe=execute)
    args = {"source_device": "source", "destination_device": "phone", "source_path": str(source), "destination_path": "copy.bin"}
    try:
        result = await copy_between_workers(control, "requester", args)
        assert result["status"] == "success"
        assert calls == [("source", "transfer.inspect"), ("phone", "transfer.prepare"), ("source", "transfer.send")]
        assert (tmp_path / "receiver/copy.bin").read_bytes() == source.read_bytes()
        assert source.exists()
        from hub.file_transfer import FileTransferManager
        session = await FileTransferManager.get_instance().get_session(result["data"]["transfer_id"])
        assert session.state == "COMPLETED" and session.transferred_bytes == source.stat().st_size
        assert session.destination_checksum == result["data"]["sha256"]
        assert "token" not in session.metadata
    finally:
        await asyncio.gather(*(worker.close() for worker in workers.values()))


@pytest.mark.asyncio
async def test_permission_and_high_privacy_denial_precede_file_access():
    registry = ExecutorRegistry.get_instance()
    for device in ["source", "phone"]:
        registry.register_device(DeviceHandle(device_id=device, display_name=device, locality="trusted_lan_device",
            trust_level="trusted", capabilities=["transfer.inspect", "transfer.send", "transfer.prepare"]))
        registry.set_device_online(device, True)
    control = SimpleNamespace(check_permissions=AsyncMock(return_value=SimpleNamespace(allowed=False)), execute_and_observe=AsyncMock())
    args = {"source_device": "source", "destination_device": "phone", "source_path": "private", "destination_path": "copy"}
    assert (await copy_between_workers(control, "requester", args))["status"] == "denied"
    control.check_permissions.return_value = SimpleNamespace(allowed=True)
    assert (await copy_between_workers(control, "requester", args, {"privacy_class": "HIGHLY_SENSITIVE"}))["status"] == "denied"
    control.execute_and_observe.assert_not_awaited()
