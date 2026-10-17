"""
agent/ui/main.py
----------------
Cortex Desktop GUI & Runtime Control Plane (Phase 8A).
Serves as the unified desktop launcher and control surface.
"""

import sys
import os
import json
import argparse
from pathlib import Path

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import pyqtSlot, QObject, pyqtSignal, QTimer

from .bridge import Bridge
from .window import MainWindow
from .runtime_manager import CortexRuntimeManager
from ..ui_ipc import UIIPCClient

IPC_PORT_FILE = str(Path.home() / ".cortex" / ".ipc_port")


class UIController(QObject):
    ipc_message_received = pyqtSignal(dict)

    def __init__(
        self,
        port: int | None = None,
        core_url: str | None = None,
        device_id: str = "asus",
        auto_start: bool = True,
    ):
        super().__init__()
        self.bridge = Bridge()
        self.window = MainWindow(self.bridge)

        # Unified Runtime Manager
        self.runtime_manager = CortexRuntimeManager(
            core_url=core_url,
            device_id=device_id,
            parent=self,
        )
        self.window.set_runtime_manager(self.runtime_manager)

        # IPC Client with auto-discovery and auto-reconnect
        self.ipc = UIIPCClient(port=port, on_message=self._on_ipc_message, auto_reconnect=True)
        self.bridge.configure_controls(self.runtime_manager, self.ipc)
        self.ipc_message_received.connect(self._handle_ipc_message)

        self.bridge.actionRequested.connect(self._on_action_requested)
        self.bridge.approvalDecision.connect(self._on_approval_decision)

        self.ipc.start()

        if auto_start:
            # Asynchronously trigger Cortex startup after Qt event loop begins
            QTimer.singleShot(200, self.runtime_manager.start_cortex)

    def _on_ipc_message(self, msg: dict):
        self.ipc_message_received.emit(msg)

    @pyqtSlot(dict)
    def _handle_ipc_message(self, msg: dict):
        # Notify runtime manager for health & status sync
        self.runtime_manager.handle_ipc_message(msg)

        mtype = msg.get("type")
        if mtype == "state":
            state = msg.get("state", "idle")
            if state == "thinking":
                state = "working"
            self.bridge.coreStateChanged.emit(state)
        elif mtype == "audio_level":
            level = msg.get("level", 0.0)
            self.bridge.audioLevelChanged.emit(level)
        elif mtype == "approval_request":
            # Send to JS via QWebChannel
            approval_data = json.dumps({
                "id": msg.get("id", ""),
                "action": msg.get("action", "Unknown Action"),
                "device": msg.get("device", "Unknown Device"),
                "summary": msg.get("summary", ""),
            })
            self.bridge.approvalRequested.emit(approval_data)

    def _on_action_requested(self, action: str):
        self.ipc.send({"type": "action", "action": action})

    @pyqtSlot(str)
    def _on_approval_decision(self, decision_json: str):
        """Forward JS approval decision back to Hub and Agent."""
        try:
            decision = json.loads(decision_json)
            cid = decision.get("id")
            approved = decision.get("approved", False)
            dec_str = "approve" if approved else "deny"

            # 1. Authoritative resolution on Hub
            if cid:
                self.runtime_manager.resolve_approval(cid, dec_str)

            # 2. Also notify local Agent IPC
            self.ipc.send({
                "type": "approval_response",
                "id": cid,
                "approved": approved
            })
        except Exception as e:
            print(f"Error forwarding approval decision: {e}")


def main():
    parser = argparse.ArgumentParser(description="Cortex Desktop Control Plane")
    parser.add_argument("--ipc-port", type=int, default=None, help="Explicit Agent IPC port")
    parser.add_argument("--hub", type=str, default=None, help="Cortex Core endpoint URL")
    parser.add_argument("--device", type=str, default="asus", help="Target device ID")
    parser.add_argument("--no-auto-start", action="store_true", help="Do not auto-start Cortex on launch")
    args = parser.parse_args()

    port = args.ipc_port
    if port is None and os.path.exists(IPC_PORT_FILE):
        try:
            with open(IPC_PORT_FILE, "r") as f:
                port = int(f.read().strip())
        except Exception:
            port = None

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    controller = UIController(
        port=port,
        core_url=args.hub,
        device_id=args.device,
        auto_start=not args.no_auto_start,
    )
    controller.window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
