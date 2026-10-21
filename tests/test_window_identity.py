"""OS-ownership boundaries, independent of user wording or application names."""
from agent.window_identity import window_identity


class Windows:
    def __init__(self, children, frame=True):
        self.children = children
        self.frame = frame

    def GetWindowThreadProcessId(self, hwnd, pointer):
        pointer._obj.value = 10 if hwnd == 1 else self.children[hwnd][0]
        return 20

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = "ApplicationFrameWindow" if self.frame else "OrdinaryWindow"
        return len(buffer.value)

    def IsWindowVisible(self, hwnd):
        return self.children[hwnd][1]

    def EnumChildWindows(self, hwnd, callback, data):
        for child in self.children:
            callback(child, data)


def test_hosted_content_uses_actual_child_process_identity():
    windows = Windows({2: (11, True), 3: (11, True), 4: (12, False)})
    result = window_identity(1, windows, lambda pid: {10: "OSHost", 11: "UnseenApplication", 12: "Other"}[pid])
    assert result == {"foreground_app": "UnseenApplication", "pid": 11, "host_pid": 10,
        "host_app": "OSHost", "identity_source": "hosted_content_process"}


def test_multiple_content_processes_are_not_guessed():
    result = window_identity(1, Windows({2: (11, True), 3: (12, True)}), lambda pid: f"process-{pid}")
    assert result["foreground_app"] == "process-10"
    assert result["identity_source"] == "window_process"


def test_ordinary_window_keeps_owner_even_with_embedded_content():
    result = window_identity(1, Windows({2: (11, True)}, frame=False), lambda pid: f"process-{pid}")
    assert result["pid"] == 10


def test_unreadable_content_keeps_host_identity():
    def name(pid):
        if pid == 12:
            raise LookupError("exited process")
        return f"process-{pid}"
    result = window_identity(1, Windows({2: (11, True), 3: (12, True)}), name)
    assert result["pid"] == 10


def test_launch_does_not_return_a_transient_startup_handle(monkeypatch):
    from unittest.mock import Mock
    from agent import desktop_actions as desktop
    monkeypatch.setattr(desktop, "_ACTION_OVERRIDE", None)
    monkeypatch.setattr("agent.app_resolver.resolve_installed_app", lambda _: {
        "launch_type": "executable", "launch_target": "editor.exe", "display_name": "Fixture Editor"})
    start = Mock()
    monkeypatch.setattr(desktop.os, "startfile", start)
    clock = [0.0]
    monkeypatch.setattr(desktop.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(desktop.time, "sleep", lambda duration: clock.__setitem__(0, clock[0]+duration))
    windows = [{"title": "Fixture Editor", "process_name": "editor.exe", "pid": 12, "window_id": value}
               for value in [99, 99, 77]]
    monkeypatch.setattr(desktop, "list_windows", lambda: {"windows": [windows.pop(0) if len(windows)>1 else windows[0]]})
    monkeypatch.setattr(desktop.psutil, "process_iter", lambda _: [])
    monkeypatch.setattr(desktop, "focus_window", lambda args: {"verified": True})
    result = desktop.launch_app({"app": "Fixture Editor"})
    assert result["data"]["window_id"] == 77
    assert clock[0] >= 1.5
    start.assert_called_once()


def test_requested_missing_window_is_a_failure_not_an_unknown_app(monkeypatch):
    from agent.ui_models import UIObservation
    from agent.ui_action_executor import UIActionExecutor
    import agent.ui_observer as observer
    monkeypatch.setattr(observer, "observe_ui_sync", lambda **kwargs: UIObservation(
        observation_id="empty", device_id="source", timestamp="now", screen_size={},
        foreground_app="Unknown", window_title="", summary="No window"))
    result = UIActionExecutor.get_instance().observe({"target_window_id": 99})
    assert result["status"] == "failure" and result["error"] == "window_not_found"
    assert result["data"]["input_attempted"] is False
