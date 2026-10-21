"""
hub/executor_registry.py
------------------------
ExecutorRegistry tracks every currently known execution node for Cortex.

Consumes:
- DeviceHandle models
- Authenticated websocket state
- Capability advertisement
- Heartbeat state & telemetry
- Advisory resource/workload profiles

Invariants:
- Online requires active authenticated connection AND fresh heartbeat.
- Never infer online merely from historical DB state.
- ASUS is role = primary_admin, an execution node, not Cortex itself.
"""

import asyncio
from datetime import datetime, timezone
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from .device_handle import (
    DeviceHandle,
    normalize_device_type,
    normalize_role,
    normalize_trust_level,
    default_transports,
    default_workload,
)

log = logging.getLogger("cortex.executor_registry")

DEFAULT_HEARTBEAT_TIMEOUT = float(os.getenv("CORTEX_HEARTBEAT_TIMEOUT", "60.0"))


class ExecutorRegistry:
    """Central registry of distributed execution nodes."""

    _instance: Optional["ExecutorRegistry"] = None

    def __init__(self, heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT):
        self.heartbeat_timeout = heartbeat_timeout
        # device_id -> DeviceHandle
        self._handles: Dict[str, DeviceHandle] = {}
        # device_id -> monotonic timestamp of last heartbeat
        self._last_heartbeat_time: Dict[str, float] = {}
        # device_id -> active websocket or connection object
        self._live_connections: Dict[str, Any] = {}
        # device overrides for tests
        self._overrides: Optional[List[DeviceHandle]] = None

    @classmethod
    def get_instance(cls) -> "ExecutorRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset registry for clean test isolation."""
        cls._instance = None

    def set_heartbeat_timeout(self, timeout_seconds: float) -> None:
        self.heartbeat_timeout = max(0.1, timeout_seconds)

    def set_device_overrides(self, handles: Optional[List[DeviceHandle]]) -> None:
        self._overrides = handles

    def register_device(self, handle: DeviceHandle) -> None:
        """Register or update an execution node handle."""
        self._handles[handle.device_id] = handle

    def on_device_connected(self, device_id: str, websocket: Any) -> None:
        """Called when a device establishes an authenticated WebSocket."""
        self._live_connections[device_id] = websocket
        now_mono = time.monotonic()
        now_iso = datetime.now(timezone.utc).isoformat()
        self._last_heartbeat_time[device_id] = now_mono

        handle = self._handles.get(device_id)
        if handle:
            handle.online = True
            handle.authenticated = True
            handle.last_seen = now_iso
            handle.last_heartbeat = now_iso
        else:
            self._handles[device_id] = DeviceHandle(
                device_id=device_id,
                display_name=device_id,
                authenticated=True,
                online=True,
                last_seen=now_iso,
                last_heartbeat=now_iso,
            )

    def set_device_online(self, device_id: str, online: bool) -> None:
        """Manually set device online status (for simulated test harnesses / lifecycle control)."""
        handle = self._handles.get(device_id)
        if online:
            self._live_connections[device_id] = True
            now_mono = time.monotonic()
            now_iso = datetime.now(timezone.utc).isoformat()
            self._last_heartbeat_time[device_id] = now_mono
            if handle:
                handle.online = True
                handle.authenticated = True
                handle.last_seen = now_iso
                handle.last_heartbeat = now_iso
        else:
            self._live_connections.pop(device_id, None)
            self._last_heartbeat_time.pop(device_id, None)
            if handle:
                handle.online = False
                handle.authenticated = False

    def on_device_authenticated(
        self,
        device_id: str,
        capabilities: List[str],
        platform: str = "unknown",
        device_type: Optional[str] = None,
        agent_version: Optional[str] = None,
        transports: Optional[Dict[str, bool]] = None,
        role: Optional[str] = None,
        trust_level: Optional[str] = None,
    ) -> None:
        """Called after authentication handshake with advertised capabilities."""
        handle = self._handles.get(device_id)
        now_mono = time.monotonic()
        now_iso = datetime.now(timezone.utc).isoformat()
        self._last_heartbeat_time[device_id] = now_mono

        if not handle:
            handle = DeviceHandle(
                device_id=device_id,
                display_name=device_id,
                platform=platform,
                device_type=normalize_device_type(device_type),
                role=normalize_role(role),
                trust_level=normalize_trust_level(trust_level, role),
                authenticated=True,
                online=True,
                last_seen=now_iso,
                last_heartbeat=now_iso,
                agent_version=agent_version,
                capabilities=list(capabilities),
                transports=transports or default_transports(),
            )
            self._handles[device_id] = handle
        else:
            handle.capabilities = list(capabilities)
            if platform and platform != "unknown":
                handle.platform = platform
            if device_type:
                handle.device_type = normalize_device_type(device_type)
            if agent_version:
                handle.agent_version = agent_version
            if transports:
                handle.transports = dict(transports)
            if role:
                handle.role = normalize_role(role)
            if trust_level:
                handle.trust_level = normalize_trust_level(trust_level, handle.role)
            handle.authenticated = True
            handle.online = True
            handle.last_seen = now_iso
            handle.last_heartbeat = now_iso

        log.info(f"Executor registered | {device_id} | caps={len(capabilities)} | role={handle.role}")

    def on_heartbeat(
        self,
        device_id: str,
        timestamp: Optional[float] = None,
        agent_version: Optional[str] = None,
        state: Optional[str] = None,
        cpu_load: Optional[float] = None,
        memory_load: Optional[float] = None,
        battery: Optional[Dict[str, Any]] = None,
        active_task_count: int = 0,
        telemetry: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Process lightweight heartbeat ping."""
        now_mono = time.monotonic()
        now_iso = datetime.now(timezone.utc).isoformat()
        self._last_heartbeat_time[device_id] = now_mono

        handle = self._handles.get(device_id)
        if not handle:
            handle = DeviceHandle(
                device_id=device_id,
                display_name=device_id,
                online=True,
                authenticated=True,
                last_seen=now_iso,
                last_heartbeat=now_iso,
            )
            self._handles[device_id] = handle

        handle.online = True
        handle.last_heartbeat = now_iso
        handle.last_seen = now_iso
        if agent_version:
            handle.agent_version = agent_version

        # Resource profile
        res = handle.resource_profile
        if cpu_load is not None:
            res["cpu_load"] = cpu_load
        if memory_load is not None:
            res["memory_load"] = memory_load
        if battery is not None:
            res["battery"] = battery
            if "level" in battery:
                res["battery_level"] = battery["level"]
            if "charging" in battery:
                res["is_charging"] = bool(battery["charging"])
        if telemetry:
            res.update(telemetry)

        # Workload
        handle.workload["active_task_count"] = active_task_count
        if state:
            handle.metadata["state"] = state

    def on_device_disconnected(self, device_id: str) -> None:
        """Called when a WebSocket disconnects."""
        self._live_connections.pop(device_id, None)
        self._last_heartbeat_time.pop(device_id, None)
        handle = self._handles.get(device_id)
        if handle:
            handle.online = False
            handle.authenticated = False
            handle.last_seen = datetime.now(timezone.utc).isoformat()

    def is_device_online(self, device_id: str) -> bool:
        """
        Check if a device is currently online.
        Requires active authenticated connection AND unexpired heartbeat.
        """
        if self._overrides is not None:
            for h in self._overrides:
                if h.device_id == device_id:
                    return h.online

        # Check live connection
        has_conn = device_id in self._live_connections
        conn_map = None
        if not has_conn:
            try:
                from .goals import GoalRunner
                runner = GoalRunner.get_instance()
                ctrl = runner.get_control_layer()
                if ctrl and hasattr(ctrl, "connected_devices") and isinstance(ctrl.connected_devices, dict):
                    conn_map = ctrl.connected_devices
                    has_conn = device_id in conn_map
            except Exception:
                pass

        if not has_conn and conn_map is None:
            try:
                import hub.main as main_mod
                if hasattr(main_mod, "CONNECTED_DEVICES") and isinstance(main_mod.CONNECTED_DEVICES, dict):
                    conn_map = main_mod.CONNECTED_DEVICES
                    has_conn = device_id in conn_map
            except Exception:
                has_conn = False

        if not has_conn:
            handle = self._handles.get(device_id)
            if handle is not None and not handle.online:
                return False
            return False

        # Check heartbeat freshness
        last_hb = self._last_heartbeat_time.get(device_id)
        if last_hb is None:
            # Active authenticated connection established without timeout expiry yet
            return True

        elapsed = time.monotonic() - last_hb
        return elapsed <= self.heartbeat_timeout

    def get_heartbeat_freshness(self, device_id: str) -> Optional[float]:
        """Return elapsed seconds since last heartbeat ping, or None if unknown."""
        last_hb = self._last_heartbeat_time.get(device_id)
        if last_hb is None:
            return None
        return round(time.monotonic() - last_hb, 2)

    def get_device_handle(self, device_id: str) -> Optional[DeviceHandle]:
        """Retrieve DeviceHandle with synchronized live status."""
        if self._overrides is not None:
            for h in self._overrides:
                if h.device_id == device_id:
                    return h

        handle = self._handles.get(device_id)
        if not handle:
            return None

        # Synchronize live online state
        handle.online = self.is_device_online(device_id)
        return handle

    def list_device_handles(self) -> List[DeviceHandle]:
        """List all known execution node handles with synchronized live state."""
        if self._overrides is not None:
            return list(self._overrides)

        handles: List[DeviceHandle] = []
        for dev_id, h in self._handles.items():
            h.online = self.is_device_online(dev_id)
            handles.append(h)
        return handles

    def get_capable_devices(self, capability: str, online_only: bool = True) -> List[DeviceHandle]:
        """Find execution nodes that support a required capability."""
        capable: List[DeviceHandle] = []
        for handle in self.list_device_handles():
            if online_only and not handle.online:
                continue
            if handle.supports_capability(capability):
                capable.append(handle)
        return capable

    def get_executor_status(self) -> Dict[str, Any]:
        """Summary of executor nodes, online counts, and capability distribution."""
        handles = self.list_device_handles()
        online_nodes = [h.device_id for h in handles if h.online]
        offline_nodes = [h.device_id for h in handles if not h.online]

        cap_dist: Dict[str, List[str]] = {}
        for h in handles:
            for c in h.capabilities:
                cap_dist.setdefault(c, []).append(h.device_id)

        return {
            "total_devices": len(handles),
            "online_devices": online_nodes,
            "offline_devices": offline_nodes,
            "nodes": [h.to_dict() for h in handles],
            "capabilities_distribution": cap_dist,
        }
