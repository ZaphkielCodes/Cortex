"""Keep the hub socket responsive while serializing native desktop execution."""
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json


class CommandDispatcher:
    def __init__(self, max_pending=128, retained_results=256):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cortex-desktop")
        self.serial = asyncio.Lock()
        self.calls = OrderedDict()
        self.max_pending, self.retained_results = max_pending, retained_results

    async def dispatch(self, command_id, command, arguments, execute):
        fingerprint = hashlib.sha256(json.dumps([command, arguments], sort_keys=True).encode()).hexdigest()
        if not command_id:
            return {"status": "failure", "message": "The command has no correlation identifier."}
        previous = self.calls.get(command_id)
        if previous:
            if previous[0] != fingerprint:
                return {"status": "failure", "message": "That command identifier belongs to different input."}
            return await asyncio.shield(previous[1])
        if sum(not item[1].done() for item in self.calls.values()) >= self.max_pending:
            return {"status": "failure", "message": "The device command queue is full. No action was started."}
        job = asyncio.create_task(self._execute(execute))
        self.calls[command_id] = (fingerprint, job)
        job.add_done_callback(lambda _: self._prune())
        return await asyncio.shield(job)

    async def _execute(self, execute):
        async with self.serial:
            try:
                # execute itself selects an async transfer/control or the single
                # worker thread. Never block WebSocket result/approval reception.
                return await execute(self.executor)
            except Exception:
                return {"status": "failure", "message": "The device action failed; completion could not be verified."}

    def _prune(self):
        finished = [key for key, value in self.calls.items() if value[1].done()]
        for key in finished[:-self.retained_results]:
            self.calls.pop(key, None)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
