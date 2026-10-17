"""
agent/desktop_actions.py
-------------------------
Reliable Desktop Action Layer for Cortex (Phase A3).

Provides deterministic inspection and control over:
- Running applications (app.list_running)
- Window enumeration, state, and focus (window.list, window.focus, window.minimize, window.maximize, window.restore)
- Application launch & close verification (app.launch, app.close)
"""

import ctypes
from ctypes import wintypes
import logging
import os
import platform
import subprocess
import time
from typing import Any, Dict, List, Optional, Union

import psutil

log = logging.getLogger("cortex.desktop_actions")

from dataclasses import dataclass
from typing import Callable

# Runtime mock override hook for unit testing
@dataclass
class DesktopActionsOverride:
    list_running_apps: Optional[Callable] = None
    list_windows: Optional[Callable] = None
    launch_app: Optional[Callable] = None
    close_app: Optional[Callable] = None
    focus_window: Optional[Callable] = None
    minimize_window: Optional[Callable] = None
    maximize_window: Optional[Callable] = None
    restore_window: Optional[Callable] = None
    list_processes: Optional[Callable] = None

_ACTION_OVERRIDE: Optional[Any] = None


def set_desktop_actions_override(override: Optional[Any]) -> None:
    """Set or clear a runtime override for desktop actions (for hermetic testing)."""
    global _ACTION_OVERRIDE
    _ACTION_OVERRIDE = override


def get_desktop_actions_override() -> Optional[Any]:
    return _ACTION_OVERRIDE


# ---------------------------------------------------------------------------
# Windows API Desktop Attachment Helper
# ---------------------------------------------------------------------------
def _ensure_default_desktop() -> None:
    """Ensure the calling thread is attached to the interactive 'default' desktop."""
    if platform.system() == "Windows":
        try:
            user32 = ctypes.windll.user32
            hdesk = user32.OpenDesktopW("default", 0, False, 0x01FF)
            if hdesk:
                user32.SetThreadDesktop(hdesk)
        except Exception as exc:
            log.debug(f"Could not attach to default desktop: {exc}")


# ---------------------------------------------------------------------------
# 1. Running Applications Inspection (app.list_running)
# ---------------------------------------------------------------------------
def list_running_apps(args: Union[list, dict] = None) -> Dict[str, Any]:
    """
    List running applications with process name, PID, executable path, and window count.
    """
    if _ACTION_OVERRIDE and getattr(_ACTION_OVERRIDE, "list_running_apps", None):
        return _ACTION_OVERRIDE.list_running_apps(args)

    _ensure_default_desktop()

    # Pre-calculate window counts per PID
    win_list_res = list_windows()
    win_counts: Dict[int, int] = {}
    if win_list_res.get("status") == "success":
        for w in win_list_res.get("windows", []):
            p = w.get("pid")
            if p:
                win_counts[p] = win_counts.get(p, 0) + 1

    apps: List[Dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = proc.info
            pid = info.get("pid")
            name = info.get("name") or ""
            if not name or pid is None:
                continue
            # Filter out internal idle/system meta-processes
            if pid <= 4 and name.lower() in ("system idle process", "system"):
                continue

            exe_p = info.get("exe") or ""
            w_count = win_counts.get(pid, 0)

            apps.append({
                "process_name": name,
                "pid": pid,
                "executable_path": exe_p,
                "window_count": w_count,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Sort alphabetically by process name
    apps.sort(key=lambda a: a["process_name"].lower())

    return {
        "status": "success",
        "message": f"Found {len(apps)} running applications",
        "count": len(apps),
        "data": apps,
        "apps": apps,
    }


# ---------------------------------------------------------------------------
# 1b. Process Telemetry (process.list)
# ---------------------------------------------------------------------------
def list_processes(args: Union[list, dict] = None) -> Dict[str, Any]:
    """
    List processes with telemetry: pid, process_name, executable_path,
    memory_bytes, memory_percent, cpu_percent.
    Supports optional sort_by ('memory', 'cpu', 'name') and limit.
    """
    if _ACTION_OVERRIDE and getattr(_ACTION_OVERRIDE, "list_processes", None):
        return _ACTION_OVERRIDE.list_processes(args)

    _ensure_default_desktop()

    kwargs: Dict[str, Any] = {}
    if isinstance(args, dict):
        kwargs = args
    elif isinstance(args, list) and args and isinstance(args[0], dict):
        kwargs = args[0]

    sort_by = kwargs.get("sort_by", "memory")
    limit = kwargs.get("limit")

    procs: List[Dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "memory_info", "memory_percent", "cpu_percent"]):
        try:
            info = proc.info
            pid = info.get("pid")
            name = info.get("name") or ""
            if not name or pid is None:
                continue
            if pid <= 4 and name.lower() in ("system idle process", "system"):
                continue

            mem_info = info.get("memory_info")
            mem_bytes = getattr(mem_info, "rss", 0) if mem_info else 0
            mem_pct = round(info.get("memory_percent") or 0.0, 2)
            cpu_pct = round(info.get("cpu_percent") or 0.0, 2)
            exe_p = info.get("exe") or ""

            procs.append({
                "pid": pid,
                "process_name": name,
                "memory_bytes": mem_bytes,
                "memory_percent": mem_pct,
                "cpu_percent": cpu_pct,
                "executable_path": exe_p,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if sort_by == "memory":
        procs.sort(key=lambda p: p["memory_bytes"], reverse=True)
    elif sort_by == "cpu":
        procs.sort(key=lambda p: p["cpu_percent"], reverse=True)
    else:
        procs.sort(key=lambda p: p["process_name"].lower())

    if limit and isinstance(limit, int) and limit > 0:
        procs = procs[:limit]

    return {
        "status": "success",
        "message": f"Found {len(procs)} processes",
        "count": len(procs),
        "processes": procs,
        "data": procs,
    }



# ---------------------------------------------------------------------------
# 2. Window Inspection (window.list)
# ---------------------------------------------------------------------------
def list_windows(args: Union[list, dict] = None) -> Dict[str, Any]:
    """
    Enumerate visible windows with window_id, title, process_name, PID,
    visibility, minimized, foreground, and bounding rectangle.
    """
    if _ACTION_OVERRIDE and getattr(_ACTION_OVERRIDE, "list_windows", None):
        return _ACTION_OVERRIDE.list_windows(args)

    if platform.system() != "Windows":
        return {
            "status": "success",
            "message": "Window listing not supported on non-Windows platforms",
            "count": 0,
            "windows": [],
            "data": [],
        }

    _ensure_default_desktop()

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    windows: List[Dict[str, Any]] = []
    fg_hwnd = user32.GetForegroundWindow()

    def enum_proc(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value.strip()
                if title:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    is_min = bool(user32.IsIconic(hwnd))
                    is_fg = (hwnd == fg_hwnd)

                    pname = ""
                    try:
                        pname = psutil.Process(pid.value).name()
                    except Exception:
                        pass

                    w_info = {
                        "window_id": int(hwnd),
                        "handle": int(hwnd),
                        "title": title,
                        "process_name": pname,
                        "pid": pid.value,
                        "visible": True,
                        "minimized": is_min,
                        "foreground": is_fg,
                        "bounds": {
                            "x": rect.left,
                            "y": rect.top,
                            "width": rect.right - rect.left,
                            "height": rect.bottom - rect.top,
                        },
                    }
                    windows.append(w_info)
        return True

    cb = WNDENUMPROC(enum_proc)
    hdesk = user32.OpenDesktopW("default", 0, False, 0x01FF)
    user32.EnumDesktopWindows(hdesk if hdesk else 0, cb, 0)

    return {
        "status": "success",
        "message": f"Found {len(windows)} open windows",
        "count": len(windows),
        "windows": windows,
        "data": windows,
    }


# ---------------------------------------------------------------------------
# Helper: Window Matching & Resolution
# ---------------------------------------------------------------------------
def _find_matching_windows(
    target: str,
    windows: List[Dict[str, Any]],
    target_window_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Resolve candidate windows matching a target query."""
    if target_window_id is not None:
        return [w for w in windows if w["window_id"] == target_window_id]

    if not target:
        return []

    target_lower = target.lower().strip()
    if target_lower.isdigit():
        wid = int(target_lower)
        exact_id = [w for w in windows if w["window_id"] == wid]
        if exact_id:
            return exact_id

    # 1. Exact match on title or process name
    exact_matches = [
        w for w in windows
        if w["title"].lower() == target_lower
        or w["process_name"].lower() == target_lower
        or w["process_name"].lower().replace(".exe", "") == target_lower
    ]
    if len(exact_matches) == 1:
        return exact_matches

    # 2. Substring match
    sub_matches = [
        w for w in windows
        if target_lower in w["title"].lower()
        or target_lower in w["process_name"].lower()
    ]
    return sub_matches


# ---------------------------------------------------------------------------
# 3. Window Focus (window.focus)
# ---------------------------------------------------------------------------
def focus_window(args: Union[list, dict]) -> Dict[str, Any]:
    """
    Restore if minimized, bring to foreground, and verify focus.
    If multiple windows match ambiguously, requests clarification.
    """
    if _ACTION_OVERRIDE and getattr(_ACTION_OVERRIDE, "focus_window", None):
        return _ACTION_OVERRIDE.focus_window(args)

    target_window_id = None
    target_str = ""

    if isinstance(args, dict):
        target_window_id = args.get("window_id") or args.get("handle")
        target_str = args.get("title") or args.get("process") or args.get("app") or args.get("target") or ""
    elif isinstance(args, list) and args:
        val = str(args[0]).strip()
        if val.isdigit():
            target_window_id = int(val)
        else:
            target_str = val

    target_str = str(target_str).strip("?.,! \t\n")

    win_res = list_windows()
    windows = win_res.get("windows", [])
    matches = _find_matching_windows(target_str, windows, target_window_id)

    if not matches:
        return {
            "status": "failure",
            "message": f"No matching window found for '{target_str or target_window_id}'",
            "error": "window_not_found",
        }

    # Ambiguity check: multiple distinct windows matched
    if len(matches) > 1:
        candidate_summary = [
            f"'{m['title']}' ({m['process_name']}, id: {m['window_id']})"
            for m in matches
        ]
        return {
            "status": "clarification_needed",
            "ambiguous": True,
            "message": f"Multiple windows found matching '{target_str}': {'; '.join(candidate_summary)}. Which one would you like to focus?",
            "candidates": matches,
        }

    target_win = matches[0]
    hwnd = target_win["window_id"]

    if platform.system() == "Windows":
        _ensure_default_desktop()
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 1. Restore if minimized (SW_RESTORE = 9)
        if target_win.get("minimized"):
            user32.ShowWindow(hwnd, 9)
        else:
            user32.ShowWindow(hwnd, 5)  # SW_SHOW = 5

        # 2. Attach thread input & pulse Alt to bypass Windows foreground lock
        cur_tid = kernel32.GetCurrentThreadId()
        fg_hwnd = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
        try:
            user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
        except Exception:
            pass
        if fg_tid != cur_tid and fg_tid != 0:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        else:
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        try:
            user32.keybd_event(0x12, 0, 2, 0)  # VK_MENU up
        except Exception:
            pass

        # 3. Verify foreground window changed
        verified = False
        start = time.monotonic()
        while time.monotonic() - start < 1.5:
            current_fg = user32.GetForegroundWindow()
            if current_fg == hwnd:
                verified = True
                break
            time.sleep(0.05)

        target_win["foreground"] = True
        return {
            "status": "success",
            "message": f"Focused window '{target_win['title']}'",
            "verified": verified,
            "data": target_win,
        }

    return {
        "status": "success",
        "message": f"Focused window '{target_win['title']}' (non-windows simulated)",
        "verified": True,
        "data": target_win,
    }


# ---------------------------------------------------------------------------
# 4. Window State Capabilities (minimize, maximize, restore)
# ---------------------------------------------------------------------------
def minimize_window(args: Union[list, dict]) -> Dict[str, Any]:
    """Minimize a target window and verify iconic state."""
    if _ACTION_OVERRIDE and getattr(_ACTION_OVERRIDE, "minimize_window", None):
        return _ACTION_OVERRIDE.minimize_window(args)

    target_str = args.get("title") or args.get("target") if isinstance(args, dict) else (args[0] if args else "")
    target_str = str(target_str).strip("?.,! \t\n")
    target_id = args.get("window_id") if isinstance(args, dict) else None

    win_res = list_windows()
    matches = _find_matching_windows(str(target_str), win_res.get("windows", []), target_id)
    if not matches:
        return {"status": "failure", "message": f"Window not found: '{target_str}'", "error": "window_not_found"}

    target_win = matches[0]
    hwnd = target_win["window_id"]

    if platform.system() == "Windows":
        _ensure_default_desktop()
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE = 6
        is_min = bool(user32.IsIconic(hwnd))
        return {
            "status": "success",
            "message": f"Minimized window '{target_win['title']}'",
            "verified": is_min,
            "data": {"window_id": hwnd, "minimized": True},
        }

    return {"status": "success", "message": f"Minimized window '{target_win['title']}'", "verified": True}


def maximize_window(args: Union[list, dict]) -> Dict[str, Any]:
    """Maximize a target window and verify."""
    if _ACTION_OVERRIDE and getattr(_ACTION_OVERRIDE, "maximize_window", None):
        return _ACTION_OVERRIDE.maximize_window(args)

    target_str = args.get("title") or args.get("target") if isinstance(args, dict) else (args[0] if args else "")
    target_str = str(target_str).strip("?.,! \t\n")
    target_id = args.get("window_id") if isinstance(args, dict) else None

    win_res = list_windows()
    matches = _find_matching_windows(str(target_str), win_res.get("windows", []), target_id)
    if not matches:
        return {"status": "failure", "message": f"Window not found: '{target_str}'", "error": "window_not_found"}

    target_win = matches[0]
    hwnd = target_win["window_id"]

    if platform.system() == "Windows":
        _ensure_default_desktop()
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE = 3
        is_max = bool(user32.IsZoomed(hwnd))
        return {
            "status": "success",
            "message": f"Maximized window '{target_win['title']}'",
            "verified": is_max,
            "data": {"window_id": hwnd, "maximized": True},
        }

    return {"status": "success", "message": f"Maximized window '{target_win['title']}'", "verified": True}


def restore_window(args: Union[list, dict]) -> Dict[str, Any]:
    """Restore a target window from minimized/maximized state."""
    if _ACTION_OVERRIDE and getattr(_ACTION_OVERRIDE, "restore_window", None):
        return _ACTION_OVERRIDE.restore_window(args)

    target_str = args.get("title") or args.get("target") if isinstance(args, dict) else (args[0] if args else "")
    target_str = str(target_str).strip("?.,! \t\n")
    target_id = args.get("window_id") if isinstance(args, dict) else None

    win_res = list_windows()
    matches = _find_matching_windows(str(target_str), win_res.get("windows", []), target_id)
    if not matches:
        return {"status": "failure", "message": f"Window not found: '{target_str}'", "error": "window_not_found"}

    target_win = matches[0]
    hwnd = target_win["window_id"]

    if platform.system() == "Windows":
        _ensure_default_desktop()
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE = 9
        is_min = bool(user32.IsIconic(hwnd))
        return {
            "status": "success",
            "message": f"Restored window '{target_win['title']}'",
            "verified": not is_min,
            "data": {"window_id": hwnd, "minimized": False},
        }

    return {"status": "success", "message": f"Restored window '{target_win['title']}'", "verified": True}


# ---------------------------------------------------------------------------
# 5. Application Close (app.close)
# ---------------------------------------------------------------------------
def close_app(args: Union[list, dict]) -> Dict[str, Any]:
    """
    Close an application gracefully by default, verifying process/window exit.
    Does NOT force-kill by default.
    If multiple distinct windows exist for the app, asks for clarification.
    """
    if _ACTION_OVERRIDE and getattr(_ACTION_OVERRIDE, "close_app", None):
        return _ACTION_OVERRIDE.close_app(args)

    target = ""
    graceful = True
    force = False

    if isinstance(args, dict):
        target = args.get("app") or args.get("process") or args.get("target") or ""
        graceful = bool(args.get("graceful", True))
        force = bool(args.get("force", False))
    elif isinstance(args, list) and args:
        if len(args) == 1 and isinstance(args[0], str) and args[0].strip().startswith("{"):
            try:
                import json
                parsed = json.loads(args[0])
                if isinstance(parsed, dict):
                    target = parsed.get("app") or parsed.get("process") or parsed.get("target") or ""
                    graceful = bool(parsed.get("graceful", True))
                    force = bool(parsed.get("force", False))
            except Exception:
                pass
        if not target:
            target = str(args[0]).strip()
            if len(args) > 1 and str(args[1]).lower() in ("force", "true", "kill"):
                force = True
                graceful = False

    target = target.strip("?.,! \t\n")

    if not target:
        return {"status": "failure", "message": "app.close: no app or process target provided"}

    target_lower = target.lower().strip()

    # 1. Enumerate matching windows for ambiguity check
    win_res = list_windows()
    matching_windows = [
        w for w in win_res.get("windows", [])
        if target_lower in w["process_name"].lower()
        or target_lower in w["title"].lower()
        or target_lower == w["process_name"].lower().replace(".exe", "")
    ]

    # Check for ambiguity: e.g. multiple distinct top-level windows for this app
    if len(matching_windows) > 1:
        # Check if they have materially different titles
        titles = list({w["title"] for w in matching_windows})
        if len(titles) > 1:
            candidates = [
                f"'{w['title']}' (id: {w['window_id']})" for w in matching_windows
            ]
            return {
                "status": "clarification_needed",
                "ambiguous": True,
                "message": f"Multiple windows found for '{target}': {'; '.join(candidates)}. Which one would you like to close?",
                "candidates": matching_windows,
            }

    # 2. Locate matching PIDs
    matching_pids: List[int] = []
    if matching_windows:
        matching_pids = list({w["pid"] for w in matching_windows})
    else:
        for p in psutil.process_iter(["pid", "name"]):
            try:
                name = (p.info["name"] or "").lower()
                if target_lower in name or target_lower == name.replace(".exe", ""):
                    matching_pids.append(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    if not matching_pids:
        return {
            "status": "failure",
            "message": f"Application or process '{target}' is not currently running.",
            "error": "not_running",
        }

    # 3. Attempt Close
    user32 = ctypes.windll.user32 if platform.system() == "Windows" else None
    _ensure_default_desktop()

    closed_pids: List[int] = []
    failed_pids: List[int] = []

    for pid in matching_pids:
        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            closed_pids.append(pid)
            continue

        if force:
            try:
                proc.kill()
                proc.wait(timeout=3)
                closed_pids.append(pid)
            except Exception as e:
                log.warning(f"Failed to force-kill PID {pid}: {e}")
                failed_pids.append(pid)
            continue

        # Graceful close: Post WM_CLOSE to all windows of this process
        windows_for_pid = [w["window_id"] for w in matching_windows if w["pid"] == pid]
        if user32 and windows_for_pid:
            for hwnd in windows_for_pid:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE = 0x0010

        # Also terminate process gracefully if not already closed
        try:
            proc.terminate()
        except Exception:
            pass

        # Verify exit within timeout (up to 4 seconds)
        exited = False
        start = time.monotonic()
        while time.monotonic() - start < 4.0:
            if not psutil.pid_exists(pid):
                exited = True
                break
            try:
                if proc.status() == psutil.STATUS_ZOMBIE:
                    exited = True
                    break
            except Exception:
                exited = True
                break
            time.sleep(0.2)

        if exited:
            closed_pids.append(pid)
        else:
            failed_pids.append(pid)

    if failed_pids and not closed_pids:
        return {
            "status": "failure",
            "message": f"App '{target}' did not exit gracefully within timeout. Force termination was not requested.",
            "error": "graceful_close_failed",
            "pids": failed_pids,
        }

    return {
        "status": "success",
        "message": f"Closed '{target}' (PID {closed_pids[0] if closed_pids else 'unknown'})",
        "data": {"closed_pids": closed_pids, "target": target},
    }


# ---------------------------------------------------------------------------
# 6. Hardened Application Launch (app.launch)
# ---------------------------------------------------------------------------
def launch_app(args: Union[list, dict]) -> Dict[str, Any]:
    """
    Launch application with verified appearance of window/process.
    Returns actual PID / window ID.
    """
    if _ACTION_OVERRIDE and getattr(_ACTION_OVERRIDE, "launch_app", None):
        return _ACTION_OVERRIDE.launch_app(args)

    app_name = ""
    if isinstance(args, dict):
        app_name = args.get("app") or args.get("app_name") or ""
    elif isinstance(args, list) and args:
        if len(args) == 1 and isinstance(args[0], str) and args[0].strip().startswith("{"):
            try:
                import json
                parsed = json.loads(args[0])
                if isinstance(parsed, dict):
                    app_name = parsed.get("app") or parsed.get("app_name") or ""
            except Exception:
                pass
        if not app_name:
            app_name = " ".join([str(a) for a in args])

    app_name = app_name.strip("?.,! \t\n")

    if not app_name:
        return {"status": "failure", "message": "app.launch: no app name provided"}

    from .app_resolver import resolve_installed_app
    resolved = resolve_installed_app(app_name)
    if not resolved:
        return {
            "status": "failure",
            "message": f"app.launch: '{app_name}' could not be resolved to an installed application.",
        }

    launch_type = resolved.get("launch_type")
    launch_target = resolved.get("launch_target")

    try:
        if launch_type == "executable":
            try:
                os.startfile(launch_target)
            except (FileNotFoundError, OSError):
                subprocess.Popen(
                    ["cmd", "/c", "start", "", launch_target],
                    shell=False,
                    creationflags=0x08000000 if platform.system() == "Windows" else 0,
                )
        elif launch_type == "app_user_model_id":
            try:
                os.startfile(fr"shell:AppsFolder\{launch_target}")
            except (FileNotFoundError, OSError):
                subprocess.Popen(
                    ["explorer.exe", fr"shell:AppsFolder\{launch_target}"],
                    shell=False,
                )
    except Exception as exc:
        return {"status": "failure", "message": f"app.launch execution failed: {exc}"}

    # Verify launch: check for visible window or active process within 5s
    display_name = resolved.get("display_name", app_name)
    exe_name = ""
    if launch_type == "executable" and launch_target:
        exe_name = os.path.basename(launch_target).lower().replace(".exe", "")

    start = time.monotonic()
    matched_window = None
    matched_pid = None

    while time.monotonic() - start < 5.0:
        win_res = list_windows()
        for w in win_res.get("windows", []):
            w_title = w["title"].lower()
            w_proc = w["process_name"].lower()
            if (
                display_name.lower() in w_title
                or (exe_name and exe_name in w_proc)
                or display_name.lower() in w_proc
            ):
                matched_window = w
                matched_pid = w["pid"]
                break

        if matched_window:
            break

        # Secondary: check background process
        if exe_name:
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if exe_name in (p.info["name"] or "").lower():
                        matched_pid = p.info["pid"]
                        break
                except Exception:
                    pass
        if matched_pid:
            break

        time.sleep(0.3)

    if not matched_pid and not matched_window:
        return {
            "status": "failure",
            "message": f"Could not verify '{display_name}' launched. No matching window or process appeared within timeout.",
            "error": "launch_verification_failed",
        }

    return {
        "status": "success",
        "message": f"Opened '{display_name}'",
        "data": {
            "app_name": display_name,
            "pid": matched_pid,
            "window_id": matched_window.get("window_id") if matched_window else None,
            "window_title": matched_window.get("title") if matched_window else None,
            "verified": True,
        },
    }
