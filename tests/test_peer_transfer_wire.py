import hashlib
import secrets
import time

import pytest

from shared.peer_identity import load_identity
from shared.peer_transfer import MAX_CHUNK, PeerClient, PeerReceiver


@pytest.mark.asyncio
async def test_tls_wire_multichunk_resume_integrity_and_no_overwrite(tmp_path):
    data = secrets.token_bytes(MAX_CHUNK * 2 + 17)
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    root = tmp_path / "received"
    receiver = PeerReceiver(root)
    context, pin = load_identity(tmp_path / "identity")
    token = secrets.token_urlsafe(32)
    checksum = hashlib.sha256(data).hexdigest()
    receiver.authorize("wire", token, "nested/target.bin", len(data), checksum, time.time() + 60)
    port = await receiver.start(context)
    client = PeerClient("127.0.0.1", port, pin, "wire", token)
    try:
        first = data[:MAX_CHUNK]
        await client.request("chunk", first, offset=0, length=len(first), sha256=hashlib.sha256(first).hexdigest())
        assert not (root / "nested/target.bin").exists()
        assert (await client.request("status"))["offset"] == MAX_CHUNK
        offsets = []
        async def progress(offset, total):
            offsets.append(offset)
        result = await client.send_file(source, progress)
        assert offsets == [MAX_CHUNK * 2, len(data)]
        assert result["sha256"] == checksum
        assert (root / "nested/target.bin").read_bytes() == data
        with pytest.raises(PermissionError):
            await client.request("chunk", b"bad", offset=0, length=3, sha256=hashlib.sha256(b"bad").hexdigest())
        assert (root / "nested/target.bin").read_bytes() == data
    finally:
        await receiver.close()


@pytest.mark.asyncio
async def test_tls_pin_ticket_offset_checksum_expiry_and_cancel(tmp_path):
    receiver = PeerReceiver(tmp_path / "received")
    context, pin = load_identity(tmp_path / "identity")
    token = secrets.token_urlsafe(32)
    receiver.authorize("x", token, "target", 3, hashlib.sha256(b"abc").hexdigest(), time.time() + 60)
    port = await receiver.start(context)
    client = PeerClient("127.0.0.1", port, pin, "x", token)
    try:
        for bad in [PeerClient("127.0.0.1", port, "0" * 64, "x", token),
                    PeerClient("127.0.0.1", port, pin, "x", "wrong")]:
            with pytest.raises(PermissionError):
                await bad.request("status")
        with pytest.raises(PermissionError):
            await client.request("chunk", b"abc", offset=1, length=3, sha256=hashlib.sha256(b"abc").hexdigest())
        with pytest.raises(PermissionError):
            await client.request("chunk", b"abc", offset=0, length=3, sha256="0" * 64)
        assert (await client.request("status"))["offset"] == 0
        await client.request("chunk", b"abc", offset=0, length=3, sha256=hashlib.sha256(b"abc").hexdigest())
        assert (await client.request("cancel"))["status"] == "cancelled"
        assert not list(receiver.root.glob("*.part"))
        with pytest.raises(PermissionError):
            await client.request("status")
    finally:
        await receiver.close()


def test_grants_cannot_escape_root_or_overwrite(tmp_path):
    receiver = PeerReceiver(tmp_path)
    (tmp_path / "exists").write_text("preserve")
    for destination in ["../escape", "exists", str(tmp_path.parent / "outside")]:
        with pytest.raises((ValueError, FileExistsError)):
            receiver.authorize("x", secrets.token_urlsafe(32), destination, 0, hashlib.sha256(b"").hexdigest(), time.time() + 60)
