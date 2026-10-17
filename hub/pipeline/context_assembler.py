"""
hub/pipeline/context_assembler.py
---------------------------------
Assembles bounded relevant context for semantic reasoning (Section 8).
Applies A7 data minimization principles.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..capability_registry import CanonicalCapabilityRegistry
from ..world_state import WorldStateManager


@dataclass
class AssembledContext:
    """Bounded, privacy-minimized context for semantic reasoning."""
    source_device: str
    foreground_app: Optional[str] = None
    active_window: Optional[str] = None
    previous_app: Optional[str] = None
    recent_foreground_history: List[Dict[str, Any]] = field(default_factory=list)
    last_confirmed_target: Optional[str] = None
    last_screenshot: Optional[Dict[str, Any]] = None
    recent_screen_summary: Optional[str] = None
    active_task: Optional[str] = None
    available_devices: List[str] = field(default_factory=list)
    recent_conversation_turns: List[Dict[str, str]] = field(default_factory=list)
    available_capabilities: List[str] = field(default_factory=list)
    raw_context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_device": self.source_device,
            "foreground_app": self.foreground_app,
            "active_window": self.active_window,
            "previous_app": self.previous_app,
            "recent_foreground_history": self.recent_foreground_history,
            "last_confirmed_target": self.last_confirmed_target,
            "recent_screen_summary": self.recent_screen_summary,
            "active_task": self.active_task,
            "available_devices": self.available_devices,
            "recent_conversation_turns": self.recent_conversation_turns,
            "available_capabilities": self.available_capabilities,
        }


class ContextAssembler:
    """
    Assembles bounded situational context without dumping the whole database.
    """

    @classmethod
    async def assemble(
        cls,
        source_device_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AssembledContext:
        ctx = context or {}
        wsm = WorldStateManager.get_instance()

        # 1. Live and Previous Foreground
        fg_app = None
        active_win = None
        fg_sig = wsm.get_signal(f"device.foreground:{source_device_id}")
        if fg_sig and fg_sig.payload.get("app_name"):
            fg_app = fg_sig.payload["app_name"]
            active_win = fg_sig.payload.get("window_title")

        if not fg_app and "world_state" in ctx and isinstance(ctx["world_state"], dict):
            ws_dict = ctx["world_state"]
            fg_app = ws_dict.get("foreground_app") or ws_dict.get("foreground_activity", {}).get("app_name")
            active_win = ws_dict.get("foreground_activity", {}).get("window_title")

        prev_app = None
        prev_fg = wsm.get_previous_foreground()
        if prev_fg:
            prev_app = prev_fg.get("app_name")

        # 2. Foreground History
        fg_history = wsm.get_foreground_history(limit=5)

        # 3. Last confirmed target & references from session
        session_refs = ctx.get("session", {}).get("references", {}) or ctx.get("references", {})
        last_confirmed = (
            session_refs.get("last_confirmed_app")
            or session_refs.get("grounded_target", {}).get("target_name")
            or session_refs.get("last_app")
        )

        # 4. Screen Perception
        screen_sum = None
        screen_sig = wsm.get_signal(f"screen.summary:{source_device_id}") or wsm.get_signal("screen.summary")
        if screen_sig:
            screen_sum = screen_sig.payload.get("summary")

        # 5. Active Task
        active_task = None
        task_sig = wsm.get_signal("task.active")
        if task_sig:
            active_task = task_sig.payload.get("title")

        # 6. Connected/Available Devices
        avail_devices = [source_device_id]
        try:
            import hub.main
            online = getattr(hub.main, "CONNECTED_DEVICES", {})
            for d in online.keys():
                if d not in avail_devices:
                    avail_devices.append(d)
        except Exception:
            pass

        # 7. Recent conversation turns (bounded to last 4)
        turns: List[Dict[str, str]] = []
        raw_turns = ctx.get("conversation_turns") or ctx.get("history") or []
        for t in raw_turns[-4:]:
            if isinstance(t, dict):
                turns.append({
                    "role": t.get("role", "user"),
                    "text": t.get("text") or t.get("content") or "",
                })

        # 8. Available Capabilities
        reg = CanonicalCapabilityRegistry.get_instance()
        avail_caps = [c.name for c in reg.get_available_capabilities(device_id=source_device_id)]

        return AssembledContext(
            source_device=source_device_id,
            foreground_app=fg_app,
            active_window=active_win,
            previous_app=prev_app,
            recent_foreground_history=fg_history,
            last_confirmed_target=last_confirmed,
            last_screenshot=ctx.get("screenshot"),
            recent_screen_summary=screen_sum,
            active_task=active_task,
            available_devices=avail_devices,
            recent_conversation_turns=turns,
            available_capabilities=avail_caps,
            raw_context=ctx,
        )
