"""
agent/ui/sidebar.py
-------------------
Left sidebar for Cortex Desktop Shell (Phase 8A).
Includes:
- Safe Runtime Controls (Start/Stop Cortex, Restart Agent, Restart Hub)
- Compact Runtime Status Area (Hub, ASUS Agent, Voice, Model/Provider, Tasks, Approvals)
- Tier 3 Approvals Card (Action, Device, Reason, Allow, Deny)
- System Hardware Metrics (CPU, RAM)
"""

from typing import Optional, List, Dict, Any
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QFrame
)
from PyQt6.QtCore import Qt, QTimer
import psutil


class LeftSidebar(QWidget):
    """Control plane and status dashboard sidebar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(280)
        self.setStyleSheet("""
            QWidget#SidebarRoot {
                background-color: #0b0922;
                color: #ffffff;
                border-right: 1px solid #1c1850;
            }
        """)
        self.setObjectName("SidebarRoot")

        self.runtime_manager = None
        self.labels: Dict[str, QLabel] = {}
        self.status_badges: Dict[str, QLabel] = {}

        # Scroll area for compact responsive sidebar
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self.layout = QVBoxLayout(content)
        self.layout.setContentsMargins(18, 20, 18, 20)
        self.layout.setSpacing(18)

        # ── Header ──────────────────────────────────────────────────────────
        title = QLabel("Cortex Control")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #a99bff; border: none;")
        self.layout.addWidget(title)

        # ── Runtime Management Controls ─────────────────────────────────────
        controls_group = self._create_group("Runtime Controls")
        c_layout = QVBoxLayout(controls_group)
        c_layout.setContentsMargins(0, 8, 0, 0)
        c_layout.setSpacing(8)

        # Start / Stop Cortex primary buttons
        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(8)

        self.btn_start = QPushButton("Start Cortex")
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #4338ca;
                color: #ffffff;
                border: 1px solid #6366f1;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #4f46e5;
            }
        """)
        self.btn_start.clicked.connect(self._on_start_clicked)
        btn_row1.addWidget(self.btn_start)

        self.btn_stop = QPushButton("Stop Cortex")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.setStyleSheet("""
            QPushButton {
                background-color: #3b1122;
                color: #ff99aa;
                border: 1px solid #771d3d;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #551933;
                color: #ffffff;
            }
        """)
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        btn_row1.addWidget(self.btn_stop)

        c_layout.addLayout(btn_row1)

        # Restart Hub / Restart Agent secondary buttons
        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(8)

        self.btn_restart_agent = QPushButton("Restart Agent")
        self.btn_restart_agent.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_restart_agent.setStyleSheet("""
            QPushButton {
                background-color: #1a1640;
                color: #c0b8ff;
                border: 1px solid #2d266e;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #272160;
                color: #ffffff;
            }
        """)
        self.btn_restart_agent.clicked.connect(self._on_restart_agent_clicked)
        btn_row2.addWidget(self.btn_restart_agent)

        self.btn_restart_hub = QPushButton("Restart Hub")
        self.btn_restart_hub.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_restart_hub.setStyleSheet("""
            QPushButton {
                background-color: #1a1640;
                color: #c0b8ff;
                border: 1px solid #2d266e;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #272160;
                color: #ffffff;
            }
        """)
        self.btn_restart_hub.clicked.connect(self._on_restart_hub_clicked)
        btn_row2.addWidget(self.btn_restart_hub)

        c_layout.addLayout(btn_row2)
        self.layout.addWidget(controls_group)

        # ── Compact Runtime Status Area ─────────────────────────────────────
        status_group = self._create_group("Runtime Status")
        s_layout = QVBoxLayout(status_group)
        s_layout.setContentsMargins(0, 8, 0, 0)
        s_layout.setSpacing(6)

        s_layout.addWidget(self._create_status_item("Hub", "Offline", "hub"))
        s_layout.addWidget(self._create_status_item("ASUS Agent", "Disconnected", "agent"))
        s_layout.addWidget(self._create_status_item("Voice", "Idle", "voice"))
        s_layout.addWidget(self._create_info_item("Model / Provider", "Gemini 2.5 Flash / Groq", "model"))
        s_layout.addWidget(self._create_info_item("Active Tasks", "0 active", "tasks"))
        s_layout.addWidget(self._create_info_item("Pending Approvals", "0 pending", "approvals"))

        self.layout.addWidget(status_group)

        # ── Tier 3 Approvals Container ──────────────────────────────────────
        self.approvals_container = QWidget()
        self.approvals_layout = QVBoxLayout(self.approvals_container)
        self.approvals_layout.setContentsMargins(0, 0, 0, 0)
        self.approvals_layout.setSpacing(8)
        self.approvals_container.setVisible(False)
        self.layout.addWidget(self.approvals_container)

        # ── Hardware Usage ──────────────────────────────────────────────────
        usage_group = self._create_group("Hardware Usage")
        u_layout = QVBoxLayout(usage_group)
        u_layout.setContentsMargins(0, 8, 0, 0)
        self.lbl_system = QLabel("CPU: --%   RAM: --%")
        self.lbl_system.setStyleSheet("color: #e0dfff; font-size: 12px;")
        u_layout.addWidget(self.lbl_system)
        self.layout.addWidget(usage_group)

        self.layout.addStretch()

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)

        # Hardware stats timer
        psutil.cpu_percent(interval=None)
        self.stats_timer = QTimer(self)
        self.stats_timer.timeout.connect(self.update_system_stats)
        self.stats_timer.start(2500)

    # -----------------------------------------------------------------------
    # Component Setup Helpers
    # -----------------------------------------------------------------------
    def _create_group(self, title: str) -> QWidget:
        box = QWidget()
        box.setStyleSheet("border: none;")
        l = QVBoxLayout(box)
        l.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(title)
        lbl.setStyleSheet("color: #8c78ff; font-size: 11px; font-weight: bold; text-transform: uppercase; letter-spacing: 1px;")
        l.addWidget(lbl)
        return box

    def _create_status_item(self, label: str, initial_value: str, key: str) -> QWidget:
        item = QWidget()
        l = QHBoxLayout(item)
        l.setContentsMargins(0, 2, 0, 2)
        lbl_name = QLabel(label)
        lbl_name.setStyleSheet("color: #94a3b8; font-size: 12px;")
        l.addWidget(lbl_name)
        l.addStretch()

        val = QLabel(initial_value)
        val.setStyleSheet("color: #94a3b8; font-size: 12px; font-weight: bold;")
        self.status_badges[key] = val
        l.addWidget(val)
        return item

    def _create_info_item(self, label: str, initial_value: str, key: str) -> QWidget:
        item = QWidget()
        l = QVBoxLayout(item)
        l.setContentsMargins(0, 2, 0, 2)
        l.setSpacing(2)
        lbl_name = QLabel(label)
        lbl_name.setStyleSheet("color: #94a3b8; font-size: 11px;")
        val = QLabel(initial_value)
        val.setStyleSheet("color: #e2e8f0; font-size: 12px; font-weight: 500;")
        val.setWordWrap(True)
        self.labels[key] = val
        l.addWidget(lbl_name)
        l.addWidget(val)
        return item

    # -----------------------------------------------------------------------
    # Wiring with RuntimeManager
    # -----------------------------------------------------------------------
    def set_runtime_manager(self, manager):
        """Connect signals from the CortexRuntimeManager."""
        self.runtime_manager = manager
        manager.hubStatusChanged.connect(self.update_hub_status)
        manager.agentStatusChanged.connect(self.update_agent_status)
        manager.voiceStatusChanged.connect(self.update_voice_status)
        manager.metadataChanged.connect(self.update_metadata)
        manager.approvalsChanged.connect(self.update_approvals)

    def _on_start_clicked(self):
        if self.runtime_manager:
            self.runtime_manager.start_cortex()

    def _on_stop_clicked(self):
        if self.runtime_manager:
            self.runtime_manager.stop_cortex()

    def _on_restart_agent_clicked(self):
        if self.runtime_manager:
            self.runtime_manager.restart_agent()

    def _on_restart_hub_clicked(self):
        if self.runtime_manager:
            self.runtime_manager.restart_hub()

    # -----------------------------------------------------------------------
    # Status Updaters
    # -----------------------------------------------------------------------
    def update_hub_status(self, status: str):
        badge = self.status_badges.get("hub")
        if not badge:
            return
        status_norm = status.lower()
        if status_norm == "online":
            badge.setText("● Online")
            badge.setStyleSheet("color: #4ade80; font-size: 12px; font-weight: bold;")
        elif status_norm == "starting":
            badge.setText("◐ Starting")
            badge.setStyleSheet("color: #fbbf24; font-size: 12px; font-weight: bold;")
        elif status_norm == "error":
            badge.setText("✕ Error")
            badge.setStyleSheet("color: #f87171; font-size: 12px; font-weight: bold;")
        else:
            badge.setText("○ Offline")
            badge.setStyleSheet("color: #94a3b8; font-size: 12px; font-weight: bold;")

    def update_agent_status(self, status: str):
        badge = self.status_badges.get("agent")
        if not badge:
            return
        status_norm = status.lower()
        if status_norm == "connected":
            badge.setText("● Connected")
            badge.setStyleSheet("color: #4ade80; font-size: 12px; font-weight: bold;")
        elif status_norm in ("reconnecting", "starting"):
            badge.setText("◐ Reconnecting" if status_norm == "reconnecting" else "◐ Starting")
            badge.setStyleSheet("color: #fbbf24; font-size: 12px; font-weight: bold;")
        else:
            badge.setText("○ Disconnected")
            badge.setStyleSheet("color: #94a3b8; font-size: 12px; font-weight: bold;")

    def update_voice_status(self, status: str):
        badge = self.status_badges.get("voice")
        if not badge:
            return
        status_norm = status.lower()
        if status_norm == "active_session":
            badge.setText("● Active Session")
            badge.setStyleSheet("color: #38bdf8; font-size: 12px; font-weight: bold;")
        elif status_norm == "listening":
            badge.setText("● Listening")
            badge.setStyleSheet("color: #a78bfa; font-size: 12px; font-weight: bold;")
        elif status_norm == "thinking" or status_norm == "working":
            badge.setText("◐ Thinking")
            badge.setStyleSheet("color: #fbbf24; font-size: 12px; font-weight: bold;")
        elif status_norm == "speaking":
            badge.setText("● Speaking")
            badge.setStyleSheet("color: #34d399; font-size: 12px; font-weight: bold;")
        elif status_norm == "muted":
            badge.setText("✕ Muted")
            badge.setStyleSheet("color: #f87171; font-size: 12px; font-weight: bold;")
        else:
            badge.setText("○ Idle")
            badge.setStyleSheet("color: #94a3b8; font-size: 12px; font-weight: bold;")

    def update_metadata(self, meta: Dict[str, Any]):
        if "model" in meta and "provider" in meta:
            m_lbl = self.labels.get("model")
            if m_lbl:
                m_lbl.setText(f"{meta.get('model')} ({meta.get('provider')})")

        if "tasks" in meta:
            t_lbl = self.labels.get("tasks")
            if t_lbl:
                t_lbl.setText(f"{meta.get('tasks')} active")

        if "approvals" in meta:
            a_lbl = self.labels.get("approvals")
            if a_lbl:
                a_lbl.setText(f"{meta.get('approvals')} pending")

    # -----------------------------------------------------------------------
    # Tier 3 Approvals Card Rendering
    # -----------------------------------------------------------------------
    def update_approvals(self, approvals: List[Dict[str, Any]]):
        """Render pending Tier 3 approval cards in the sidebar."""
        # Clear existing cards
        while self.approvals_layout.count():
            item = self.approvals_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        if not approvals:
            self.approvals_container.setVisible(False)
            return

        self.approvals_container.setVisible(True)

        header = QLabel("⚠ Pending Approvals")
        header.setStyleSheet("color: #fbbf24; font-size: 11px; font-weight: bold; text-transform: uppercase;")
        self.approvals_layout.addWidget(header)

        for app in approvals:
            cid = app.get("command_id", "")
            action = app.get("command") or app.get("action", "Unknown Action")
            target = app.get("target_device") or app.get("device", "Local Device")
            summary = app.get("summary") or app.get("reason", "Action requires confirmation")

            card = QFrame()
            card.setStyleSheet("""
                QFrame {
                    background-color: #171336;
                    border: 1px solid #362f6b;
                    border-radius: 8px;
                    padding: 8px;
                }
            """)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(6, 6, 6, 6)
            card_layout.setSpacing(6)

            action_lbl = QLabel(f"<b>Action:</b> {action}")
            action_lbl.setStyleSheet("color: #ffffff; font-size: 12px;")
            card_layout.addWidget(action_lbl)

            target_lbl = QLabel(f"<b>Target:</b> {target}")
            target_lbl.setStyleSheet("color: #c4b5fd; font-size: 11px;")
            card_layout.addWidget(target_lbl)

            reason_lbl = QLabel(summary)
            reason_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
            reason_lbl.setWordWrap(True)
            card_layout.addWidget(reason_lbl)

            # Allow and Deny button row
            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)

            btn_deny = QPushButton("Deny")
            btn_deny.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_deny.setStyleSheet("""
                QPushButton {
                    background-color: #3b1122;
                    color: #ff99aa;
                    border: 1px solid #771d3d;
                    border-radius: 4px;
                    padding: 4px 10px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #551933;
                }
            """)
            btn_deny.clicked.connect(lambda _, c=cid: self._handle_approval_decision(c, "deny"))
            btn_row.addWidget(btn_deny)

            btn_allow = QPushButton("Allow")
            btn_allow.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_allow.setStyleSheet("""
                QPushButton {
                    background-color: #064e3b;
                    color: #6ee7b7;
                    border: 1px solid #059669;
                    border-radius: 4px;
                    padding: 4px 10px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #047857;
                }
            """)
            btn_allow.clicked.connect(lambda _, c=cid: self._handle_approval_decision(c, "approve"))
            btn_row.addWidget(btn_allow)

            card_layout.addLayout(btn_row)
            self.approvals_layout.addWidget(card)

    def _handle_approval_decision(self, command_id: str, decision: str):
        if self.runtime_manager:
            self.runtime_manager.resolve_approval(command_id, decision)

    def update_system_stats(self):
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        self.lbl_system.setText(f"CPU: {cpu:.1f}%   RAM: {ram:.1f}%")
