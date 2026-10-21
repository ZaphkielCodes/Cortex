"""
agent/executor.py
-----------------
Pure command execution functions for the Cortex agent.

Each function:
  - receives a list of string arguments
  - returns a dict {"status": "success"|"failure", "message": str}
  - NEVER raises — all exceptions are caught and returned as failures

Supported commands
------------------
  take_screenshot           — captures screen, saves PNG locally, returns path
  open_app <app_name>       — opens an application by name
  move_file <src> <dst>     — moves a file from src to dst
"""

import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# ---------------------------------------------------------------------------
# Type alias for a result dict
# ---------------------------------------------------------------------------
Result = dict  # {"status": "success"|"failure", "message": str}

# ---------------------------------------------------------------------------
# Screenshot output directory (created next to this script)
# ---------------------------------------------------------------------------
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# screen.capture & take_screenshot
# ---------------------------------------------------------------------------
def handle_screen_capture(args: Union[list, dict]) -> Result:
    """
    Capture the screen and save it as a timestamped PNG in ./screenshots/.
    Uses the real OS screen capture path (pyautogui or PIL ImageGrab).
    Supports display_index, region (bounded bounding box), and bounded file retention.
    """
    try:
        from datetime import timezone
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
        filename = SCREENSHOT_DIR / f"screenshot_{timestamp}.png"

        # Parse optional inputs (supports both list and dict args)
        target_device = "this_device"
        display_index = 0
        region = None

        if isinstance(args, dict):
            target_device = args.get("target_device") or args.get("device_id") or target_device
            display_index = int(args.get("display_index", 0)) if args.get("display_index") is not None else 0
            region = args.get("region")
        elif isinstance(args, list) and args:
            # If JSON passed as first arg
            if len(args) == 1 and args[0].startswith("{"):
                try:
                    import json
                    parsed = json.loads(args[0])
                    target_device = parsed.get("target_device", target_device)
                    display_index = int(parsed.get("display_index", 0))
                    region = parsed.get("region")
                except Exception:
                    pass
            elif len(args) >= 1:
                target_device = args[0]
                if len(args) >= 2:
                    try:
                        display_index = int(args[1])
                    except (ValueError, TypeError):
                        pass

        bbox = None
        if region:
            if isinstance(region, dict):
                rx = int(region.get("x", 0))
                ry = int(region.get("y", 0))
                rw = int(region.get("width", 0))
                rh = int(region.get("height", 0))
                if rw > 0 and rh > 0:
                    bbox = (rx, ry, rx + rw, ry + rh)
            elif isinstance(region, (list, tuple)) and len(region) == 4:
                rx, ry, rw, rh = [int(v) for v in region]
                if rw > 0 and rh > 0:
                    bbox = (rx, ry, rx + rw, ry + rh)

        # On Windows: ensure thread desktop is set to 'default' for non-interactive / service processes
        if platform.system() == "Windows":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                hdesk_default = user32.OpenDesktopW("default", 0, False, 0x01FF)
                if hdesk_default:
                    user32.SetThreadDesktop(hdesk_default)
            except Exception:
                pass

        img = None
        last_exc = None
        try:
            from PIL import ImageGrab
            if bbox:
                img = ImageGrab.grab(bbox=bbox)
            else:
                img = ImageGrab.grab()
        except Exception as exc:
            last_exc = exc
            try:
                import pyautogui
                if bbox:
                    rw = bbox[2] - bbox[0]
                    rh = bbox[3] - bbox[1]
                    img = pyautogui.screenshot(region=(bbox[0], bbox[1], rw, rh))
                else:
                    img = pyautogui.screenshot()
            except Exception as e2:
                last_exc = e2

        if img is None:
            err_msg = f"Failed to capture screen image: {last_exc}" if last_exc else "Failed to capture screen image"
            return {"status": "failure", "message": err_msg}

        img.save(str(filename))
        w, h = 1920, 1080
        try:
            if hasattr(img, "size") and img.size:
                size_tuple = tuple(img.size)
                if len(size_tuple) >= 2:
                    w, h = int(size_tuple[0]), int(size_tuple[1])
        except Exception:
            pass
        captured_at_iso = datetime.now(timezone.utc).isoformat()

        # Bounded retention: cleanup old screenshots exceeding retention window
        try:
            from hub.screen_perception import ScreenPerceptionManager
            ScreenPerceptionManager.cleanup_ephemeral_screenshots(SCREENSHOT_DIR)
        except Exception:
            pass

        return {
            "status": "success",
            "message": f"Screenshot saved: {filename}",
            "data": {
                "image_ref": str(filename),
                "width": w,
                "height": h,
                "display_index": display_index,
                "captured_at": captured_at_iso,
                "source_device": target_device,
            },
        }
    except Exception as exc:
        return {"status": "failure", "message": f"screen.capture failed: {exc}"}


# Backward compatibility alias
handle_take_screenshot = handle_screen_capture


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# app and window management handlers (Phase A3)
# ---------------------------------------------------------------------------
def handle_app_list_running(args: Union[list, dict] = None) -> Result:
    try:
        from .desktop_actions import list_running_apps
        return list_running_apps(args)
    except Exception as exc:
        return {"status": "failure", "message": f"app.list_running failed: {exc}"}


def handle_process_list(args: Union[list, dict] = None) -> Result:
    try:
        from .desktop_actions import list_processes
        return list_processes(args)
    except Exception as exc:
        return {"status": "failure", "message": f"process.list failed: {exc}"}



def handle_window_list(args: Union[list, dict] = None) -> Result:
    try:
        from .desktop_actions import list_windows
        return list_windows(args)
    except Exception as exc:
        return {"status": "failure", "message": f"window.list failed: {exc}"}


def handle_window_focus(args: Union[list, dict]) -> Result:
    try:
        from .desktop_actions import focus_window
        return focus_window(args)
    except Exception as exc:
        return {"status": "failure", "message": f"window.focus failed: {exc}"}


def handle_window_minimize(args: Union[list, dict]) -> Result:
    try:
        from .desktop_actions import minimize_window
        return minimize_window(args)
    except Exception as exc:
        return {"status": "failure", "message": f"window.minimize failed: {exc}"}


def handle_window_maximize(args: Union[list, dict]) -> Result:
    try:
        from .desktop_actions import maximize_window
        return maximize_window(args)
    except Exception as exc:
        return {"status": "failure", "message": f"window.maximize failed: {exc}"}


def handle_window_restore(args: Union[list, dict]) -> Result:
    try:
        from .desktop_actions import restore_window
        return restore_window(args)
    except Exception as exc:
        return {"status": "failure", "message": f"window.restore failed: {exc}"}


def handle_app_close(args: Union[list, dict]) -> Result:
    try:
        from .desktop_actions import close_app
        return close_app(args)
    except Exception as exc:
        return {"status": "failure", "message": f"app.close failed: {exc}"}


def handle_app_launch(args: Union[list, dict]) -> Result:
    try:
        from .desktop_actions import launch_app
        return launch_app(args)
    except Exception as exc:
        return {"status": "failure", "message": f"app.launch failed: {exc}"}


def handle_open_app(args: Union[list, dict]) -> Result:
    return handle_app_launch(args)


# ---------------------------------------------------------------------------
# fs.open, fs.copy, fs.move, fs.rename (Phase A3)
# ---------------------------------------------------------------------------
def handle_fs_open(args: Union[list, dict]) -> Result:
    try:
        from hub.environment import fs_open
        path = args.get("path", "") if isinstance(args, dict) else (args[0] if args else "")
        return fs_open(path)
    except Exception as exc:
        return {"status": "failure", "message": f"fs.open failed: {exc}"}


def handle_fs_copy(args: Union[list, dict]) -> Result:
    try:
        from hub.environment import fs_copy
        if isinstance(args, dict):
            src = args.get("src") or args.get("source") or ""
            dst = args.get("dst") or args.get("destination") or ""
            overwrite = bool(args.get("overwrite", False))
        else:
            if not args or len(args) < 2:
                return {"status": "failure", "message": "fs.copy requires src and dst arguments"}
            src = args[0]
            dst = args[1]
            overwrite = len(args) > 2 and str(args[2]).lower() in ("true", "1", "overwrite")
        return fs_copy(src=src, dst=dst, overwrite=overwrite)
    except Exception as exc:
        return {"status": "failure", "message": f"fs.copy failed: {exc}"}


def handle_fs_move(args: Union[list, dict]) -> Result:
    try:
        from hub.environment import fs_move
        if isinstance(args, dict):
            src = args.get("src") or args.get("source") or ""
            dst = args.get("dst") or args.get("destination") or ""
            overwrite = bool(args.get("overwrite", False))
        else:
            if not args or len(args) < 2:
                return {"status": "failure", "message": "fs.move requires src and dst arguments"}
            src = args[0]
            dst = args[1]
            overwrite = len(args) > 2 and str(args[2]).lower() in ("true", "1", "overwrite")
        return fs_move(src=src, dst=dst, overwrite=overwrite)
    except Exception as exc:
        return {"status": "failure", "message": f"fs.move failed: {exc}"}


def handle_move_file(args: Union[list, dict]) -> Result:
    return handle_fs_move(args)


def handle_fs_rename(args: Union[list, dict]) -> Result:
    try:
        from hub.environment import fs_rename
        if isinstance(args, dict):
            src = args.get("src") or args.get("source") or ""
            new_name = args.get("new_name") or args.get("dst") or ""
            overwrite = bool(args.get("overwrite", False))
        else:
            if not args or len(args) < 2:
                return {"status": "failure", "message": "fs.rename requires src and new_name arguments"}
            src = args[0]
            new_name = args[1]
            overwrite = len(args) > 2 and str(args[2]).lower() in ("true", "1", "overwrite")
        return fs_rename(src=src, new_name=new_name, overwrite=overwrite)
    except Exception as exc:
        return {"status": "failure", "message": f"fs.rename failed: {exc}"}


# ---------------------------------------------------------------------------
# show_notification
# ---------------------------------------------------------------------------
def handle_show_notification(args: Union[list, dict]) -> Result:
    """
    Display a notification or popup on the target device.
    Supports canonical dict {"title": ..., "message": ...} or positional list [title, message].
    """
    if isinstance(args, dict):
        title = args.get("title") or "Cortex Notification"
        message = args.get("message") or ""
    elif isinstance(args, list):
        if len(args) < 2:
            return {"status": "failure", "message": "show_notification requires title and message"}
        title = args[0]
        message = args[1]
    else:
        return {"status": "failure", "message": "show_notification requires title and message"}
    
    system = platform.system()

    try:
        if system == "Windows":
            # Simple powershell popup script
            clean_msg = message.replace('"', '`"').replace("'", "''")
            clean_title = title.replace('"', '`"').replace("'", "''")
            ps_script = f'''
            Add-Type -AssemblyName PresentationFramework
            [System.Windows.MessageBox]::Show("{clean_msg}", "{clean_title}")
            '''
            subprocess.Popen(["powershell", "-Command", ps_script], creationflags=subprocess.CREATE_NO_WINDOW)
        elif system == "Darwin":
            # macOS notification
            script = f'display notification "{message}" with title "{title}"'
            subprocess.Popen(["osascript", "-e", script])
        else:
            # Linux notify-send
            subprocess.Popen(["notify-send", title, message])
            
        return {"status": "success", "message": "Notification triggered"}
    except Exception as exc:
        return {"status": "failure", "message": f"show_notification failed: {exc}"}


# ---------------------------------------------------------------------------
# get_device_status
# ---------------------------------------------------------------------------
def handle_get_device_status(args: list[str]) -> Result:
    """
    Get current device telemetry like battery percentage.
    """
    try:
        import psutil
        battery = psutil.sensors_battery()
        if battery:
            percent = int(battery.percent)
            plugged = "plugged in" if battery.power_plugged else "on battery"
            message = f"Battery is at {percent}% and is {plugged}."
        else:
            message = "Battery status is unavailable."
        return {"status": "success", "message": message}
    except Exception as exc:
        return {"status": "failure", "message": f"get_device_status failed: {exc}"}

# ---------------------------------------------------------------------------
# fs.list, fs.stat, fs.delete (Phase 9G)
# ---------------------------------------------------------------------------
def handle_fs_list(args: Union[list, dict]) -> Result:
    """
    List direct children of a directory with bounded output and metadata.
    Supports canonical dict or positional list args.
    """
    try:
        from hub.environment import fs_list
        if isinstance(args, dict):
            path = args.get("path") or "desktop"
            max_results = int(args.get("max_results", 50))
            include_hidden = bool(args.get("include_hidden", False))
        else:
            path = args[0] if args else "desktop"
            max_results = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 50
            include_hidden = str(args[2]).lower() in ("true", "1") if len(args) > 2 else False
        res = fs_list(path=path, max_results=max_results, include_hidden=include_hidden)
        return {
            "status": res["status"],
            "message": res["message"],
            "data": {
                "items": res.get("items", []),
                "count": res.get("count", 0),
                "total_count": res.get("total_count", 0),
                "truncated": res.get("truncated", False),
                "path": res.get("path", ""),
            },
            "count": res.get("count", 0),
            "total_count": res.get("total_count", 0),
            "path": res.get("path", ""),
        }
    except Exception as exc:
        return {"status": "failure", "message": f"fs.list failed: {exc}"}


def handle_fs_stat(args: Union[list, dict]) -> Result:
    """
    Inspect metadata of a specific file or directory.
    """
    try:
        from hub.environment import fs_stat
        path = args.get("path", "") if isinstance(args, dict) else (args[0] if args else "")
        if not path:
            return {"status": "failure", "message": "fs.stat requires path argument"}
        res = fs_stat(path=path)
        return {
            "status": res["status"],
            "message": res["message"],
            "data": res,
            "exists": res.get("exists", False),
        }
    except Exception as exc:
        return {"status": "failure", "message": f"fs.stat failed: {exc}"}


def handle_fs_delete(args: Union[list, dict]) -> Result:
    """
    Permanently delete a file or directory.
    Tier 3 destructive command.
    """
    try:
        from hub.environment import fs_delete
        path = args.get("path", "") if isinstance(args, dict) else (args[0] if args else "")
        if not path:
            return {"status": "failure", "message": "fs.delete requires path argument"}
        res = fs_delete(path=path)
        return {
            "status": res["status"],
            "message": res["message"],
            "data": res,
        }
    except Exception as exc:
        return {"status": "failure", "message": f"fs.delete failed: {exc}"}


def handle_fs_shortcut_info(args: Union[list, dict]) -> Result:
    """
    Inspect metadata of a Windows shortcut (.lnk).
    """
    try:
        from hub.environment import fs_shortcut_info
        path = args.get("path", "") if isinstance(args, dict) else (args[0] if args else "")
        if not path:
            return {"status": "failure", "message": "fs.shortcut_info requires path argument"}
        res = fs_shortcut_info(path=path)
        return {
            "status": res["status"],
            "message": res.get("message", "Shortcut inspected"),
            "data": res,
            "exists": res.get("exists", False),
        }
    except Exception as exc:
        return {"status": "failure", "message": f"fs.shortcut_info failed: {exc}"}


# ---------------------------------------------------------------------------
# browser.open_url (Phase A1)
# ---------------------------------------------------------------------------
def handle_browser_open_url(args: Union[list, dict]) -> Result:
    """
    Open a URL in the default web browser.
    """
    url = ""
    if isinstance(args, dict):
        url = args.get("url") or args.get("link") or args.get("target") or ""
    elif isinstance(args, list) and args:
        url = str(args[0]).strip()
    elif isinstance(args, str):
        url = args.strip()

    url = url.strip("?.,! \t\n")
    if not url:
        return {"status": "failure", "message": "browser.open_url: no URL provided"}

    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"

    try:
        import webbrowser
        opened = webbrowser.open(url)
        return {
            "status": "success",
            "message": f"Opened {url} in browser",
            "url": url,
            "opened": opened,
        }
    except Exception as exc:
        return {"status": "failure", "message": f"browser.open_url failed: {exc}"}


# ---------------------------------------------------------------------------
# Phase A4: System Registry & Self-Knowledge Handlers
# ---------------------------------------------------------------------------
def _run_coroutine_sync(coro):
    """Safely execute an async coroutine from synchronous executor context."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except Exception:
        return asyncio.run(coro)


def handle_system_identity(args: Union[list, dict] = None) -> Result:
    from hub.system_registry import SystemRegistry
    cur_node = args.get("current_node") if isinstance(args, dict) else (args[0] if args else None)
    ident = SystemRegistry.get_instance().get_identity(current_node=cur_node)
    return {
        "status": "success",
        "message": f"{ident['product_name']} v{ident['version']} ({ident['architecture_mode']} mode, node: {ident['current_node']})",
        "data": ident,
    }


def handle_system_models(args: Union[list, dict] = None) -> Result:
    from hub.system_registry import SystemRegistry
    models = SystemRegistry.get_instance().get_models()
    return {
        "status": "success",
        "message": f"Active models: {len(models)} introspected.",
        "data": models,
    }


def handle_system_list_capabilities(args: Union[list, dict] = None) -> Result:
    from hub.system_registry import SystemRegistry
    caps = SystemRegistry.get_instance().list_capabilities()
    return {
        "status": "success",
        "message": f"{len(caps)} capabilities registered.",
        "data": caps,
    }


def handle_system_capability_status(args: Union[list, dict]) -> Result:
    from hub.system_registry import SystemRegistry
    if isinstance(args, dict):
        raw = args.get("capabilities") or args.get("capability") or []
        cap_names = raw if isinstance(raw, list) else [c.strip() for c in str(raw).split(",") if c.strip()]
    elif isinstance(args, list) and args:
        cap_names = [c.strip() for c in str(args[0]).split(",") if c.strip()]
    else:
        cap_names = []
    stat = SystemRegistry.get_instance().get_capability_status(cap_names)
    return {
        "status": "success",
        "message": "All available." if stat.get("all_available") else "Some unavailable.",
        "data": stat,
    }


def handle_system_list_devices(args: Union[list, dict] = None) -> Result:
    from hub.system_registry import SystemRegistry
    devices = _run_coroutine_sync(SystemRegistry.get_instance().list_devices())
    return {
        "status": "success",
        "message": f"{len(devices)} devices in registry.",
        "data": devices,
    }


def handle_system_device_status(args: Union[list, dict]) -> Result:
    from hub.system_registry import SystemRegistry
    d_id = args.get("device_id") if isinstance(args, dict) else (args[0] if args else "asus")
    d_id = d_id or "asus"
    stat = _run_coroutine_sync(SystemRegistry.get_instance().get_device_status(d_id))
    if not stat:
        return {"status": "failure", "message": f"Device '{d_id}' not found."}
    return {
        "status": "success",
        "message": f"Device {d_id} status retrieved.",
        "data": stat,
    }


def handle_system_list_integrations(args: Union[list, dict] = None) -> Result:
    from hub.system_registry import SystemRegistry
    integs = _run_coroutine_sync(SystemRegistry.get_instance().list_integrations())
    return {
        "status": "success",
        "message": f"{len(integs)} integrations introspected.",
        "data": integs,
    }


def handle_system_integration_status(args: Union[list, dict]) -> Result:
    from hub.system_registry import SystemRegistry
    key = args.get("integration") or args.get("key") if isinstance(args, dict) else (args[0] if args else "")
    key = key or ""
    stat = _run_coroutine_sync(SystemRegistry.get_instance().get_integration_status(key))
    return {
        "status": "success",
        "message": f"Integration '{key}' status: {stat.get('status')}.",
        "data": stat,
    }


def handle_system_providers(args: Union[list, dict] = None) -> Result:
    from hub.system_registry import SystemRegistry
    provs = SystemRegistry.get_instance().get_providers()
    return {
        "status": "success",
        "message": f"{len(provs)} providers reported.",
        "data": provs,
    }


def handle_system_explain_capability(args: Union[list, dict]) -> Result:
    from hub.system_registry import SystemRegistry
    q = args.get("capability") or args.get("query") if isinstance(args, dict) else (args[0] if args else "")
    q = q or ""
    res = _run_coroutine_sync(SystemRegistry.get_instance().explain_capability(q))
    return {
        "status": "success",
        "message": res.get("explanation", ""),
        "data": res,
    }


# ---------------------------------------------------------------------------
# Phase A10: General Computer Use Handlers
# ---------------------------------------------------------------------------
def handle_ui_observe(args: Union[list, dict] = None) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().observe(args)


def handle_ui_click(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().click(args)


def handle_ui_double_click(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().click(args, clicks=2)


def handle_ui_right_click(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().click(args, button="right")


def handle_ui_move_pointer(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().move_pointer(args)


def handle_ui_scroll(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().scroll(args)


def handle_ui_type(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().type_text(args)


def handle_ui_keypress(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().keypress(args)


def handle_ui_hotkey(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().hotkey(args)


def handle_ui_drag(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().drag(args)


def handle_ui_select(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().select_item(args)


def handle_ui_wait(args: Union[list, dict] = None) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().wait(args)


def handle_clipboard_read(args: Union[list, dict] = None) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().clipboard_read(args)


def handle_clipboard_write(args: Union[list, dict]) -> Result:
    from agent.ui_action_executor import UIActionExecutor
    return UIActionExecutor.get_instance().clipboard_write(args)


# ---------------------------------------------------------------------------
# Dispatch table & Capability Detection
# ---------------------------------------------------------------------------
HANDLERS = {
    # General Computer Use (Phase A10)
    "ui.observe": handle_ui_observe,
    "ui.click": handle_ui_click,
    "ui.double_click": handle_ui_double_click,
    "ui.right_click": handle_ui_right_click,
    "ui.move_pointer": handle_ui_move_pointer,
    "ui.scroll": handle_ui_scroll,
    "ui.type": handle_ui_type,
    "ui.keypress": handle_ui_keypress,
    "ui.hotkey": handle_ui_hotkey,
    "ui.drag": handle_ui_drag,
    "ui.select": handle_ui_select,
    "ui.wait": handle_ui_wait,
    "clipboard.read": handle_clipboard_read,
    "clipboard.write": handle_clipboard_write,
    # Screen perception
    "screen.capture": handle_screen_capture,
    "take_screenshot": handle_screen_capture,
    "screenshot.read": handle_screen_capture,
    "ui.screenshot": handle_screen_capture,
    "intent_screenshot": handle_screen_capture,

    # App inspection & lifecycle (Phase A3)
    "app.list_running": handle_app_list_running,
    "list_running_apps": handle_app_list_running,
    "process.list": handle_process_list,
    "list_processes": handle_process_list,
    "app.launch": handle_app_launch,
    "open_app": handle_app_launch,
    "app.close": handle_app_close,
    "close_app": handle_app_close,
    "kill_app": handle_app_close,

    # Window inspection & focus / state (Phase A3)
    "window.list": handle_window_list,
    "list_windows": handle_window_list,
    "window.focus": handle_window_focus,
    "focus_window": handle_window_focus,
    "switch_window": handle_window_focus,
    "window.minimize": handle_window_minimize,
    "window.maximize": handle_window_maximize,
    "window.restore": handle_window_restore,

    # Filesystem operations (Phase A3)
    "fs.open": handle_fs_open,
    "open_file": handle_fs_open,
    "open_folder": handle_fs_open,
    "fs.copy": handle_fs_copy,
    "copy_file": handle_fs_copy,
    "fs.move": handle_fs_move,
    "move_file": handle_fs_move,
    "fs.rename": handle_fs_rename,
    "rename_file": handle_fs_rename,
    "fs.delete": handle_fs_delete,
    "delete_file": handle_fs_delete,
    "fs.list": handle_fs_list,
    "fs.stat": handle_fs_stat,
    "fs.shortcut_info": handle_fs_shortcut_info,

    # Browser & system actions
    "browser.open_url": handle_browser_open_url,
    "open_url": handle_browser_open_url,
    "show_notification": handle_show_notification,
    "ui.notify": handle_show_notification,
    "get_status": handle_get_device_status,
    "device.status": handle_get_device_status,
    "device.info": handle_get_device_status,

    # Phase A4: System Registry & Self-Knowledge
    "system.identity": handle_system_identity,
    "system.models": handle_system_models,
    "system.list_capabilities": handle_system_list_capabilities,
    "system.capability_status": handle_system_capability_status,
    "system.list_devices": handle_system_list_devices,
    "system.device_status": handle_system_device_status,
    "system.list_integrations": handle_system_list_integrations,
    "system.integration_status": handle_system_integration_status,
    "system.providers": handle_system_providers,
    "system.explain_capability": handle_system_explain_capability,

    # Backward-compatibility alias
    "retrieve_recent_screenshot": handle_screen_capture,
}


def normalize_command_arguments(command: str, args: Any) -> Dict[str, Any]:
    """
    Establish one canonical command argument representation (dict[str, Any]).
    Normalizes legacy serialized JSON lists or positional argument lists exactly once
    at the execution boundary. Strips trailing conversational punctuation from targets.
    """
    if args is None:
        return {}

    # Case 1: Already a dict
    if isinstance(args, dict):
        norm = dict(args)
        # Strip trailing punctuation from common entity fields
        for k in ("app", "process", "target", "title", "url", "path", "src", "dst", "new_name"):
            if k in norm and isinstance(norm[k], str):
                norm[k] = norm[k].strip("?.,! \t\n")
        return norm

    # Case 2: List (either serialized JSON or positional args)
    if isinstance(args, list):
        if not args:
            return {}
        # Legacy serialized JSON in single string element
        if len(args) == 1 and isinstance(args[0], str) and args[0].strip().startswith(("{", "[")):
            try:
                import json
                parsed = json.loads(args[0])
                if isinstance(parsed, dict):
                    return normalize_command_arguments(command, parsed)
            except Exception:
                pass

        # Positional mappings based on command
        if command in ("app.close", "close_app", "kill_app"):
            app_target = str(args[0]).strip("?.,! \t\n")
            force = len(args) > 1 and str(args[1]).lower() in ("force", "true", "kill")
            return {"app": app_target, "graceful": not force, "force": force}
        elif command in ("app.launch", "open_app"):
            app_target = str(args[0]).strip("?.,! \t\n")
            return {"app": app_target}
        elif command in ("browser.open_url", "open_url"):
            url = str(args[0]).strip("?.,! \t\n")
            return {"url": url}
        elif command in ("screen.capture", "take_screenshot", "screenshot.read", "intent_screenshot"):
            target_device = str(args[0]) if len(args) >= 1 else "this_device"
            disp = int(args[1]) if len(args) >= 2 and str(args[1]).isdigit() else 0
            return {"target_device": target_device, "display_index": disp}
        elif command in ("window.focus", "focus_window", "switch_window"):
            win_target = str(args[0]).strip("?.,! \t\n")
            return {"title": win_target, "app": win_target}
        elif command in ("window.minimize", "window.maximize", "window.restore"):
            win_target = str(args[0]).strip("?.,! \t\n")
            return {"title": win_target}
        elif command in ("fs.open", "open_file", "open_folder", "fs.stat", "fs.delete", "delete_file", "fs.shortcut_info"):
            path_val = str(args[0]).strip() if args else ""
            return {"path": path_val}
        elif command == "fs.list":
            path_val = str(args[0]).strip() if args else "desktop"
            max_res = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 50
            inc_hid = str(args[2]).lower() in ("true", "1") if len(args) > 2 else False
            return {"path": path_val, "max_results": max_res, "include_hidden": inc_hid}
        elif command in ("fs.copy", "copy_file", "fs.move", "move_file"):
            src_val = str(args[0]).strip() if len(args) > 0 else ""
            dst_val = str(args[1]).strip() if len(args) > 1 else ""
            ow = len(args) > 2 and str(args[2]).lower() in ("true", "1", "overwrite")
            return {"src": src_val, "dst": dst_val, "overwrite": ow}
        elif command in ("fs.rename", "rename_file"):
            src_val = str(args[0]).strip() if len(args) > 0 else ""
            new_val = str(args[1]).strip() if len(args) > 1 else ""
            ow = len(args) > 2 and str(args[2]).lower() in ("true", "1", "overwrite")
            return {"src": src_val, "new_name": new_val, "overwrite": ow}
        elif command in ("ui.notify", "show_notification"):
            title = str(args[0]) if len(args) > 1 else "Cortex Notification"
            msg = str(args[1]) if len(args) > 1 else (str(args[0]) if args else "")
            return {"title": title, "message": msg}
        elif command in ("ui.click", "ui.double_click", "ui.right_click", "ui.move_pointer"):
            if len(args) == 1 and not str(args[0]).isdigit():
                return {"target": str(args[0]).strip()}
            elif len(args) >= 2 and str(args[0]).isdigit() and str(args[1]).isdigit():
                return {"x": int(args[0]), "y": int(args[1])}
            elif args:
                return {"target": str(args[0]).strip()}
        elif command == "ui.type":
            txt = str(args[0]) if len(args) >= 1 else ""
            tgt = str(args[1]).strip() if len(args) >= 2 else None
            return {"text": txt, "target": tgt} if tgt else {"text": txt}
        elif command == "ui.keypress":
            return {"key": str(args[0]).strip() if args else "enter"}
        elif command == "ui.hotkey":
            return {"keys": args if len(args) > 1 else str(args[0])} if args else {"keys": ["ctrl", "c"]}
        elif command == "ui.scroll":
            direction = str(args[0]).lower() if args else "down"
            amt = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 300
            return {"direction": direction, "amount": amt}
        elif command == "ui.select":
            val = str(args[0]) if args else ""
            tgt = str(args[1]) if len(args) > 1 else None
            return {"value": val, "target": tgt} if tgt else {"value": val}
        elif command == "ui.wait":
            secs = float(args[0]) if args and (args[0].replace(".", "", 1).isdigit()) else 1.0
            return {"seconds": secs}
        elif command == "clipboard.write":
            return {"text": str(args[0]) if args else ""}
        elif command == "clipboard.read":
            return {}
        elif command == "system.identity":
            return {"current_node": args[0] if args else None}
        elif command == "system.capability_status":
            return {"capabilities": args[0] if args else ""}
        elif command == "system.device_status":
            return {"device_id": args[0] if args else "asus"}
        elif command == "system.integration_status":
            return {"integration": args[0] if args else ""}
        elif command == "system.explain_capability":
            return {"capability": args[0] if args else ""}
        else:
            return {"args": args}

    # Case 3: Single string argument
    if isinstance(args, str):
        s = args.strip()
        if s.startswith(("{", "[")):
            try:
                import json
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return normalize_command_arguments(command, parsed)
            except Exception:
                pass
        return normalize_command_arguments(command, [s])

    return {"args": args}


def execute(command: str, args: Union[dict, list, str, None] = None, latency_metrics: dict = None) -> Result:
    """
    Route a command string to the correct handler.
    Normalizes arguments into canonical dict at the execution boundary.
    Returns consumer-safe failure result for unknown or failed commands.
    """
    import time
    import logging
    log = logging.getLogger("cortex.executor")
    
    if latency_metrics is None:
        latency_metrics = {}

    handler = HANDLERS.get(command)
    if handler is None:
        log.warning(f"Unknown command: '{command}'. Available handlers: {list(HANDLERS.keys())}")
        return {
            "status": "failure",
            "message": f"Action '{command}' is not supported.",
            "error": "unknown_command",
        }

    norm_args = normalize_command_arguments(command, args)
        
    try:
        result = handler(norm_args)
    except Exception as e:
        log.exception(f"Exception during command '{command}': {e}")
        return {
            "status": "failure",
            "message": f"I couldn't complete the action because an error occurred.",
            "error": str(e),
        }
    latency_metrics["app_process_launch"] = time.monotonic()
    
    # Generate Latency Report
    route = latency_metrics.get("routing_path", "UNKNOWN")
    speech_end = latency_metrics.get("speech_end", 0)
    intent_start = latency_metrics.get("intent_route_start", 0)
    exec_start = latency_metrics.get("executor_start", 0)
    
    if speech_end > 0:
        total_latency = latency_metrics["app_process_launch"] - speech_end
        log.info(f"--- LATENCY REPORT ---")
        log.info("Command: %s", command)
        log.info(f"Route: {route}")
        log.info(f"Executor Dispatch: {exec_start - intent_start:.3f}s")
        log.info(f"Speech-end -> app launch: {total_latency:.3f}s")
        log.info(f"----------------------")
        
    # Preserve native structured metadata across both local and WebSocket execution.
    # The wire result carries data, so top-level paths and verification flags must not vanish.
    if isinstance(result, dict):
        metadata = {key:value for key,value in result.items() if key not in {
            "status","message","error","data","latency_metrics","image_base64","screenshot_base64",
            "secret","token","credentials","authorization"}}
        if result.get("data") is None:
            result = dict(result,data=metadata or None)
        elif isinstance(result.get("data"),dict):
            merged=dict(metadata,**result["data"])
            # Negative verification cannot be overwritten by an inconsistent nested flag.
            if result.get("verified") is False:
                merged["verified"]=False
            if result.get("verification_required") is True:
                merged["verification_required"]=True
            result = dict(result,data=merged)
    return result


def get_capabilities() -> list[str]:
    """
    Dynamically determine which capabilities this agent supports at runtime.
    """
    caps = [
        "transfer.inspect",
        "transfer.send",
        "app.list_running",
        "process.list",
        "app.launch",
        "app.close",
        "window.list",
        "window.focus",
        "window.minimize",
        "window.maximize",
        "window.restore",
        "fs.open",
        "fs.copy",
        "fs.move",
        "fs.rename",
        "fs.delete",
        "fs.list",
        "fs.stat",
        "fs.shortcut_info",
        "browser.open_url",
        "device.status",
        # Phase A10 UI capabilities
        "ui.observe",
        "ui.click",
        "ui.double_click",
        "ui.right_click",
        "ui.move_pointer",
        "ui.scroll",
        "ui.type",
        "ui.keypress",
        "ui.hotkey",
        "ui.drag",
        "ui.select",
        "ui.wait",
        "clipboard.read",
        "clipboard.write",
    ]

    # Check screenshot support
    try:
        import pyautogui
        caps.extend(["screen.capture", "ui.screenshot", "screenshot.read"])
    except ImportError:
        if platform.system() == "Windows":
            try:
                from PIL import ImageGrab
                caps.extend(["screen.capture", "ui.screenshot", "screenshot.read"])
            except ImportError:
                pass

    # Check notification support
    system = platform.system()
    if system == "Windows" or system == "Darwin":
        caps.append("ui.notify")
    elif system == "Linux":
        if shutil.which("notify-send"):
            caps.append("ui.notify")

    # Voice capabilities
    caps.extend(["voice.mute", "voice.unmute", "voice.push_to_talk"])

    if os.environ.get("CORTEX_RECEIVE_ROOT"):
        caps.extend(["transfer.prepare", "transfer.cancel"])
    suppressed = [c.strip() for c in os.environ.get("CORTEX_SUPPRESS_CAPS", "").split(",") if c.strip()]
    if suppressed:
        caps = [c for c in caps if c not in suppressed]

    return caps

