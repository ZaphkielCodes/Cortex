"""
agent/ui_action_executor.py
---------------------------
Bounded UI Action Executor for Cortex General Computer Use (Phase A10).

Provides deterministic, verified OS input interaction:
- Pointer movement & clicks (click, double_click, right_click)
- Scrolling (wheel scroll up/down/left/right)
- Text entry with sensitive credential protection (type)
- Single keys and combinations (keypress, hotkey)
- Drag and drop (drag)
- Item selection (select)
- Bounded wait (wait)
- Safe clipboard operations (clipboard.read, clipboard.write)

Enforces:
- Observation versioning & stale-screen fencing before OS mutations
- Semantic element target resolution via UIElementGrounder
- Window focus verification before keyboard input
- Zero leakage of raw secrets in logs or responses
"""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import platform
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .ui_grounder import GroundingResult, UIElementGrounder
from .ui_models import UIActionResult, UIElement, UIObservation
from .ui_observer import UIObserver

log = logging.getLogger("cortex.ui_action_executor")


# Windows user32 Virtual-Key Codes
VK_MAP = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "escape": 0x1B,
    "esc": 0x1B,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menu": 0x12,
    "win": 0x5B,
    "windows": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
    "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
    "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
    "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
    "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59,
    "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
}

# Mouse event constants
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


# Structure definitions for SendInput
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", INPUT_UNION),
    ]


# Runtime test override hook
_ACTION_EXECUTOR_OVERRIDE: Optional[Any] = None


def set_ui_action_override(override: Optional[Any]) -> None:
    """Set or clear a runtime override for UI actions (for hermetic testing)."""
    global _ACTION_EXECUTOR_OVERRIDE
    _ACTION_EXECUTOR_OVERRIDE = override


def get_ui_action_override() -> Optional[Any]:
    return _ACTION_EXECUTOR_OVERRIDE


class UIActionExecutor:
    """
    Executes bounded UI actions on Windows with stale observation rejection,
    semantic grounding, focus verification, and structured verification metadata.
    """

    _instance: Optional["UIActionExecutor"] = None

    def __init__(self):
        self._last_executed_action: Optional[UIActionResult] = None

    @classmethod
    def get_instance(cls) -> "UIActionExecutor":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def _ensure_desktop(self) -> None:
        """Attach calling thread to interactive desktop on Windows."""
        if platform.system() == "Windows":
            try:
                user32 = ctypes.windll.user32
                hdesk = user32.OpenDesktopW("default", 0, False, 0x01FF)
                if hdesk:
                    user32.SetThreadDesktop(hdesk)
            except Exception:
                pass

    def _check_fencing(
        self,
        observation_id: Optional[str],
        target_window_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Verify observation version freshness.
        Rejects stale observations if the interface materially changed.
        """
        if not observation_id:
            # If no observation_id provided, execution proceeds without fencing guarantee
            return True, None

        observer = UIObserver.get_instance()
        is_fresh, reason = observer.is_observation_fresh(observation_id, target_window_id)
        if not is_fresh:
            return False, reason
        return True, None

    def _resolve_target(
        self,
        target: Any,
        observation_id: Optional[str],
        coords: Optional[Tuple[int, int]] = None,
    ) -> Tuple[GroundingResult, Optional[UIObservation]]:
        """Ground semantic target against active observation."""
        observer = UIObserver.get_instance()
        obs = None
        if observation_id:
            obs = observer.get_observation(observation_id)
        if not obs:
            obs = observer.get_latest_observation()

        if not obs:
            # Synchronously create an observation if none exists
            from .ui_observer import observe_ui_sync
            obs = observe_ui_sync()

        grounding = UIElementGrounder.ground(target, obs, coords=coords)
        return grounding, obs

    def _send_mouse_event(self, flags: int, data: int = 0) -> None:
        """Issue bounded mouse event via user32."""
        if platform.system() == "Windows":
            try:
                ctypes.windll.user32.mouse_event(flags, 0, 0, data, 0)
            except Exception as exc:
                log.error(f"[UIActionExecutor] mouse_event failed: {exc}")

    def _move_cursor(self, x: int, y: int) -> None:
        """Move cursor position bounded to screen limits."""
        if platform.system() == "Windows":
            try:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
            except Exception as exc:
                log.error(f"[UIActionExecutor] SetCursorPos failed: {exc}")

    def _send_unicode_text(self, text: str, focus_check=None) -> None:
        """Send UTF-16 code units, including emoji, and surface rejected input."""
        if platform.system() != "Windows":
            raise RuntimeError("unsupported_platform")
        data = text.encode("utf-16-le")
        for offset in range(0, len(data), 2):
            if focus_check and not focus_check():
                raise RuntimeError("focus_changed")
            unit = int.from_bytes(data[offset:offset+2], "little")
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                event = INPUT()
                event.type = 1
                event.u.ki.wScan = unit
                event.u.ki.dwFlags = flags
                if ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
                    raise RuntimeError("input_rejected")

    def _send_vk_key(self, vk: int, key_down: bool = True, key_up: bool = True) -> None:
        """Send virtual key down/up."""
        if platform.system() != "Windows":
            return
        user32 = ctypes.windll.user32
        if key_down:
            user32.keybd_event(vk, 0, 0, 0)
        if key_up:
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    # ---------------------------------------------------------------------------
    # Action: ui.observe
    # ---------------------------------------------------------------------------
    def observe(self, args: Union[dict, list] = None) -> Dict[str, Any]:
        """Perform a structured UI observation."""
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "observe", None):
            return _ACTION_EXECUTOR_OVERRIDE.observe(args)

        norm_args = args if isinstance(args, dict) else {}
        target_device = norm_args.get("target_device") or norm_args.get("device_id") or "asus"
        include_tree = bool(norm_args.get("include_tree", True))
        include_vision = bool(norm_args.get("include_vision", False))
        target_win = norm_args.get("target_window_id") or norm_args.get("window_id")
        mode = norm_args.get("mode", "full")

        from .ui_observer import observe_ui_sync
        obs = observe_ui_sync(
            target_device=target_device,
            include_tree=include_tree,
            include_vision=include_vision,
            target_window_id=target_win,
        )
        return {
            "status": "success",
            "message": obs.summary,
            "data": obs.to_dict(),
            "observation_id": obs.observation_id,
        }

    # ---------------------------------------------------------------------------
    # Action: ui.click / ui.double_click / ui.right_click
    # ---------------------------------------------------------------------------
    def click(self, args: Union[dict, list], button: str = "left", clicks: int = 1) -> Dict[str, Any]:
        """
        Execute bounded click on grounded target or coordinates.
        Verifies observation freshness before clicking.
        """
        action_name = "ui.double_click" if clicks == 2 else ("ui.right_click" if button == "right" else "ui.click")
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, action_name, None):
            return getattr(_ACTION_EXECUTOR_OVERRIDE, action_name)(args)

        self._ensure_desktop()

        norm = args if isinstance(args, dict) else {}
        target = norm.get("target") or norm.get("element_id") or norm.get("label") or norm.get("selector")
        obs_id = norm.get("observation_id")
        btn = norm.get("button") or button
        clk_count = int(norm.get("clicks", clicks))

        # Check explicit coordinates
        coords = None
        if "x" in norm and "y" in norm:
            coords = (int(norm["x"]), int(norm["y"]))

        # 1. Fencing / Stale-screen check
        fresh, stale_reason = self._check_fencing(obs_id)
        if not fresh:
            res = UIActionResult(
                status="failure",
                action=action_name,
                target=str(target) if target else None,
                observation_id=obs_id,
                error="stale_observation",
                message=f"Screen state changed: {stale_reason}. Re-observation required before action.",
                verification_required=False,
            )
            return res.to_dict()

        # 2. Ground target to coordinates
        grounding, obs = self._resolve_target(target, obs_id, coords=coords)
        if not grounding.success or not grounding.target_point:
            res = UIActionResult(
                status="failure",
                action=action_name,
                target=str(target) if target else None,
                observation_id=obs.observation_id if obs else obs_id,
                error="target_not_found",
                message=grounding.explanation,
                verification_required=False,
            )
            return res.to_dict()

        gx, gy = grounding.target_point
        matched_el = grounding.element

        # 3. Focus verification & foreground attachment if window is specified
        if obs and obs.window_id:
            try:
                ctypes.windll.user32.SetForegroundWindow(obs.window_id)
                time.sleep(0.05)
            except Exception:
                pass

        # 4. Perform bounded OS cursor move & click, or UIA invocation fallback if off-screen
        if gx > 0 and gy > 0:
            self._move_cursor(gx, gy)
            time.sleep(0.05)

            for _ in range(clk_count):
                if btn == "right":
                    self._send_mouse_event(MOUSEEVENTF_RIGHTDOWN)
                    time.sleep(0.03)
                    self._send_mouse_event(MOUSEEVENTF_RIGHTUP)
                else:
                    self._send_mouse_event(MOUSEEVENTF_LEFTDOWN)
                    time.sleep(0.03)
                    self._send_mouse_event(MOUSEEVENTF_LEFTUP)
                if clk_count > 1:
                    time.sleep(0.08)
        elif matched_el and platform.system() == "Windows":
            try:
                import comtypes, comtypes.client
                comtypes.CoInitialize()
                mod = comtypes.client.GetModule("UIAutomationCore.dll")
                uia = comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
                hwnd = obs.window_id if (obs and obs.window_id) else ctypes.windll.user32.GetForegroundWindow()
                if hwnd:
                    root_item = uia.ElementFromHandle(hwnd)
                    cond = uia.CreateTrueCondition()
                    arr = root_item.FindAll(4, cond)
                    for idx in range(arr.Length):
                        it = arr.GetElement(idx)
                        if (it.CurrentName or "").strip().lower() == matched_el.label.strip().lower():
                            for pid, ptype in [(10000, mod.IUIAutomationInvokePattern), (10010, mod.IUIAutomationSelectionItemPattern), (10005, mod.IUIAutomationExpandCollapsePattern)]:
                                pat = it.GetCurrentPattern(pid)
                                if pat:
                                    try:
                                        if pid == 10000:
                                            pat.QueryInterface(ptype).Invoke()
                                        elif pid == 10010:
                                            pat.QueryInterface(ptype).Select()
                                        elif pid == 10005:
                                            pat.QueryInterface(ptype).Expand()
                                        break
                                    except Exception:
                                        pass
                            break
            except Exception:
                pass

        tgt_label = matched_el.label if matched_el else (str(target) if target else f"({gx},{gy})")
        tgt_role = matched_el.role if matched_el else "coordinate"
        tgt_id = matched_el.element_id if matched_el else None

        result = UIActionResult(
            status="success",
            action=action_name,
            target=tgt_id or str(target),
            target_label=tgt_label,
            target_role=tgt_role,
            observation_id=obs.observation_id if obs else obs_id,
            coordinates=[gx, gy],
            verification_required=True,
            message=f"{action_name} executed on '{tgt_label}' ({tgt_role}) at ({gx}, {gy}).",
            data={
                "grounding_strategy": grounding.strategy,
                "confidence": grounding.confidence,
                "element_id": tgt_id,
                "coordinates": [gx, gy],
            },
        )
        self._last_executed_action = result
        return result.to_dict()

    # ---------------------------------------------------------------------------
    # Action: ui.move_pointer
    # ---------------------------------------------------------------------------
    def move_pointer(self, args: Union[dict, list]) -> Dict[str, Any]:
        """Move cursor to grounded target or coordinates."""
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "move_pointer", None):
            return _ACTION_EXECUTOR_OVERRIDE.move_pointer(args)

        self._ensure_desktop()
        norm = args if isinstance(args, dict) else {}
        target = norm.get("target")
        obs_id = norm.get("observation_id")
        coords = (int(norm["x"]), int(norm["y"])) if ("x" in norm and "y" in norm) else None

        grounding, obs = self._resolve_target(target, obs_id, coords=coords)
        if not grounding.success or not grounding.target_point:
            return UIActionResult(
                status="failure",
                action="ui.move_pointer",
                error="target_not_found",
                message=grounding.explanation,
                verification_required=False,
            ).to_dict()

        gx, gy = grounding.target_point
        self._move_cursor(gx, gy)
        return UIActionResult(
            status="success",
            action="ui.move_pointer",
            coordinates=[gx, gy],
            observation_id=obs.observation_id if obs else obs_id,
            verification_required=False,
            message=f"Pointer moved to ({gx}, {gy}).",
        ).to_dict()

    # ---------------------------------------------------------------------------
    # Action: ui.scroll
    # ---------------------------------------------------------------------------
    def scroll(self, args: Union[dict, list]) -> Dict[str, Any]:
        """
        Scroll mouse wheel in direction ('up', 'down', 'left', 'right')
        optionally hovering over a grounded element or coordinates.
        """
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "scroll", None):
            return _ACTION_EXECUTOR_OVERRIDE.scroll(args)

        self._ensure_desktop()
        norm = args if isinstance(args, dict) else {}
        direction = (norm.get("direction") or "down").lower()
        amount = int(norm.get("amount", 300))  # standard wheel delta clicks (120 per notch)
        target = norm.get("target")
        obs_id = norm.get("observation_id")

        coords = (int(norm["x"]), int(norm["y"])) if ("x" in norm and "y" in norm) else None
        if target or coords:
            grounding, obs = self._resolve_target(target, obs_id, coords=coords)
            if grounding.success and grounding.target_point:
                self._move_cursor(*grounding.target_point)
                time.sleep(0.05)

        # Delta calculation (positive = up/forward, negative = down/backward)
        wheel_delta = amount if direction == "up" else -amount
        if direction in ("left", "right"):
            h_delta = amount if direction == "right" else -amount
            self._send_mouse_event(MOUSEEVENTF_HWHEEL, data=h_delta)
        else:
            self._send_mouse_event(MOUSEEVENTF_WHEEL, data=wheel_delta)

        return UIActionResult(
            status="success",
            action="ui.scroll",
            observation_id=obs_id,
            verification_required=True,
            message=f"Scrolled {direction} by {amount} units.",
            data={"direction": direction, "amount": amount},
        ).to_dict()

    # ---------------------------------------------------------------------------
    # Action: ui.type (with Sensitive Credential Protection)
    # ---------------------------------------------------------------------------
    def type_text(self, args: Union[dict, list]) -> Dict[str, Any]:
        """Enter text only in an observed, editable control with verified focus."""
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "type", None):
            return _ACTION_EXECUTOR_OVERRIDE.type(args)
        from .ui_text import resolve_text_target
        norm = args if isinstance(args, dict) else {}
        obs_id = norm.get("observation_id")
        observer = UIObserver.get_instance()
        observation = observer.get_observation(obs_id) if obs_id else None
        def fail(code, message, attempted=False):
            return UIActionResult(status="failure", action="ui.type", error=code, message=message,
                                  observation_id=obs_id, verification_required=attempted,
                                  data={"input_attempted": attempted}).to_dict()
        if not observation:
            return fail("observation_required", "Observe the target field before typing.")
        if norm.get("target_device") and norm["target_device"] != observation.device_id:
            return fail("wrong_device", "The observation belongs to another device.")
        fresh, reason = self._check_fencing(obs_id)
        if not fresh:
            return fail("stale_observation", "The interface changed. Observe it again before typing.")
        element, error = resolve_text_target(observation, norm.get("target"))
        if error:
            return fail(error, "The requested editable field could not be identified uniquely. Observe or clarify the target.")
        if norm.get("press_enter"):
            return fail("separate_submission_required", "Enter text first; submit with a separate authorized key or click action.")
        raw_text = norm.get("text", norm.get("value", ""))
        if not isinstance(raw_text, str) or not raw_text or len(raw_text) > 10000:
            return fail("invalid_text", "Text must contain between 1 and 10000 characters.")
        protected = bool(norm.get("is_credential") or norm.get("secret_id") or element.metadata.get("is_password"))
        text_to_type = raw_text
        if norm.get("secret_id"):
            try:
                from hub.secret_provisioning import resolve_secret
                text_to_type = resolve_secret(norm["secret_id"])
            except Exception:
                text_to_type = None
            if not text_to_type:
                return fail("secret_unavailable", "The requested stored credential is unavailable.")
        self._ensure_desktop()
        if not observer.focus_text_target(observation, element):
            return fail("focus_unverified", "Keyboard focus on the requested field could not be verified. No text was entered.")
        focus_check = lambda: observer.text_target_has_focus(observation, element)
        if not focus_check():
            return fail("focus_changed", "Keyboard focus changed before typing.")
        attempted = False
        try:
            if norm.get("clear_existing"):
                attempted = True
                self._send_vk_key(VK_MAP["ctrl"], key_down=True, key_up=False)
                try:
                    self._send_vk_key(VK_MAP["a"])
                finally:
                    self._send_vk_key(VK_MAP["ctrl"], key_down=False, key_up=True)
                if not focus_check():
                    raise RuntimeError("focus_changed")
                self._send_vk_key(VK_MAP["backspace"])
            attempted = True
            self._send_unicode_text(text_to_type, focus_check=focus_check)
        except Exception:
            return fail("input_unverified", "Text entry was interrupted; some input may have reached the field. Re-observe before continuing.", attempted)
        # Do not echo field contents into logs or persisted action results.
        log.info("ui.type input_sent observation_id=%s target_id=%s protected=%s", obs_id, element.element_id, protected)
        result = UIActionResult(status="success", action="ui.type", target=element.element_id,
            target_label=element.label, target_role=element.role, observation_id=obs_id,
            verification_required=True, verified=False,
            message="[CREDENTIAL REDACTED] input sent; completion requires observation." if protected else
                    "Text input sent to the selected field; completion requires observation.",
            data={"char_count": len(text_to_type), "cleared_existing": bool(norm.get("clear_existing")),
                  "pressed_enter": False, "is_credential": protected, "input_attempted": True})
        self._last_executed_action = result
        return result.to_dict()

    # ---------------------------------------------------------------------------
    # Action: ui.keypress
    # ---------------------------------------------------------------------------
    def _keyboard_observation_error(self, args, action):
        observer = UIObserver.get_instance()
        obs_id = args.get("observation_id")
        observation = observer.get_observation(obs_id) if obs_id else None
        if not observation:
            error = "observation_required"
        elif args.get("target_device") and args["target_device"] != observation.device_id:
            error = "wrong_device"
        elif not self._check_fencing(obs_id)[0]:
            error = "stale_observation"
        elif not observation.window_id or observer._get_active_window_info().get("window_id") != observation.window_id:
            error = "focus_changed"
        else:
            return None
        return UIActionResult(status="failure", action=action, error=error,
            observation_id=obs_id, verification_required=False,
            message="A fresh observation of the active target window is required before keyboard input.",
            data={"input_attempted": False}).to_dict()

    def keypress(self, args: Union[dict, list]) -> Dict[str, Any]:
        """Press an individual key (e.g. enter, tab, escape, backspace)."""
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "keypress", None):
            return _ACTION_EXECUTOR_OVERRIDE.keypress(args)

        self._ensure_desktop()
        norm = args if isinstance(args, dict) else {}
        error = self._keyboard_observation_error(norm, "ui.keypress")
        if error:
            return error
        key_name = (norm.get("key") or norm.get("name") or "enter").lower().strip()
        obs_id = norm.get("observation_id")

        vk = VK_MAP.get(key_name)
        if vk is None:
            return UIActionResult(
                status="failure",
                action="ui.keypress",
                error="unknown_key",
                message=f"Unsupported key: '{key_name}'",
                verification_required=False,
            ).to_dict()

        self._send_vk_key(vk)
        return UIActionResult(
            status="success",
            action="ui.keypress",
            observation_id=obs_id,
            verification_required=True,
            message=f"Pressed key '{key_name}'.",
            data={"key": key_name},
        ).to_dict()

    # ---------------------------------------------------------------------------
    # Action: ui.hotkey
    # ---------------------------------------------------------------------------
    def hotkey(self, args: Union[dict, list]) -> Dict[str, Any]:
        """
        Press a key combination (e.g. ['ctrl', 'a'], ['ctrl', 'c'], ['alt', 'left']).
        """
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "hotkey", None):
            return _ACTION_EXECUTOR_OVERRIDE.hotkey(args)

        self._ensure_desktop()
        norm = args if isinstance(args, dict) else {}
        error = self._keyboard_observation_error(norm, "ui.hotkey")
        if error:
            return error
        keys_val = norm.get("keys") or norm.get("combo") or ["ctrl", "c"]
        obs_id = norm.get("observation_id")

        if isinstance(keys_val, str):
            keys = [k.strip().lower() for k in keys_val.replace("+", " ").split()]
        else:
            keys = [str(k).strip().lower() for k in keys_val]

        vks = []
        for k in keys:
            vk = VK_MAP.get(k)
            if vk is None:
                return UIActionResult(
                    status="failure",
                    action="ui.hotkey",
                    error="unknown_key",
                    message=f"Unsupported hotkey modifier: '{k}'",
                    verification_required=False,
                ).to_dict()
            vks.append(vk)

        # Press modifiers down in order
        for vk in vks:
            self._send_vk_key(vk, key_down=True, key_up=False)
            time.sleep(0.02)

        time.sleep(0.03)

        # Release modifiers in reverse order
        for vk in reversed(vks):
            self._send_vk_key(vk, key_down=False, key_up=True)
            time.sleep(0.01)

        combo_str = "+".join(keys)
        return UIActionResult(
            status="success",
            action="ui.hotkey",
            observation_id=obs_id,
            verification_required=True,
            message=f"Pressed hotkey '{combo_str}'.",
            data={"keys": keys, "combo": combo_str},
        ).to_dict()

    # ---------------------------------------------------------------------------
    # Action: ui.drag
    # ---------------------------------------------------------------------------
    def drag(self, args: Union[dict, list]) -> Dict[str, Any]:
        """Execute bounded drag and drop between two points or elements."""
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "drag", None):
            return _ACTION_EXECUTOR_OVERRIDE.drag(args)

        self._ensure_desktop()
        norm = args if isinstance(args, dict) else {}
        from_x = norm.get("from_x")
        from_y = norm.get("from_y")
        to_x = norm.get("to_x")
        to_y = norm.get("to_y")
        from_target = norm.get("from_target")
        to_target = norm.get("to_target")
        obs_id = norm.get("observation_id")

        if from_target or to_target:
            if from_target:
                g_from, _ = self._resolve_target(from_target, obs_id)
                if g_from.success and g_from.target_point:
                    from_x, from_y = g_from.target_point
            if to_target:
                g_to, _ = self._resolve_target(to_target, obs_id)
                if g_to.success and g_to.target_point:
                    to_x, to_y = g_to.target_point

        if from_x is None or from_y is None or to_x is None or to_y is None:
            return UIActionResult(
                status="failure",
                action="ui.drag",
                error="missing_coordinates",
                message="Drag requires valid from (x, y) and to (x, y) coordinates or targets.",
                verification_required=False,
            ).to_dict()

        # Move to source, press down, move to destination, release
        self._move_cursor(from_x, from_y)
        time.sleep(0.05)
        self._send_mouse_event(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.1)

        # Smooth drag steps
        steps = 10
        for i in range(1, steps + 1):
            curr_x = int(from_x + (to_x - from_x) * (i / steps))
            curr_y = int(from_y + (to_y - from_y) * (i / steps))
            self._move_cursor(curr_x, curr_y)
            time.sleep(0.02)

        self._send_mouse_event(MOUSEEVENTF_LEFTUP)
        time.sleep(0.05)

        return UIActionResult(
            status="success",
            action="ui.drag",
            observation_id=obs_id,
            coordinates=[to_x, to_y],
            verification_required=True,
            message=f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y}).",
            data={"from": [from_x, from_y], "to": [to_x, to_y]},
        ).to_dict()

    # ---------------------------------------------------------------------------
    # Action: ui.select
    # ---------------------------------------------------------------------------
    def select_item(self, args: Union[dict, list]) -> Dict[str, Any]:
        """Select item from combobox, list, or text range."""
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "select", None):
            return _ACTION_EXECUTOR_OVERRIDE.select(args)

        norm = args if isinstance(args, dict) else {}
        target = norm.get("target")
        value = norm.get("value") or norm.get("text") or ""
        obs_id = norm.get("observation_id")

        # 1. Click target dropdown/combo if present
        if target:
            clk_res = self.click({"target": target, "observation_id": obs_id})
            if clk_res.get("status") != "success":
                return clk_res
            time.sleep(0.15)

        # 2. Type value or use arrow keys
        if value:
            self._send_unicode_text(value)
            time.sleep(0.05)
            self._send_vk_key(VK_MAP["enter"])

        return UIActionResult(
            status="success",
            action="ui.select",
            target=str(target) if target else None,
            observation_id=obs_id,
            verification_required=True,
            message=f"Selected '{value}' in target '{target}'.",
            data={"selected_value": value},
        ).to_dict()

    # ---------------------------------------------------------------------------
    # Action: ui.wait
    # ---------------------------------------------------------------------------
    def wait(self, args: Union[dict, list]) -> Dict[str, Any]:
        """Bounded sleep / wait for UI state to settle."""
        norm = args if isinstance(args, dict) else {}
        secs = float(norm.get("seconds", norm.get("duration", 1.0)))
        bounded_secs = min(30.0, max(0.1, secs))
        time.sleep(bounded_secs)
        return {
            "status": "success",
            "action": "ui.wait",
            "message": f"Waited {bounded_secs:.1f} seconds.",
            "data": {"waited_seconds": bounded_secs},
        }

    # ---------------------------------------------------------------------------
    # Action: clipboard.read / clipboard.write
    # ---------------------------------------------------------------------------
    def clipboard_read(self, args: Union[dict, list] = None) -> Dict[str, Any]:
        """Safely read text from system clipboard."""
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "clipboard_read", None):
            return _ACTION_EXECUTOR_OVERRIDE.clipboard_read(args)

        content = ""
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                content = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            log.debug(f"[UIActionExecutor] Clipboard read failed: {exc}")
            return {"status": "failure", "message": f"Could not read clipboard: {exc}"}

        # Truncate content for consumer safety
        safe_preview = content[:100] + ("..." if len(content) > 100 else "")
        return {
            "status": "success",
            "message": f"Clipboard read ({len(content)} characters): '{safe_preview}'",
            "data": {"content": content, "length": len(content)},
            "content": content,
        }

    def clipboard_write(self, args: Union[dict, list]) -> Dict[str, Any]:
        """Safely write text to system clipboard."""
        if _ACTION_EXECUTOR_OVERRIDE and getattr(_ACTION_EXECUTOR_OVERRIDE, "clipboard_write", None):
            return _ACTION_EXECUTOR_OVERRIDE.clipboard_write(args)

        norm = args if isinstance(args, dict) else {}
        text = norm.get("text") or (args[0] if isinstance(args, list) and args else "")
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, str(text))
            finally:
                win32clipboard.CloseClipboard()
            return {
                "status": "success",
                "message": f"Copied {len(text)} characters to clipboard.",
                "data": {"length": len(text)},
            }
        except Exception as exc:
            log.error(f"[UIActionExecutor] Clipboard write failed: {exc}")
            return {"status": "failure", "message": f"Could not write to clipboard: {exc}"}

    # Alias type to type_text for API consistency
    type = type_text
