"""Coordinate authenticated workers; the coordinator never opens their file paths."""
import secrets
import time
from datetime import datetime, timezone
from pathlib import PurePath
from uuid import uuid4

from .executor_registry import ExecutorRegistry
from .privacy import PrivacyPolicyEngine, classify_task_privacy

_active_controls = {}


async def cancel_worker_transfer(transfer_id):
    job = _active_controls.get(transfer_id)
    if not job:
        raise ValueError("No active receiving worker is available to confirm cancellation")
    control, destination, session = job
    result = await control.execute_and_observe(destination, "transfer.cancel", {"transfer_id": transfer_id})
    if result.get("status") != "success" or not (result.get("data") or {}).get("cancelled"):
        raise ValueError("The receiving worker could not confirm cancellation; completion may already have occurred")
    from .database import save_transfer_session
    session.state = "CANCELLED"
    await save_transfer_session(session.to_dict())
    return session


async def copy_between_workers(control, requester, args, privacy_context=None):
    source, destination = args["source_device"], args["destination_device"]
    registry = ExecutorRegistry.get_instance()
    for device, capability in ((source, "transfer.inspect"), (source, "transfer.send"), (destination, "transfer.prepare")):
        handle = registry.get_device_handle(device)
        if not handle or not registry.is_device_online(device) or not handle.supports_capability(capability):
            return {"status": "failure", "message": "A required transfer worker is offline or unavailable."}
        permission = await control.check_permissions(requester, device, capability)
        if not permission or not permission.allowed:
            return {"status": "denied", "message": "Permission denied for the source or receiving device."}
    # Device locality is authoritative. Caller-provided labels cannot authorize cloud sharing.
    from .database import get_device_permission
    if (await get_device_permission(source, "fs.read") == "deny" or
            await get_device_permission(destination, "fs.write") == "deny"):
        return {"status": "denied", "message": "File access is explicitly denied on a participating device."}
    privacy = classify_task_privacy("fs.read", {"path": args["source_path"]}, privacy_context or {})
    evaluation = PrivacyPolicyEngine.get_instance().evaluate_candidate(
        registry.get_device_handle(destination), privacy, "fs.read")
    if not evaluation.is_allowed:
        return {"status": "denied", "message": evaluation.reason}
    inspected = await control.execute_and_observe(source, "transfer.inspect", {"path": args["source_path"]})
    if inspected.get("status") != "success":
        return inspected
    manifest = inspected.get("data") or {}
    transfer_id = "peer_" + uuid4().hex
    grant = {"transfer_id": transfer_id, "token": secrets.token_urlsafe(32),
             "size": manifest["size"], "sha256": manifest["sha256"],
             "destination": args["destination_path"], "expires_at": time.time() + 3600}
    prepared = await control.execute_and_observe(destination, "transfer.prepare", grant)
    if prepared.get("status") != "success":
        return prepared
    from .file_transfer import FileTransferManager, TransferSession, TransferState
    from .database import save_transfer_session
    manager = FileTransferManager.get_instance()
    session = TransferSession(transfer_id=transfer_id, source_device_id=source, destination_device_id=destination,
        source_path=args["source_path"], destination_path=args["destination_path"],
        filename=PurePath(args["destination_path"]).name, total_bytes=manifest["size"],
        source_checksum=manifest["sha256"], privacy_class=privacy.privacy_class,
        state=TransferState.TRANSFERRING, resumable=False,
        metadata={"worker_protocol": True, "progress_note": "Bytes are counted only after receiver verification"})
    manager._active_sessions[transfer_id] = session
    await save_transfer_session(session.to_dict())
    _active_controls[transfer_id] = (control, destination, session)
    endpoint = prepared.get("data") or {}
    try:
        result = await control.execute_and_observe(source, "transfer.send", {
            "path": args["source_path"], "transfer_id": transfer_id, "token": grant["token"],
            "host": endpoint["host"], "port": endpoint["port"], "fingerprint": endpoint["fingerprint"]}, timeout=110)
    except BaseException:
        session.state = TransferState.FAILED
        session.metadata["failure_reason"] = "Completion unverified after coordinator interruption"
        await save_transfer_session(session.to_dict())
        raise
    finally:
        _active_controls.pop(transfer_id, None)
    if session.state == TransferState.CANCELLED:
        return {"status": "cancelled", "message": "The receiving worker confirmed transfer cancellation."}
    if result.get("status") != "success":
        session.state = TransferState.FAILED
        session.metadata["failure_reason"] = "Completion unverified"
        await save_transfer_session(session.to_dict())
        return {"status": "failure", "message": "Transfer interrupted or unverified. The source was preserved.",
                "data": {"transfer_id": transfer_id}}
    evidence = result.get("data") or {}
    if (evidence.get("status") != "complete" or evidence.get("sha256") != manifest["sha256"]
            or evidence.get("offset") != manifest["size"]):
        session.state = TransferState.FAILED
        session.metadata["failure_reason"] = "Receiver checksum verification missing"
        await save_transfer_session(session.to_dict())
        return {"status": "failure", "message": "The receiving file could not be verified."}
    session.state = TransferState.COMPLETED
    session.completed_at = datetime.now(timezone.utc).isoformat()
    session.transferred_bytes = manifest["size"]
    session.destination_checksum = manifest["sha256"]
    await save_transfer_session(session.to_dict())
    return {"status": "success", "message": "Copied the file and verified its checksum on the receiving device.",
            "data": {"transfer_id": transfer_id, "bytes": manifest["size"], "sha256": manifest["sha256"]}}
