"""TLS-pinned, ticket-scoped, bounded-memory transfer protocol shared by device workers.

The receiver must be provisioned through an authenticated control connection or an
out-of-band pairing step. Discovery announcements are never authorization.
"""
import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import ssl
import time

MAX_CHUNK = 256 * 1024
MAX_HEADER = 8192


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(MAX_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


async def read_json(reader):
    line = await asyncio.wait_for(reader.readline(), 30)
    if not line or len(line) > MAX_HEADER:
        raise ValueError("Invalid protocol header")
    result = json.loads(line)
    if not isinstance(result, dict):
        raise ValueError("Invalid protocol object")
    return result


async def write_json(writer, value):
    writer.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
    await writer.drain()


@dataclass
class ReceiveGrant:
    transfer_id: str
    token: str = field(repr=False)
    destination: Path
    size: int
    sha256: str
    expires_at: float
    completed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class PeerReceiver:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.grants = {}
        self.server = None

    def authorize(self, transfer_id, token, relative_path, size, sha256, expires_at):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", transfer_id):
            raise ValueError("Invalid transfer ID")
        if len(token) < 32 or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("Invalid ticket or checksum")
        if type(size) is not int or not 0 <= size <= 2**40:
            raise ValueError("Invalid transfer size")
        path = Path(relative_path)
        if path.is_absolute() or path.drive or any(p in {"..", "."} for p in path.parts):
            raise ValueError("Destination must be inside the receive folder")
        destination = (self.root / path).resolve()
        if not destination.is_relative_to(self.root) or destination == self.root or destination.exists():
            raise FileExistsError("Destination unavailable; overwrite is forbidden")
        if not time.time() < expires_at <= time.time() + 3600:
            raise ValueError("Invalid ticket lifetime")
        existing = self.grants.get(transfer_id)
        if existing and not existing.completed:
            raise ValueError("Transfer already authorized")
        self.grants[transfer_id] = ReceiveGrant(transfer_id, token, destination, size, sha256, expires_at)

    def partial(self, grant):
        # Ticket ID, not the user filename, selects the private partial artifact.
        return self.root / (".cortex-" + grant.transfer_id + ".part")

    async def start(self, ssl_context, host="127.0.0.1", port=0):
        if ssl_context is None:
            raise ValueError("TLS is mandatory")
        self.server = await asyncio.start_server(self.handle, host, port, ssl=ssl_context, limit=MAX_HEADER + 1)
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def handle(self, reader, writer):
        try:
            header = await read_json(reader)
            grant = self.grants.get(header.get("transfer_id"))
            token = header.get("token")
            if (grant is None or not isinstance(token, str) or
                    not hmac.compare_digest(grant.token, token) or time.time() >= grant.expires_at):
                raise PermissionError("Invalid or expired ticket")
            async with grant.lock:
                if grant.completed:
                    if header.get("operation") == "status":
                        await write_json(writer, {"status": "complete", "offset": grant.size, "sha256": grant.sha256})
                        return
                    raise PermissionError("Ticket consumed")
                part = self.partial(grant)
                # Reject links even inside the authorized folder.
                if part.is_symlink() or not grant.destination.resolve().is_relative_to(self.root):
                    raise PermissionError("Unsafe destination")
                offset = part.stat().st_size if part.exists() else 0
                operation = header.get("operation")
                if operation == "status":
                    await write_json(writer, {"status": "ready", "offset": offset})
                elif operation == "chunk":
                    length = header.get("length")
                    if (type(length) is not int or not 0 < length <= MAX_CHUNK or
                            type(header.get("offset")) is not int or header["offset"] != offset or
                            offset + length > grant.size):
                        raise ValueError("Invalid chunk boundary")
                    chunk = await asyncio.wait_for(reader.readexactly(length), 30)
                    if not hmac.compare_digest(hashlib.sha256(chunk).hexdigest(), str(header.get("sha256", ""))):
                        raise ValueError("Chunk checksum mismatch")
                    with open(part, "ab") as stream:
                        stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    await write_json(writer, {"status": "ready", "offset": offset + length})
                elif operation == "finish":
                    if grant.size == 0 and not part.exists():
                        part.touch(exist_ok=False)
                    if offset != grant.size or await asyncio.to_thread(digest_file, part) != grant.sha256:
                        raise ValueError("Final integrity mismatch")
                    grant.destination.parent.mkdir(parents=True, exist_ok=True)
                    if not grant.destination.parent.resolve().is_relative_to(self.root):
                        raise PermissionError("Unsafe destination parent")
                    # Hard link is atomic and refuses an existing destination on both Windows/Linux.
                    # It also avoids the overwrite race in exists()+replace(). Same volume required.
                    os.link(part, grant.destination)
                    part.unlink()
                    grant.completed = True
                    await write_json(writer, {"status": "complete", "offset": grant.size, "sha256": grant.sha256})
                elif operation == "cancel":
                    part.unlink(missing_ok=True)
                    self.grants.pop(grant.transfer_id, None)
                    await write_json(writer, {"status": "cancelled"})
                else:
                    raise ValueError("Unknown operation")
        except (ValueError, KeyError, TypeError, PermissionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            try:
                await write_json(writer, {"status": "error", "message": "Transfer rejected"})
            except (ConnectionError, OSError):
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


class PeerClient:
    def __init__(self, host, port, fingerprint, transfer_id, token):
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("A paired TLS certificate fingerprint is required")
        self.host, self.port, self.fingerprint = host, port, fingerprint
        self.transfer_id, self.token = transfer_id, token

    async def request(self, operation, payload=None, **fields):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Identity is checked against the authenticated pairing pin below.
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            self.host, self.port, ssl=context, server_hostname=None, limit=MAX_HEADER + 1), 10)
        try:
            cert = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if not hmac.compare_digest(hashlib.sha256(cert).hexdigest(), self.fingerprint):
                raise PermissionError("TLS pairing fingerprint mismatch")
            await write_json(writer, dict(fields, operation=operation, transfer_id=self.transfer_id, token=self.token))
            if payload:
                writer.write(payload)
                await writer.drain()
            result = await read_json(reader)
            if result.get("status") == "error":
                raise PermissionError("Peer rejected transfer")
            return result
        finally:
            writer.close()
            await writer.wait_closed()

    async def send_file(self, path, progress=None):
        path = Path(path)
        size = path.stat().st_size
        checksum = await asyncio.to_thread(digest_file, path)
        state = await self.request("status")
        offset = state.get("offset")
        if type(offset) is not int or not 0 <= offset <= size:
            raise ValueError("Invalid peer resume offset")
        if state.get("status") == "complete":
            if state.get("sha256") != checksum or offset != size:
                raise ValueError("Completed transfer does not match source")
            return state
        with path.open("rb") as stream:
            stream.seek(offset)
            while chunk := await asyncio.to_thread(stream.read, MAX_CHUNK):
                result = await self.request("chunk", chunk, offset=offset, length=len(chunk),
                                            sha256=hashlib.sha256(chunk).hexdigest())
                offset += len(chunk)
                if result.get("offset") != offset:
                    raise ValueError("Peer did not acknowledge the chunk")
                if progress:
                    await progress(offset, size)
        result = await self.request("finish")
        if result.get("sha256") != checksum or result.get("offset") != size:
            raise ValueError("Peer final checksum mismatch")
        return result
