"""Native process identity for a window, including Windows hosted application frames."""
import ctypes
from ctypes import wintypes


def window_identity(hwnd, user32=None, process_name=None):
    """Resolve OS ownership only; never infer an application from a window title.

    A Windows ApplicationFrameWindow may host a distinct application's child
    window. Only one visible content process is accepted; ambiguity keeps the
    frame owner. EnumChildWindows/GetWindowThreadProcessId are documented Win32
    ownership APIs, not a list of supported user applications.
    """
    if user32 is None:
        user32 = ctypes.windll.user32
    if process_name is None:
        import psutil
        process_name = lambda pid: psutil.Process(pid).name().removesuffix(".exe")

    def owner(window):
        pid = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(window, ctypes.byref(pid)) or not pid.value:
            raise OSError("Window ownership unavailable")
        return int(pid.value)

    pid = owner(hwnd)
    host = process_name(pid)
    result = {"foreground_app": host, "pid": pid, "host_pid": pid,
              "host_app": host, "identity_source": "window_process"}
    class_name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_name, len(class_name))
    if class_name.value != "ApplicationFrameWindow":
        return result
    content = {}
    complete = True

    def visit(child, _):
        nonlocal complete
        try:
            if user32.IsWindowVisible(child):
                child_pid = owner(child)
                if child_pid != pid:
                    content[child_pid] = process_name(child_pid)
        except Exception:
            complete = False
        return True

    callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    callback = callback_type(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    # https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-enumchildwindows
    user32.EnumChildWindows(hwnd, callback, 0)
    if complete and len(content) == 1:
        content_pid, content_app = next(iter(content.items()))
        result.update(foreground_app=content_app, pid=content_pid, identity_source="hosted_content_process")
    return result
