"""
hub/tasks.py
------------
Task Engine for Phase 5.
Handles scheduling, idempotency, retries, and cross-device rerouting.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4
from typing import Optional

from .database import (
    get_tasks,
    update_task,
    get_task,
    log_action,
    get_device_capabilities,
    list_devices
)
from .permissions import check_permission
from .models import CommandMessage

log = logging.getLogger(__name__)

class TaskEngine:
    def __init__(self, hub_state):
        # hub_state gives access to connected websockets
        self.hub_state = hub_state
        self._running = False
        self._task = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("Task Engine started.")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Task Engine stopped.")

    async def _loop(self):
        while self._running:
            try:
                await self.process_pending_tasks()
            except Exception as e:
                log.error(f"Task engine error: {e}")
            await asyncio.sleep(5)  # Check every 5 seconds

    async def process_pending_tasks(self):
        tasks = await get_tasks()
        now = datetime.now(timezone.utc)
        
        for task in tasks:
            if task["status"] == "pending":
                # Check trigger time for scheduled tasks
                if task["type"] == "scheduled" and task["trigger_time"]:
                    try:
                        trigger = datetime.fromisoformat(task["trigger_time"])
                        if now >= trigger:
                            await self.execute_task(task)
                    except ValueError:
                        log.error(f"Invalid trigger time for task {task['id']}")
                        await update_task(task["id"], {"status": "failed", "last_error": "Invalid trigger time format"})
                elif task["type"] in ["one_time", "background"]:
                    await self.execute_task(task)
                elif task["type"] == "recurring":
                    # TODO: proper cron evaluation (simplified for now)
                    pass

    async def execute_task(self, task: dict):
        """Executes a task securely, persisting state transitions."""
        task_id = task["id"]
        
        # Check idempotency
        if task["status"] in ["running", "completed", "cancelled"]:
            log.warning(f"Task {task_id} already in state {task['status']}, skipping execution.")
            return

        # State -> running
        execution_id = uuid4().hex
        await update_task(task_id, {"status": "running", "execution_id": execution_id})
        log.info(f"Task {task_id} marked as running (execution_id: {execution_id}).")

        target_device = task["assigned_device"]
        command = task["command"]
        args_raw = task.get("args", "[]")
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        req_cap = task.get("required_capability", "")
        reroutable = task.get("reroutable", False)

        # 1. Resolve Target Device
        connected_devices = self.hub_state.get("CONNECTED_DEVICES", {})
        
        if not target_device or target_device not in connected_devices:
            if reroutable:
                target_device = await self._find_alternate_device(req_cap, connected_devices)
                if target_device:
                    await update_task(task_id, {"assigned_device": target_device})
                    log.info(f"Task {task_id} rerouted to {target_device}")
                else:
                    await self._fail_task(task_id, "No online devices with required capabilities.")
                    return
            else:
                await self._fail_task(task_id, f"Target device {target_device} offline and task not reroutable.")
                return

        # 2. Permission Check
        # For background tasks, the hub acts as admin or we enforce strict tier rules.
        # Assuming hub acts as admin for now.
        perm = await check_permission("admin", "hub", target_device, req_cap or command)
        
        if not perm.allowed:
            await self._fail_task(task_id, f"Permission denied: {perm.reason}")
            return
            
        if perm.needs_confirmation:
            await update_task(task_id, {"status": "waiting_for_approval"})
            log.info(f"Task {task_id} needs confirmation.")
            # Typically here we would trigger the confirmation flow.
            return

        # 3. Dispatch
        ws = connected_devices[target_device]
        cmd_msg = CommandMessage(command=command, args=args, command_id=execution_id)
        
        try:
            await ws.send_text(cmd_msg.model_dump_json())
            await update_task(task_id, {"status": "waiting"})  # waiting for result
            await log_action("hub", f"dispatch_task_{command}", "success", f"Dispatched task {task_id} to {target_device}")
        except Exception as e:
            await self._fail_task(task_id, f"Dispatch failed: {str(e)}")

    async def handle_result(self, task_id: str, success: bool, message: str):
        """Called when a device returns a result for a task execution."""
        status = "completed" if success else "failed"
        await update_task(task_id, {"status": status, "last_error": message if not success else ""})
        log.info(f"Task {task_id} {status}: {message}")

    async def _fail_task(self, task_id: str, reason: str):
        await update_task(task_id, {"status": "failed", "last_error": reason})
        log.warning(f"Task {task_id} failed: {reason}")
        await log_action("hub", "task_failed", "failure", reason, command_id=task_id)

    async def _find_alternate_device(self, capability: str, connected_devices: dict) -> Optional[str]:
        if not capability:
            # Pick first available if no capability required
            return next(iter(connected_devices.keys()), None)
            
        for d_id in connected_devices.keys():
            caps = await get_device_capabilities(d_id)
            if capability in caps:
                # Need to check permissions for rerouting
                perm = await check_permission("admin", "hub", d_id, capability)
                if perm.allowed:
                    return d_id
        return None
