"""
hub/pipeline/event_bus.py
-------------------------
Minimal typed async event bus for Cortex request pipeline decoupling.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Type

log = logging.getLogger("cortex.event_bus")


@dataclass
class CortexEvent:
    """Base event for the Cortex event bus."""
    event_type: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceConnected(CortexEvent):
    event_type: str = "device.connected"
    device_id: str = ""


@dataclass
class DeviceDisconnected(CortexEvent):
    event_type: str = "device.disconnected"
    device_id: str = ""


@dataclass
class WorldStateChanged(CortexEvent):
    event_type: str = "world_state.changed"
    signal_key: str = ""


@dataclass
class TaskStateChanged(CortexEvent):
    event_type: str = "task.state_changed"
    task_id: str = ""
    status: str = ""


@dataclass
class ActionExecuted(CortexEvent):
    event_type: str = "action.executed"
    action: str = ""
    target_device: str = ""
    success: bool = True
    result: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionFailed(CortexEvent):
    event_type: str = "action.failed"
    action: str = ""
    target_device: str = ""
    error: str = ""


EventHandler = Callable[[CortexEvent], Coroutine[Any, Any, None]]


class EventBus:
    """Minimal typed async event bus."""

    _instance: Optional["EventBus"] = None

    def __init__(self):
        self._subscribers: Dict[Type[CortexEvent], List[EventHandler]] = {}
        self._string_subscribers: Dict[str, List[EventHandler]] = {}

    @classmethod
    def get_instance(cls) -> "EventBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset subscribers for hermetic testing."""
        if cls._instance:
            cls._instance._subscribers.clear()
            cls._instance._string_subscribers.clear()

    def subscribe(self, event_type: Type[CortexEvent] | str, handler: EventHandler) -> None:
        """Subscribe an async handler to a typed event class or string event name."""
        if isinstance(event_type, type) and issubclass(event_type, CortexEvent):
            self._subscribers.setdefault(event_type, []).append(handler)
        else:
            self._string_subscribers.setdefault(str(event_type), []).append(handler)

    async def publish(self, event: CortexEvent) -> None:
        """Publish an event to all registered subscribers asynchronously."""
        handlers: List[EventHandler] = []

        # Find matching type handlers
        for ev_cls, h_list in self._subscribers.items():
            if isinstance(event, ev_cls):
                handlers.extend(h_list)

        # Find matching string handlers
        if event.event_type in self._string_subscribers:
            handlers.extend(self._string_subscribers[event.event_type])

        if not handlers:
            return

        for h in handlers:
            try:
                res = h(event)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                log.warning(f"[EventBus] Handler error on {event.event_type}: {exc}")
