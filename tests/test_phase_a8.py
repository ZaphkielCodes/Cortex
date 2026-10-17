"""
tests/test_phase_a8.py
----------------------
Phase A8 Test Suite: Cross-Device Transport & Resumable File Transfer.

Covers:
- Test 1 — Small LAN file transfer
- Test 2 — Multi-chunk LAN transfer
- Test 3 — Integrity checksum success
- Test 4 — Corrupted transfer rejected
- Test 5 — Disconnect -> resume (resumes from offset, not 0)
- Test 6 — Cancel transfer (cleans up partial files)
- Test 7 — Destination collision blocked (no silent overwrite)
- Test 8 — Offline destination -> WAITING_FOR_DEVICE
- Test 9 — Privacy denies transfer (unauthorized/restricted device)
- Test 10 — Sensitive ASUS -> trusted Lenovo allowed
- Test 11 — Cloud transport rejected for HIGHLY_SENSITIVE data
- Test 12 — A6 task resumes after required file arrives
- Test 13 — File locality / replica metadata updated correctly (copy vs move)
- Test 14 — Old / stale transfer ticket rejected
- Test 15 — Production DB isolation check
- Self-Knowledge queries for file transfer
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import pytest
import pytest_asyncio

from hub.device_handle import DeviceHandle
from hub.executor_registry import ExecutorRegistry
from hub.privacy import (
    PrivacyPolicyEngine,
    PrivacyClass,
    ExecutorLocality,
    ExecutionEnvironment,
    PrivacyDecision,
)
from hub.file_transfer import (
    FileTransferManager,
    TransferSession,
    FileLocalityRecord,
    TransferTicket,
    TransferState,
    LANTransportProvider,
    TransportSelector,
)
from hub.task_ownership import (
    TaskOwnershipManager,
    TaskExecution,
    ExecutionState,
)
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness
from hub.database import (
    save_transfer_session,
    get_transfer_session,
    list_transfer_sessions,
    get_file_locality,
    list_file_localities,
)


# Helper device handle factories
def make_asus() -> DeviceHandle:
    return DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        trust_level="admin",
        locality=ExecutorLocality.LOCAL_DEVICE,
        execution_environment=ExecutionEnvironment.ON_PREMISE,
        private_compute_allowed=True,
        cloud_processing_allowed=False,
        capabilities=["web.search", "fs.list", "fs.read", "email.send", "ui.notify", "app.launch"],
        transports={"lan": True, "websocket": True, "ble": True, "wifi_direct": False, "cloud_relay": False},
        online=True,
    )


def make_lenovo() -> DeviceHandle:
    return DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="secondary_worker",
        trust_level="trusted",
        locality=ExecutorLocality.TRUSTED_LAN_DEVICE,
        execution_environment=ExecutionEnvironment.ON_PREMISE,
        private_compute_allowed=True,
        cloud_processing_allowed=False,
        capabilities=["web.search", "fs.list", "fs.read", "email.send", "ui.notify", "app.launch"],
        transports={"lan": True, "websocket": True, "ble": True, "wifi_direct": False, "cloud_relay": False},
        online=True,
    )


def make_restricted_phone() -> DeviceHandle:
    return DeviceHandle(
        device_id="guest_phone",
        display_name="Guest Phone",
        role="guest",
        trust_level="restricted",
        locality=ExecutorLocality.TRUSTED_LAN_DEVICE,
        execution_environment=ExecutionEnvironment.UNTRUSTED_RUNTIME,
        private_compute_allowed=False,
        cloud_processing_allowed=False,
        capabilities=["web.search", "fs.read"],
        transports={"lan": True, "websocket": True, "ble": True},
        online=True,
    )


def make_cloud_worker() -> DeviceHandle:
    return DeviceHandle(
        device_id="cloud_relay_node",
        display_name="Cortex Cloud Node",
        role="cloud_worker",
        trust_level="trusted",
        device_type="cloud_worker",
        locality=ExecutorLocality.CLOUD_WORKER,
        execution_environment=ExecutionEnvironment.CLOUD_HOSTED,
        private_compute_allowed=False,
        cloud_processing_allowed=True,
        capabilities=["web.search", "fs.read"],
        transports={"cloud_relay": True, "websocket": True},
        online=True,
    )


# ===========================================================================
# Test 1 — Small LAN file transfer
# ===========================================================================
@pytest.mark.asyncio
async def test_1_small_lan_file_transfer(tmp_path):
    """Test single-chunk small file transfer ASUS -> Lenovo over LAN."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    src_file = tmp_path / "small_note.txt"
    dst_file = tmp_path / "lenovo_dest" / "small_note.txt"
    content = b"Hello from ASUS to Lenovo via Cortex A8 LAN transfer!"
    src_file.write_bytes(content)

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_file),
        destination_path=str(dst_file),
    )

    assert session.state == TransferState.PENDING
    assert session.transport == "lan"
    assert session.total_bytes == len(content)

    res = await ftm.execute_transfer(session.transfer_id)

    assert res.state == TransferState.COMPLETED
    assert res.transferred_bytes == len(content)
    assert dst_file.exists()
    assert dst_file.read_bytes() == content


# ===========================================================================
# Test 2 — Multi-chunk LAN transfer
# ===========================================================================
@pytest.mark.asyncio
async def test_2_multichunk_lan_transfer(tmp_path):
    """Test multi-chunk transfer with bounded chunk size and progress tracking."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    # 150 KB payload with 32 KB chunk size -> 5 chunks
    payload = b"CHUNK_DATA_ABCDEFGH_" * 7500
    src_file = tmp_path / "large_data.bin"
    dst_file = tmp_path / "lenovo_dest" / "large_data.bin"
    src_file.write_bytes(payload)

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_file),
        destination_path=str(dst_file),
        chunk_size=32768,
    )

    assert session.chunk_size == 32768
    assert session.total_bytes == len(payload)

    res = await ftm.execute_transfer(session.transfer_id)

    assert res.state == TransferState.COMPLETED
    assert res.transferred_bytes == len(payload)
    assert res.progress_pct == 100.0
    assert dst_file.exists()
    assert dst_file.read_bytes() == payload


# ===========================================================================
# Test 3 — Integrity checksum success
# ===========================================================================
@pytest.mark.asyncio
async def test_3_integrity_checksum_success(tmp_path):
    """Verify source SHA-256 is validated against destination write before marking complete."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    content = b"Verification payload for Cortex Phase A8 cryptographic integrity test."
    expected_hash = hashlib.sha256(content).hexdigest()

    src_file = tmp_path / "verified_doc.pdf"
    dst_file = tmp_path / "lenovo_dest" / "verified_doc.pdf"
    src_file.write_bytes(content)

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_file),
        destination_path=str(dst_file),
    )

    assert session.source_checksum == expected_hash

    res = await ftm.execute_transfer(session.transfer_id)

    assert res.state == TransferState.COMPLETED
    assert res.destination_checksum == expected_hash
    assert dst_file.exists()


# ===========================================================================
# Test 4 — Corrupted transfer rejected
# ===========================================================================
@pytest.mark.asyncio
async def test_4_corrupted_transfer_rejected(tmp_path):
    """Verify corrupted file transfer is detected, rejected, and .cortex_part cleaned up."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    content = b"Original uncorrupted content"
    src_file = tmp_path / "original.txt"
    dst_file = tmp_path / "lenovo_dest" / "corrupted_target.txt"
    src_file.write_bytes(content)

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_file),
        destination_path=str(dst_file),
    )

    # Intentionally pass corrupted payload bytes to execute_transfer
    corrupted_content = b"Corrupted content inserted mid-stream!"
    res = await ftm.execute_transfer(session.transfer_id, data_payload=corrupted_content)

    assert res.state == TransferState.FAILED
    assert "verification failed" in res.metadata.get("error", "").lower()
    # Destination file must NOT exist
    assert not dst_file.exists()


# ===========================================================================
# Test 5 — Disconnect -> resume (resumes from offset, not 0)
# ===========================================================================
@pytest.mark.asyncio
async def test_5_disconnect_resume(tmp_path):
    """Simulate connection drop mid-transfer, then resume from byte offset without restarting from zero."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    payload = b"PART_DATA_TEST_" * 8000  # 120 KB
    src_file = tmp_path / "resume_test.dat"
    dst_file = tmp_path / "lenovo_dest" / "resume_test.dat"
    src_file.write_bytes(payload)

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_file),
        destination_path=str(dst_file),
        chunk_size=32768,
    )

    # Inject simulated network loss into LAN provider
    lan_provider: LANTransportProvider = ftm.transport_selector.providers["lan"]
    lan_provider.simulated_network_loss = True

    # Execution should fail/pause due to simulated drop
    with pytest.raises(ConnectionResetError):
        await ftm.execute_transfer(session.transfer_id)

    paused_sess = await ftm.get_session(session.transfer_id)
    assert paused_sess.state == TransferState.PAUSED
    assert paused_sess.transferred_bytes == 0  # failed on first chunk or partial

    # Reset simulated drop, manually write first chunk to part file to test offset resume
    lan_provider.simulated_network_loss = False
    part_file = dst_file.with_suffix(dst_file.suffix + ".cortex_part")
    part_file.parent.mkdir(parents=True, exist_ok=True)
    part_file.write_bytes(payload[:65536])  # 64 KB already transferred
    paused_sess.transferred_bytes = 65536

    # Resume transfer
    resumed = await ftm.resume_transfer(session.transfer_id)

    assert resumed.state == TransferState.COMPLETED
    assert resumed.transferred_bytes == len(payload)
    assert dst_file.exists()
    assert dst_file.read_bytes() == payload


# ===========================================================================
# Test 6 — Cancel transfer
# ===========================================================================
@pytest.mark.asyncio
async def test_6_cancel_transfer(tmp_path):
    """Verify cancellation marks state CANCELLED and cleans up .cortex_part."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    src_file = tmp_path / "cancel_me.txt"
    dst_file = tmp_path / "lenovo_dest" / "cancel_me.txt"
    src_file.write_bytes(b"Data before cancel")

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_file),
        destination_path=str(dst_file),
    )

    part_file = dst_file.with_suffix(dst_file.suffix + ".cortex_part")
    part_file.parent.mkdir(parents=True, exist_ok=True)
    part_file.write_bytes(b"Partial bytes...")

    cancelled = await ftm.cancel_transfer(session.transfer_id, reason="User clicked cancel")

    assert cancelled.state == TransferState.CANCELLED
    assert not part_file.exists()
    assert not dst_file.exists()


# ===========================================================================
# Test 7 — Destination collision blocked
# ===========================================================================
@pytest.mark.asyncio
async def test_7_destination_collision_blocked(tmp_path):
    """Verify that an existing destination file is not silently overwritten."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    src_file = tmp_path / "new_report.pdf"
    dst_file = tmp_path / "existing_report.pdf"
    src_file.write_bytes(b"New report content")
    dst_file.write_bytes(b"CRITICAL ORIGINAL CONTENT DO NOT OVERWRITE")

    # Attempting to transfer without allow_overwrite must raise FileExistsError
    with pytest.raises(FileExistsError) as exc_info:
        await ftm.initiate_transfer(
            source_device_id="asus",
            destination_device_id="lenovo",
            source_path=str(src_file),
            destination_path=str(dst_file),
            allow_overwrite=False,
        )

    assert "silent overwrite blocked" in str(exc_info.value).lower()
    # Original file must remain intact
    assert dst_file.read_bytes() == b"CRITICAL ORIGINAL CONTENT DO NOT OVERWRITE"


# ===========================================================================
# Test 8 — Offline destination -> WAITING_FOR_DEVICE
# ===========================================================================
@pytest.mark.asyncio
async def test_8_offline_destination_waiting_for_device(tmp_path):
    """Verify that initiating a transfer to an offline destination enters WAITING_FOR_DEVICE."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", False)  # Lenovo offline

    src_file = tmp_path / "offline_dest_test.txt"
    dst_file = tmp_path / "lenovo_dest" / "offline_dest_test.txt"
    src_file.write_bytes(b"Waiting data")

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_file),
        destination_path=str(dst_file),
    )

    assert session.state == TransferState.WAITING_FOR_DEVICE
    assert "lenovo' is offline" in session.metadata.get("waiting_reason", "").lower()


# ===========================================================================
# Test 9 — Privacy denies transfer
# ===========================================================================
@pytest.mark.asyncio
async def test_9_privacy_denies_transfer(tmp_path):
    """Verify that sending a sensitive/personal file to a restricted device is blocked by privacy policy."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    guest = make_restricted_phone()
    registry.register_device(asus)
    registry.register_device(guest)
    registry.set_device_online("asus", True)
    registry.set_device_online("guest_phone", True)

    src_file = tmp_path / "passwords.txt"
    dst_file = tmp_path / "guest_dest" / "passwords.txt"
    src_file.write_bytes(b"bank_login=secret")

    with pytest.raises(PermissionError) as exc_info:
        await ftm.initiate_transfer(
            source_device_id="asus",
            destination_device_id="guest_phone",
            source_path=str(src_file),
            destination_path=str(dst_file),
            privacy_class=PrivacyClass.SENSITIVE,
        )

    assert "privacy policy denied" in str(exc_info.value).lower()


# ===========================================================================
# Test 10 — Sensitive ASUS -> trusted Lenovo allowed
# ===========================================================================
@pytest.mark.asyncio
async def test_10_sensitive_asus_to_trusted_lenovo_allowed(tmp_path):
    """Verify sensitive file transfer ASUS -> trusted Lenovo private compute is permitted."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    src_file = tmp_path / "financial_q3.pdf"
    dst_file = tmp_path / "lenovo_dest" / "financial_q3.pdf"
    content = b"Financial Q3 Report: Confidential"
    src_file.write_bytes(content)

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_file),
        destination_path=str(dst_file),
        privacy_class=PrivacyClass.SENSITIVE,
    )

    assert session.state == TransferState.PENDING
    assert session.privacy_class == PrivacyClass.SENSITIVE

    res = await ftm.execute_transfer(session.transfer_id)
    assert res.state == TransferState.COMPLETED
    assert dst_file.exists()


# ===========================================================================
# Test 11 — Cloud transport rejected for HIGHLY_SENSITIVE data
# ===========================================================================
@pytest.mark.asyncio
async def test_11_cloud_transport_rejected_for_highly_sensitive_data(tmp_path):
    """Verify that cloud relay transport is strictly rejected for HIGHLY_SENSITIVE data."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    cloud = make_cloud_worker()
    registry.register_device(asus)
    registry.register_device(cloud)
    registry.set_device_online("asus", True)
    registry.set_device_online("cloud_relay_node", True)

    src_file = tmp_path / "medical_records.pdf"
    dst_file = tmp_path / "cloud_dest" / "medical_records.pdf"
    src_file.write_bytes(b"Medical diagnostic data")

    with pytest.raises(Exception) as exc_info:
        await ftm.initiate_transfer(
            source_device_id="asus",
            destination_device_id="cloud_relay_node",
            source_path=str(src_file),
            destination_path=str(dst_file),
            privacy_class=PrivacyClass.HIGHLY_SENSITIVE,
        )

    err = str(exc_info.value).lower()
    assert "privacy" in err or "forbidden" in err or "cloud" in err


# ===========================================================================
# Test 12 — A6 task resumes after required file arrives
# ===========================================================================
@pytest.mark.asyncio
async def test_12_a6_task_resumes_after_required_file_arrives(tmp_path):
    """
    Test 12: A6 task requires ASUS-local report.pdf.
    ASUS is offline. Task cannot move to Lenovo -> WAITING_FOR_EXECUTOR.
    File transfer moves report.pdf to Lenovo -> updates locality -> handoff completes and resumes on Lenovo!
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    # 1. Create task pinned to ASUS file report.pdf
    report_src = str(tmp_path / "report.pdf")
    report_dst = str(tmp_path / "lenovo_dest" / "report.pdf")
    Path(report_src).write_bytes(b"Report PDF content")

    ex = await tom.create_execution(
        goal_id="goal_a6_file_resume_1",
        step_id="step_1",
        owner_device_id="cortex",
        executor_device_id="asus",
        required_capabilities=["email.send"],
        required_resources=[report_src],
        required_data_locations={report_src: "asus"},
        movable=True,
    )

    # 2. ASUS goes offline: Lenovo does not have the file -> task enters WAITING_FOR_EXECUTOR
    registry.set_device_online("asus", False)
    failover_res = await tom.failover_if_needed(ex.task_execution_id)
    assert failover_res["status"] == "waiting_for_executor"

    # 3. Bring ASUS back up online to transfer file to Lenovo
    registry.set_device_online("asus", True)

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=report_src,
        destination_path=report_dst,
    )
    await ftm.execute_transfer(session.transfer_id)

    # 4. Verify file now has replica on Lenovo
    assert ftm.has_file_replica(report_src, "lenovo")

    # 5. Task was automatically unpinned and handed off to Lenovo upon file arrival!
    ex_resumed = await tom.get_execution(ex.task_execution_id)
    assert ex_resumed.executor_device_id == "lenovo"
    assert ex_resumed.execution_state == ExecutionState.ACTIVE
    assert ex_resumed.generation == 2


# ===========================================================================
# Test 13 — File locality / replica metadata updated correctly
# ===========================================================================
@pytest.mark.asyncio
async def test_13_file_locality_and_replica_metadata(tmp_path):
    """
    Test 13:
    - Copy creates an accessible replica location without moving canonical owner.
    - Move updates canonical ownership to destination and removes source location.
    """
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    src_copy = tmp_path / "copy_doc.txt"
    dst_copy = tmp_path / "lenovo_dest" / "copy_doc.txt"
    src_copy.write_bytes(b"Copy me")

    # 1. Copy
    sess_copy = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_copy),
        destination_path=str(dst_copy),
        is_move=False,
    )
    await ftm.execute_transfer(sess_copy.transfer_id)

    loc_copy = await ftm.get_file_locality_by_path_record("asus", str(src_copy))
    assert loc_copy is not None
    assert loc_copy.canonical_device_id == "asus"
    assert loc_copy.has_location("asus")
    assert loc_copy.has_location("lenovo")

    # 2. Move
    src_move = tmp_path / "move_doc.txt"
    dst_move = tmp_path / "lenovo_dest" / "move_doc.txt"
    src_move.write_bytes(b"Move me")

    sess_move = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_move),
        destination_path=str(dst_move),
        is_move=True,
    )
    await ftm.execute_transfer(sess_move.transfer_id)

    loc_move = await ftm.get_file_locality_by_path_record("lenovo", str(dst_move))
    assert loc_move is not None
    assert loc_move.canonical_device_id == "lenovo"
    assert loc_move.has_location("lenovo")
    assert not loc_move.has_location("asus")


# ===========================================================================
# Test 14 — Old / stale transfer ticket rejected
# ===========================================================================
@pytest.mark.asyncio
async def test_14_old_stale_transfer_ticket_rejected():
    """Verify that an expired or used transfer ticket is strictly rejected."""
    ftm = FileTransferManager.get_instance()

    # 1. Test expired ticket
    ticket = await ftm.issue_ticket(
        transfer_id="xfer_test_stale_1",
        source_device_id="asus",
        destination_device_id="lenovo",
        ttl_seconds=-10.0,  # Already expired in the past
    )

    valid = await ftm.verify_ticket(
        ticket_id=ticket.ticket_id,
        raw_token=ticket.raw_token,
        transfer_id="xfer_test_stale_1",
        source_device_id="asus",
        destination_device_id="lenovo",
    )
    assert not valid

    # 2. Test mismatched device binding
    ticket_valid = await ftm.issue_ticket(
        transfer_id="xfer_test_stale_2",
        source_device_id="asus",
        destination_device_id="lenovo",
        ttl_seconds=300.0,
    )

    valid_mismatch = await ftm.verify_ticket(
        ticket_id=ticket_valid.ticket_id,
        raw_token=ticket_valid.raw_token,
        transfer_id="xfer_test_stale_2",
        source_device_id="asus",
        destination_device_id="attacker_node",
    )
    assert not valid_mismatch


# ===========================================================================
# Test 15 — Production DB Isolation Check
# ===========================================================================
def test_15_production_db_isolation():
    """Verify tests run strictly against test_cortex.db and production DB is untouched."""
    db_path = os.environ.get("CORTEX_DB_PATH", "")
    assert "test_cortex.db" in db_path
    assert not db_path.endswith("data/cortex.db")

    prod_db = Path("data/cortex.db").resolve()
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    assert resolved_test_db != prod_db, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")


# ===========================================================================
# Self-Knowledge Queries for File Transfer
# ===========================================================================
@pytest.mark.asyncio
async def test_16_file_transfer_self_knowledge_queries(tmp_path):
    """Test truthful conversational self-knowledge answers for file transfers."""
    registry = ExecutorRegistry.get_instance()
    ftm = FileTransferManager.get_instance()
    ctrl = CortexControlLayer()
    harness = AgentHarness(control_layer=ctrl)

    asus = make_asus()
    lenovo = make_lenovo()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    src_file = tmp_path / "contracts.docx"
    dst_file = tmp_path / "lenovo_dest" / "contracts.docx"
    src_file.write_bytes(b"Important contract document data")

    session = await ftm.initiate_transfer(
        source_device_id="asus",
        destination_device_id="lenovo",
        source_path=str(src_file),
        destination_path=str(dst_file),
    )
    session.transferred_bytes = 10
    session.state = TransferState.TRANSFERRING
    await save_transfer_session(session.to_dict())

    # 1. "Where is my file?"
    r1 = await harness.process_request("asus", "Where is my file?", context={})
    assert r1.status == "success"
    assert "contracts.docx" in r1.message.lower() or "transferring" in r1.message.lower()

    # 2. "Is it still transferring?"
    r2 = await harness.process_request("asus", "Is it still transferring?", context={})
    assert r2.status == "success"
    assert "yes" in r2.message.lower()
    assert "transferring" in r2.message.lower()

    # 3. "How much is left?"
    r3 = await harness.process_request("asus", "How much is left?", context={})
    assert r3.status == "success"
    assert "remaining" in r3.message.lower() or "bytes" in r3.message.lower()

    # 4. "Which transport are you using?"
    r4 = await harness.process_request("asus", "Which transport are you using?", context={})
    assert r4.status == "success"
    assert "lan" in r4.message.lower()

    # 5. "Can this resume?"
    r5 = await harness.process_request("asus", "Can this resume?", context={})
    assert r5.status == "success"
    assert "yes" in r5.message.lower()
    assert "resumable" in r5.message.lower()

    # Complete the transfer
    await ftm.execute_transfer(session.transfer_id)

    # 6. "Did Lenovo receive it?"
    r6 = await harness.process_request("asus", "Did Lenovo receive it?", context={})
    assert r6.status == "success"
    assert "yes" in r6.message.lower()
    assert "lenovo" in r6.message.lower()
