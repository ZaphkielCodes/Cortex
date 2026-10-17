"""
agent/ui/window.py
------------------
Main desktop application window for Cortex (Phase 8A).
Hosts:
- LeftSidebar: Control plane, status dashboard, and approval cards
- WorkspaceView: Center view hosting the React CortexOrb
- LogPanel: Collapsible bottom panel showing sanitized real-time events
"""

from PyQt6.QtWidgets import QMainWindow, QHBoxLayout, QVBoxLayout, QWidget
from .sidebar import LeftSidebar
from .workspace import WorkspaceView
from .log_panel import LogPanel
from .bridge import Bridge


class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cortex Control Center")
        self.resize(1440, 900)
        self.setStyleSheet("background-color: #040711;")

        self.runtime_manager = None

        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Retain background references for runtime manager wiring ─────────
        self.sidebar = LeftSidebar()
        self.sidebar.hide()
        self.log_panel = LogPanel()
        self.log_panel.hide()

        # ── WorkspaceView: Unified Polished Cortex Assistant View ──────────
        self.workspace = WorkspaceView(bridge)
        main_layout.addWidget(self.workspace, stretch=1)

    def set_runtime_manager(self, manager):
        """Wire up the runtime manager to sidebar and log panel."""
        self.runtime_manager = manager
        self.sidebar.set_runtime_manager(manager)
        manager.logMessageReceived.connect(self.log_panel.append_log)

    def closeEvent(self, event):
        """Gracefully stop managed Cortex processes on GUI window close."""
        if self.workspace.bridge.transport:
            self.workspace.bridge.transport.close()
        if self.runtime_manager:
            self.runtime_manager.stop_cortex()
        event.accept()
