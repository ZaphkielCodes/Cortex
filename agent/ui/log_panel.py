"""
agent/ui/log_panel.py
---------------------
Collapsible Debug and Runtime Log Panel for Cortex Desktop (Phase 8A).
Displays secret-scrubbed runtime events (connection, auth, routing, commands, approvals, errors).
"""

from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QCheckBox
)
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtCore import Qt


class LogPanel(QWidget):
    """Collapsible runtime log panel for the bottom of the Cortex Desktop window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_expanded = False

        self.setStyleSheet("""
            QWidget#LogPanelRoot {
                background-color: #0b0922;
                border-top: 1px solid #1c1850;
            }
        """)
        self.setObjectName("LogPanelRoot")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 6, 15, 6)
        main_layout.setSpacing(6)

        # ── Header / Toggle Bar ─────────────────────────────────────────────
        header = QWidget()
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(12)

        self.btn_toggle = QPushButton("▲ Runtime & Debug Logs")
        self.btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                color: #a99bff;
                font-size: 12px;
                font-weight: bold;
                text-align: left;
                padding: 4px 0;
            }
            QPushButton:hover {
                color: #ffffff;
            }
        """)
        self.btn_toggle.clicked.connect(self.toggle_collapse)
        h_layout.addWidget(self.btn_toggle)

        h_layout.addStretch()

        self.chk_autoscroll = QCheckBox("Auto-Scroll")
        self.chk_autoscroll.setChecked(True)
        self.chk_autoscroll.setStyleSheet("""
            QCheckBox {
                color: #8c78ff;
                font-size: 11px;
            }
        """)
        h_layout.addWidget(self.chk_autoscroll)

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear.setStyleSheet("""
            QPushButton {
                background-color: #1a1640;
                color: #c0b8ff;
                border: 1px solid #2d266e;
                border-radius: 4px;
                padding: 2px 10px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #272160;
                color: #ffffff;
            }
        """)
        self.btn_clear.clicked.connect(self.clear_logs)
        h_layout.addWidget(self.btn_clear)

        main_layout.addWidget(header)

        # ── Collapsible Log Console ─────────────────────────────────────────
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(1000)
        self.console.setFont(QFont("Consolas", 10))
        self.console.setStyleSheet("""
            QPlainTextEdit {
                background-color: #060515;
                color: #c8c5ff;
                border: 1px solid #1c1850;
                border-radius: 6px;
                padding: 8px;
                font-family: 'Consolas', 'Courier New', monospace;
            }
        """)
        self.console.setFixedHeight(160)
        self.console.setVisible(False)  # Collapsed by default
        main_layout.addWidget(self.console)

    def toggle_collapse(self):
        """Toggle between expanded and collapsed state."""
        self.is_expanded = not self.is_expanded
        self.console.setVisible(self.is_expanded)
        arrow = "▼" if self.is_expanded else "▲"
        self.btn_toggle.setText(f"{arrow} Runtime & Debug Logs")

    def append_log(self, level: str, message: str):
        """Add a sanitized log line to the console."""
        ts = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{ts}] [{level.upper()}] {message}"
        self.console.appendPlainText(formatted)

        if self.chk_autoscroll.isChecked():
            self.console.moveCursor(QTextCursor.MoveOperation.End)

    def clear_logs(self):
        """Clear log console contents."""
        self.console.clear()
