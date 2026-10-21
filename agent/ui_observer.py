"""
agent/ui_observer.py
--------------------
Structured Desktop UI Observation Engine for Cortex (Phase A10).

Fuses:
1. Windows Native Accessibility Tree (UI Automation Core COM)
2. Foreground Window & Process Metadata
3. Multimodal Vision / OCR Perception Fallback

Emits versioned, structured UIObservation instances with deterministic element IDs
and fencing tokens to detect stale interfaces.
"""

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import logging
import os
import platform
import time
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import uuid

from .ui_models import UIElement, UIObservation

log = logging.getLogger("cortex.ui_observer")

# Maximum actionable elements to extract per observation to ensure bounded latency
MAX_ELEMENTS_PER_OBSERVATION = 200

# Cache bound for observations
MAX_OBSERVATIONS_HISTORY = 30

# UIAutomation Control Type ID -> Semantic Role mapping
UIA_CONTROL_TYPE_MAP = {
    50000: "button",
    50001: "calendar",
    50002: "checkbox",
    50003: "combobox",
    50004: "input",          # Edit
    50005: "link",           # Hyperlink
    50006: "image",
    50007: "listitem",
    50008: "list",
    50009: "menu",
    50010: "menubar",
    50011: "menuitem",
    50012: "progressbar",
    50013: "radiobutton",
    50014: "scrollbar",
    50015: "slider",
    50016: "spinner",
    50017: "statusbar",
    50018: "tab",
    50019: "tabitem",
    50020: "text",
    50021: "toolbar",
    50022: "tooltip",
    50023: "tree",
    50024: "treeitem",
    50025: "custom",
    50026: "group",
    50027: "thumb",
    50028: "datagrid",
    50029: "dataitem",
    50030: "document",
    50031: "splitbutton",
    50032: "window",
    50033: "pane",
    50034: "header",
    50035: "headeritem",
    50036: "table",
    50037: "titlebar",
    50038: "separator",
    50039: "semanticzoom",
    50040: "appbar",
}


# Runtime override hook for unit and integration testing
_OBSERVER_OVERRIDE: Optional[Any] = None


def set_ui_observer_override(override: Optional[Any]) -> None:
    """Set or clear a runtime override for UI observation (for testing)."""
    global _OBSERVER_OVERRIDE
    _OBSERVER_OVERRIDE = override


def get_ui_observer_override() -> Optional[Any]:
    return _OBSERVER_OVERRIDE


class UIObserver:
    """
    Singleton observing desktop UI via accessibility automation and vision fallback.
    Maintains a versioned observation cache for stale-screen detection.
    """

    _instance: Optional["UIObserver"] = None

    @property
    def _uia_client(self):
        return getattr(getattr(self, '_uia_thread', None), 'client', None)

    @_uia_client.setter
    def _uia_client(self, value):
        if not hasattr(self, '_uia_thread'):
            self._uia_thread = threading.local()
        self._uia_thread.client = value

    @property
    def _uia_module(self):
        return getattr(getattr(self, '_uia_thread', None), 'module', None)

    @_uia_module.setter
    def _uia_module(self, value):
        if not hasattr(self, '_uia_thread'):
            self._uia_thread = threading.local()
        self._uia_thread.module = value

    def __init__(self):
        self._observations_by_id: Dict[str, UIObservation] = {}
        self._latest_by_device: Dict[str, UIObservation] = {}
        self._history: List[UIObservation] = []
        self._uia_client = None
        self._uia_module = None
        self._init_uia()

    @classmethod
    def get_instance(cls) -> "UIObserver":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def _init_uia(self) -> None:
        """Lazily initialize Windows UIAutomation COM client."""
        if platform.system() != "Windows":
            return
        try:
            import comtypes.client
            comtypes.CoInitialize()
            self._uia_module = comtypes.client.GetModule("UIAutomationCore.dll")
            self._uia_client = comtypes.client.CreateObject(
                self._uia_module.CUIAutomation,
                interface=self._uia_module.IUIAutomation,
            )
            log.info("[UIObserver] UIAutomationCore bound successfully.")
        except Exception as exc:
            log.debug(f"[UIObserver] UIAutomation COM init failed: {exc}")
            self._uia_client = None
            self._uia_module = None

    def get_screen_size(self) -> Dict[str, int]:
        """Get primary display dimensions in pixels."""
        if platform.system() == "Windows":
            try:
                user32 = ctypes.windll.user32
                w = user32.GetSystemMetrics(0)
                h = user32.GetSystemMetrics(1)
                if w > 0 and h > 0:
                    return {"width": int(w), "height": int(h)}
            except Exception:
                pass
        return {"width": 1920, "height": 1080}

    def _get_active_window_info(self, target_window_id=None):
        """Observe the requested window or actual foreground, without app-name guesses."""
        if platform.system() == "Windows":
            try:
                user32 = ctypes.windll.user32
                hwnd = target_window_id or user32.GetForegroundWindow()
                if hwnd and user32.IsWindow(hwnd):
                    title = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
                    user32.GetWindowTextW(hwnd, title, len(title))
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    from .window_identity import window_identity
                    identity = window_identity(hwnd, user32)
                    return {"window_id": int(hwnd), "window_title": title.value, **identity,
                            "window_bounds": [rect.left, rect.top, rect.right-rect.left, rect.bottom-rect.top]}
            except Exception:
                pass
        from .desktop_actions import list_windows
        windows = list_windows().get("windows", [])
        window = next((w for w in windows if (w.get("window_id") == target_window_id
                       if target_window_id else w.get("foreground"))), None)
        if window:
            bounds = window.get("bounds") or {}
            return {"window_id": window.get("window_id"), "window_title": window.get("title", ""),
                    "pid":window.get("pid"),
                    "foreground_app": window.get("process_name", ""),
                    "window_bounds": [bounds.get(k, 0) for k in ("x", "y", "width", "height")]}
        return {"window_id": None, "window_title": "", "foreground_app": "Unknown", "window_bounds": None}

    def _element_snapshot(self, item, index):
        """Read editability and identity from UIA patterns, never from app names."""
        def prop(name, default=None):
            try:
                return getattr(item, name)
            except Exception:
                return default
        role = UIA_CONTROL_TYPE_MAP.get(int(item.CurrentControlType), "control")
        rect = item.CurrentBoundingRectangle
        bounds = [int(rect.left), int(rect.top), int(rect.right-rect.left), int(rect.bottom-rect.top)]
        if prop("CurrentIsOffscreen", False) or bounds[2] <= 0 or bounds[3] <= 0:
            return None
        password = bool(prop("CurrentIsPassword", False))
        metadata = {"source": "uia", "automation_id": prop("CurrentAutomationId", ""),
                    "class_name": prop("CurrentClassName", ""),
                    "has_keyboard_focus": bool(prop("CurrentHasKeyboardFocus", False)),
                    "focusable": bool(prop("CurrentIsKeyboardFocusable", False)),
                    "is_password": password, "is_offscreen": False}
        try:
            metadata["runtime_id"] = list(item.GetRuntimeId())
        except Exception:
            pass
        for pattern_id, interface, attribute, key in (
            (10010,"IUIAutomationSelectionItemPattern","CurrentIsSelected","selected"),
            (10015,"IUIAutomationTogglePattern","CurrentToggleState","toggle_state"),
            (10005,"IUIAutomationExpandCollapsePattern","CurrentExpandCollapseState","expand_state")):
            try:
                pattern = item.GetCurrentPattern(pattern_id).QueryInterface(getattr(self._uia_module,interface))
                metadata[key] = getattr(pattern,attribute)
            except Exception:
                pass
        text = ""
        editable = role in ("input", "edit")
        try:
            value = item.GetCurrentPattern(10002).QueryInterface(self._uia_module.IUIAutomationValuePattern)
            metadata["is_read_only"] = bool(value.CurrentIsReadOnly)
            editable = not metadata["is_read_only"]
            if not password:
                text = (value.CurrentValue or "")[:2000]
                metadata["value_available"] = True
        except Exception:
            pass
        try:
            # TextEdit pattern identifies editable rich text, including custom controls.
            editable = editable or bool(item.GetCurrentPattern(10032))
        except Exception:
            pass
        if not password and not metadata.get("value_available"):
            try:
                pattern = item.GetCurrentPattern(10014).QueryInterface(self._uia_module.IUIAutomationTextPattern)
                text = pattern.DocumentRange.GetText(2000) or ""
                metadata["value_available"] = True
            except Exception:
                pass
        metadata["editable"] = editable and not metadata.get("is_read_only", False)
        label = "Protected field" if password else (prop("CurrentName", "") or "").strip()
        if not label and role in ("pane", "group", "window", "custom") and not editable:
            return None
        return UIElement(element_id=f"el_{index}", role=role, label=label or role.capitalize(),
                         text=text, bounds=bounds, enabled=bool(prop("CurrentIsEnabled", False)), metadata=metadata)

    def element_is_current(self, observation, element, max_bounds_diff=8):
        """Check the selected control again immediately before native input."""
        current = self._extract_accessibility_elements(observation.window_id, observation.screen_size)
        runtime_id = element.metadata.get("runtime_id")
        if runtime_id:
            matches = [e for e in current if e.metadata.get("runtime_id") == runtime_id]
        else:
            automation_id = element.metadata.get("automation_id")
            matches = [e for e in current if e.role == element.role and e.label == element.label
                and (not automation_id or e.metadata.get("automation_id") == automation_id)]
        if len(matches) != 1:
            return False
        live = matches[0]
        return live.enabled and live.role == element.role and live.label == element.label and all(
            abs(old-new) <= max_bounds_diff for old,new in zip(element.bounds, live.bounds))

    def _extract_accessibility_elements(self, hwnd, screen_size):
        # UIA COM interfaces belong to the calling thread. Observation may run
        # in a worker while actions run on the agent's event-loop thread.
        if self._uia_client is None:
            self._init_uia()
        if not self._uia_client or not self._uia_module or not hwnd:
            return []
        elements = []
        try:
            root = self._uia_client.ElementFromHandle(hwnd)
            found = root.FindAll(4, self._uia_client.CreateTrueCondition())
            for i in range(min(found.Length, MAX_ELEMENTS_PER_OBSERVATION * 2)):
                if len(elements) >= MAX_ELEMENTS_PER_OBSERVATION:
                    break
                try:
                    element = self._element_snapshot(found.GetElement(i), len(elements) + 1)
                    if element:
                        elements.append(element)
                except Exception:
                    continue
        except Exception as exc:
            log.warning("Accessibility tree unavailable: %s", type(exc).__name__)
        return elements

    def focus_text_target(self, observation, element):
        """Re-identify the observed control and confirm OS keyboard focus before input."""
        if self._uia_client is None:
            self._init_uia()
        if not self._uia_client or not observation.window_id or not element.metadata.get("runtime_id"):
            return False
        if self._get_active_window_info().get("window_id") != observation.window_id:
            return False
        if not self.element_is_current(observation, element):
            return False
        try:
            root = self._uia_client.ElementFromHandle(observation.window_id)
            found = root.FindAll(4, self._uia_client.CreateTrueCondition())
            for i in range(min(found.Length, MAX_ELEMENTS_PER_OBSERVATION * 2)):
                item = found.GetElement(i)
                if list(item.GetRuntimeId()) == element.metadata["runtime_id"]:
                    from .ui_text import is_editable
                    live = self._element_snapshot(item, 0)
                    if not live or not is_editable(live):
                        return False
                    item.SetFocus()
                    return self.text_target_has_focus(observation, element)
        except Exception:
            pass
        return False

    def text_target_has_focus(self, observation, element):
        try:
            if self._get_active_window_info().get("window_id") != observation.window_id:
                return False
            focused = self._uia_client.GetFocusedElement()
            return list(focused.GetRuntimeId()) == element.metadata["runtime_id"]
        except Exception:
            return False

    async def _extract_vision_fallback_elements(
        self,
        image_ref: Optional[str],
        device_id: str,
        screen_size: Dict[str, int],
        existing_elements: List[UIElement],
    ) -> List[UIElement]:
        """Complementary or fallback vision perception for unexposed UI controls."""
        vision_elements: List[UIElement] = []
        try:
            from hub.vision_provider import get_vision_provider
            vp = get_vision_provider()
            if not vp or not getattr(vp, "is_available", lambda: True)():
                return []

            if not image_ref:
                # Capture an ephemeral screenshot
                from agent.executor import handle_screen_capture
                cap = handle_screen_capture({"target_device": device_id})
                if cap.get("status") == "success":
                    image_ref = (cap.get("data") or {}).get("image_ref") or cap.get("message", "").replace("Screenshot saved: ", "").strip()

            if not image_ref:
                return []

            obs = await vp.analyze_image(
                image_ref=image_ref,
                question="Identify all prominent actionable buttons, links, search bars, and labels on screen.",
                mode="locate",
                device_id=device_id,
            )

            start_idx = len(existing_elements) + 1
            for reg in getattr(obs, "regions", []):
                lbl = reg.get("label") or reg.get("name") or ""
                if not lbl:
                    continue
                # Skip duplicate if already identified by accessibility
                if any(e.label.lower() == lbl.lower() for e in existing_elements):
                    continue

                b = reg.get("bounds") or [0, 0, 100, 30]
                conf = float(reg.get("confidence", 0.85))
                el = UIElement(
                    element_id=f"el_vis_{start_idx}",
                    role="button" if "button" in lbl.lower() else "control",
                    label=lbl,
                    text=lbl,
                    bounds=b,
                    confidence=conf,
                    enabled=True,
                    metadata={"source": "vision"},
                )
                vision_elements.append(el)
                start_idx += 1

        except Exception as exc:
            log.debug(f"[UIObserver] Vision fallback perception skipped/failed: {exc}")

        return vision_elements

    async def observe(
        self,
        target_device: str = "asus",
        include_tree: bool = False,
        include_vision: bool = False,
        target_window_id: Optional[int] = None,
        mode: str = "full",
    ) -> UIObservation:
        """
        Produce a fresh, structured, versioned UI observation.
        Checks mock overrides first for hermetic testing.
        """
        if _OBSERVER_OVERRIDE and getattr(_OBSERVER_OVERRIDE, "observe", None):
            res = _OBSERVER_OVERRIDE.observe(
                target_device=target_device,
                include_tree=include_tree,
                include_vision=include_vision,
                target_window_id=target_window_id,
            )
            if isinstance(res, dict):
                res = UIObservation.from_dict(res)
            self._store_observation(res)
            return res

        t0 = time.monotonic()
        screen_size = self.get_screen_size()
        win_info = self._get_active_window_info(target_window_id)

        elements: List[UIElement] = []
        if include_tree:
            elements = self._extract_accessibility_elements(win_info.get("window_id"), screen_size)

        # If accessibility found very few elements or vision explicitly requested, use vision
        if include_vision and mode != "fast":
            vis_elements = await self._extract_vision_fallback_elements(
                image_ref=None,
                device_id=target_device,
                screen_size=screen_size,
                existing_elements=elements,
            )
            elements.extend(vis_elements)

        obs_id = f"obs_{uuid.uuid4().hex[:8]}"
        fg_app = win_info.get("foreground_app", "Desktop")
        checked = self._get_active_window_info(win_info.get("window_id"))
        identity_consistent = bool(win_info.get("window_id")) and all(
            checked.get(key) == win_info.get(key) for key in ("window_id", "pid", "foreground_app"))
        if not identity_consistent:
            elements = []  # A raced tree must never be uploaded or used for grounding.
        w_title = win_info.get("window_title", "")
        summary = f"Foreground: {fg_app} ('{w_title}') with {len(elements)} actionable UI elements."

        observation = UIObservation(
            observation_id=obs_id,
            device_id=target_device,
            timestamp=datetime.now(timezone.utc).isoformat(),
            screen_size=screen_size,
            foreground_app=fg_app,
            window_title=w_title,
            window_id=win_info.get("window_id"),
            pid=win_info.get("pid"), process_name=fg_app,
            window_bounds=win_info.get("window_bounds"),
            summary=summary,
            elements=elements,
            provenance={"identity_consistent":identity_consistent,"extracted_in_sec": round(time.monotonic() - t0, 3),
                "window_identity": {key: win_info[key] for key in ("pid", "host_pid", "host_app", "identity_source") if key in win_info}},
        )

        self._store_observation(observation)
        log.info(
            f"[UIObserver] Created observation {obs_id} for device '{target_device}' "
            f"app='{fg_app}' elements={len(elements)}"
        )
        return observation

    def _store_observation(self, obs: UIObservation) -> None:
        """Store observation in bounded ephemeral memory."""
        self._observations_by_id[obs.observation_id] = obs
        self._latest_by_device[obs.device_id] = obs
        self._history.append(obs)
        if len(self._history) > MAX_OBSERVATIONS_HISTORY:
            oldest = self._history.pop(0)
            self._observations_by_id.pop(oldest.observation_id, None)

    def get_observation(self, observation_id: str) -> Optional[UIObservation]:
        """Retrieve observation by version ID."""
        return self._observations_by_id.get(observation_id)

    def get_latest_observation(self, device_id: Optional[str] = None) -> Optional[UIObservation]:
        """Retrieve latest observation for device or global latest."""
        if device_id and device_id in self._latest_by_device:
            return self._latest_by_device[device_id]
        return self._history[-1] if self._history else None

    def is_observation_fresh(
        self,
        observation_id: str,
        target_window_id: Optional[int] = None,
        max_bounds_diff: int = 60,
        max_age_seconds: float = 30,
    ) -> Tuple[bool, str]:
        """
        Fencing validation: verifies that the UI state has not materially changed
        since the specified observation was captured.
        Returns: (is_fresh, reason)
        """
        obs = self.get_observation(observation_id)
        if not obs:
            return False, f"Observation '{observation_id}' not found in active observation cache."

        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(obs.timestamp)).total_seconds()
        except (TypeError, ValueError):
            return False, "Observation timestamp is invalid."
        if age < -2 or age > max_age_seconds:
            return False, "Observation expired; observe again."
        foreground = self._get_active_window_info()
        if obs.window_id and foreground.get("window_id") != obs.window_id:
            return False, "Foreground window identity changed; observe again."
        # Query the exact target as well: another window from the same app is not interchangeable.
        current_win = self._get_active_window_info(target_window_id or obs.window_id)
        if obs.window_id and current_win.get("window_id") != obs.window_id:
            return False, "Observed window disappeared or changed identity."
        expected_pid = obs.pid or obs.provenance.get("window_identity", {}).get("pid")
        if expected_pid and current_win.get("pid") != expected_pid:
            return False, "Observed process identity changed; observe again."

        # Check if foreground app or window title changed
        cur_app = (current_win.get("foreground_app") or "").lower()
        obs_app = (obs.foreground_app or "").lower()
        if cur_app != obs_app and obs_app not in ("desktop", "unknown"):
            return False, f"Foreground app changed from '{obs.foreground_app}' to '{current_win.get('foreground_app')}'."

        cur_title = (current_win.get("window_title") or "").strip().lower()
        obs_title = (obs.window_title or "").strip().lower()
        if obs_title and cur_title and cur_title != obs_title:
            # Check for substring match (e.g. dirty file title '*' in Notepad or tab change)
            if obs_title not in cur_title and cur_title not in obs_title:
                return False, f"Window title changed from '{obs.window_title}' to '{current_win.get('window_title')}'."

        # Check if window bounds moved significantly
        if obs.window_bounds and current_win.get("window_bounds"):
            ob_x, ob_y, ob_w, ob_h = obs.window_bounds
            cb_x, cb_y, cb_w, cb_h = current_win["window_bounds"]
            if (
                abs(ob_x - cb_x) > max_bounds_diff
                or abs(ob_y - cb_y) > max_bounds_diff
                or abs(ob_w - cb_w) > max_bounds_diff
                or abs(ob_h - cb_h) > max_bounds_diff
            ):
                return False, (
                    f"Target window moved/resized from {obs.window_bounds} "
                    f"to {current_win['window_bounds']}."
                )

        return True, "Observation is fresh and valid."


def observe_ui_sync(
    target_device: str = "asus",
    include_tree: bool = False,
    include_vision: bool = False,
    target_window_id: Optional[int] = None,
) -> UIObservation:
    """Convenience synchronous helper for observing UI."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run,
                UIObserver.get_instance().observe(
                    target_device=target_device,
                    include_tree=include_tree,
                    include_vision=include_vision,
                    target_window_id=target_window_id,
                )
            ).result()
    else:
        return asyncio.run(
            UIObserver.get_instance().observe(
                target_device=target_device,
                include_tree=include_tree,
                include_vision=include_vision,
                target_window_id=target_window_id,
            )
        )
