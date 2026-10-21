"""Device-owned transfer execution. File bytes never pass through the hub."""
import asyncio
import os
from pathlib import Path

from shared.peer_identity import load_identity
from shared.peer_transfer import PeerClient, PeerReceiver, digest_file


class PeerWorker:
    def __init__(self):
        self.receiver = None
        self.port = None
        self.fingerprint = None

    async def execute(self, operation, args):
        try:
            if operation == "transfer.inspect":
                path = Path(args["path"])
                if not path.is_file():
                    raise FileNotFoundError("Source file missing")
                data = {"size": path.stat().st_size, "sha256": await asyncio.to_thread(digest_file, path)}
            elif operation == "transfer.prepare":
                root = os.environ.get("CORTEX_RECEIVE_ROOT")
                if not root:
                    raise ValueError("Configure CORTEX_RECEIVE_ROOT before receiving files")
                if self.receiver is None:
                    context, self.fingerprint = load_identity(Path.home() / ".cortex" / "peer-identity")
                    self.receiver = PeerReceiver(root)
                    self.port = await self.receiver.start(context, os.getenv("CORTEX_TRANSFER_BIND", "127.0.0.1"),
                                                          int(os.getenv("CORTEX_TRANSFER_PORT", "0")))
                self.receiver.authorize(args["transfer_id"], args["token"], args["destination"],
                                        args["size"], args["sha256"], args["expires_at"])
                data = {"port": self.port, "fingerprint": self.fingerprint,
                        "host": os.getenv("CORTEX_TRANSFER_ADVERTISE_HOST", "127.0.0.1")}
            elif operation == "transfer.send":
                client = PeerClient(args["host"], args["port"], args["fingerprint"], args["transfer_id"], args["token"])
                data = await client.send_file(args["path"])
            elif operation == "transfer.cancel":
                if self.receiver is None:
                    raise ValueError("No active receiver")
                grant = self.receiver.grants.get(args["transfer_id"])
                if grant:
                    async with grant.lock:
                        if grant.completed:
                            return {"status": "failure", "message": "The receiving file is already complete; it was preserved."}
                        self.receiver.grants.pop(args["transfer_id"], None)
                        self.receiver.partial(grant).unlink(missing_ok=True)
                data = {"cancelled": bool(grant)}
            else:
                raise ValueError("Unsupported transfer operation")
            return {"status": "success", "message": "Transfer operation verified.", "data": data}
        except (OSError, ValueError, KeyError, asyncio.TimeoutError):
            return {"status": "failure", "message": "Transfer unavailable, rejected, or interrupted. Completion was not verified."}

    async def close(self):
        if self.receiver:
            await self.receiver.close()
