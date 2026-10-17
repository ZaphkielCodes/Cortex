"""
agent/ui/runtime_manager.py
---------------------------
Unified Desktop Runtime & Process Manager for Cortex (Phase 8A).

Manages lifecycle and health for:
- Cortex Core (Hub): detection, health checking, graceful start/stop/restart
- Device Agent (ASUS Agent): duplicate prevention, start/stop/restart, status sync
- Health & Heartbeat: periodic polling of Hub /health, agent status, and active tasks
- Approvals: authoritative fetching and resolving of Tier 3 confirmations
- Scrubbed Logging: capturing and streaming sanitized runtime events to GUI
"""

import os
import sys
import re
import time
import socket
import logging
import platform
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List

from PyQt6.QtCore import QObject, pyqtSignal, QTimer
import httpx

log = logging.getLogger("cortex.runtime_manager")

# Root directory of the Cortex project
ROOT_DIR = Path(__file__).resolve().parents[2]
SECRET_FILE = Path.home() / ".cortex" / "device_secret"

# ---------------------------------------------------------------------------
# Secret Scrubbing Helper
# ---------------------------------------------------------------------------
_HEX_64 = re.compile(r'\b[0-9a-fA-F]{64}\b')
_HEX_32 = re.compile(r'\b[0-9a-fA-F]{32}\b')
_API_KEYS = re.compile(
    r'(Bearer\s+[A-Za-z0-9_\-\.]+|AIza[0-9A-Za-z-_]{35}|gsk_[0-9A-Za-z]{48}|xox[baprs]-[0-9a-zA-Z-]+|AKIA[0-9A-Z]{16})',
    re.IGNORECASE
)
_ANSI_STRIP = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def scrub_log_message(msg: str) -> str:
    """Scrub secrets, API keys, and hashes from log messages for safe UI display."""
    if not isinstance(msg, str):
        msg = str(msg)
    msg = _ANSI_STRIP.sub('', msg)
    msg = _HEX_64.sub('<REDACTED_SECRET>', msg)
    msg = _API_KEYS.sub('<REDACTED_API_KEY>', msg)
    return msg


# ---------------------------------------------------------------------------
# Cortex Runtime Manager
# ---------------------------------------------------------------------------
class CortexRuntimeManager(QObject):
    """
    Supervises the Cortex Core Hub and Device Agent for the desktop control plane.
    Emits PyQt signals to notify the GUI of real-time state changes.
    """

    # Signals for GUI synchronization
    hubStatusChanged = pyqtSignal(str)           # "online", "offline", "starting", "error"
    agentStatusChanged = pyqtSignal(str)         # "connected", "disconnected", "reconnecting", "starting"
    voiceStatusChanged = pyqtSignal(str)         # "idle", "listening", "thinking", "speaking", "active_session", "muted"
    metadataChanged = pyqtSignal(dict)           # {"model": ..., "provider": ..., "tasks": ..., "approvals": ...}
    approvalsChanged = pyqtSignal(list)          # list of pending approval dicts
    logMessageReceived = pyqtSignal(str, str)    # (level, message)
    errorOccurred = pyqtSignal(str)              # Error notification

    def __init__(
        self,
        core_url: Optional[str] = None,
        device_id: Optional[str] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)

        # Configurable endpoints
        self.core_url = (core_url or os.environ.get("CORTEX_HUB_URL") or "http://127.0.0.1:8000").rstrip("/")
        self.device_id = device_id or "asus"

        parsed = urlparse(self.core_url)
        self.core_host = parsed.hostname or "127.0.0.1"
        self.core_port = parsed.port or (443 if parsed.scheme == "https" else 8000)
        self.is_core_local = self.core_host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")

        # Process references (managed subprocesses)
        self.hub_proc: Optional[subprocess.Popen] = None
        self.agent_proc: Optional[subprocess.Popen] = None

        # Internal runtime states
        self.hub_state = "offline"
        self.agent_state = "disconnected"
        self.voice_state = "idle"
        self.model_name = "Unknown"
        self.provider_name = "Unknown"
        self.task_count = 0
        self.pending_approvals: List[Dict[str, Any]] = []

        # Background log reader threads
        self._hub_log_threads: List[threading.Thread] = []
        self._agent_log_threads: List[threading.Thread] = []

        # Periodic health check timer (runs on Qt event loop)
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.poll_health)
        self.health_timer.start(2500)

    # -----------------------------------------------------------------------
    # Secret and Credentials helper
    # -----------------------------------------------------------------------
    def get_device_secret(self) -> Optional[str]:
        """Load device secret from environment or local secret file."""
        env_secret = os.environ.get("CORTEX_SECRET", "").strip()
        if env_secret:
            return env_secret
        if SECRET_FILE.exists():
            try:
                content = SECRET_FILE.read_text().strip()
                if content:
                    return content
            except Exception:
                pass
        return None

    # -----------------------------------------------------------------------
    # Port Inspection
    # -----------------------------------------------------------------------
    def is_port_occupied(self, host: str, port: int) -> bool:
        """Check if TCP port is currently listening."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex((host, port)) == 0
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Hub Health Detection
    # -----------------------------------------------------------------------
    def check_hub_health(self) -> Optional[Dict[str, Any]]:
        """
        Synchronously query Core Hub health endpoint.
        Returns parsed health dictionary if healthy Cortex Hub, else None.
        """
        url = f"{self.core_url}/health"
        try:
            with httpx.Client(timeout=1.5) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    # Verify Cortex Hub signature
                    if data.get("status") == "running" and (
                        data.get("service") == "cortex-hub" or "connected_devices" in data
                    ):
                        return data
        except Exception:
            pass

        # Fallback to GET / for backward compatibility
        try:
            with httpx.Client(timeout=1.5) as client:
                resp = client.get(f"{self.core_url}/")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "running" and "connected_devices" in data:
                        return data
        except Exception:
            pass

        return None

    # -----------------------------------------------------------------------
    # Hub Process Management
    # -----------------------------------------------------------------------
    def start_hub(self) -> bool:
        """
        Ensure Hub is running.
        - If healthy Hub is already running (e.g. in background or terminal), reconnect.
        - If port is occupied by non-Cortex process, report Error and DO NOT kill.
        - If port is free and Core is local, spawn Hub process safely.
        """
        self._emit_log("INFO", f"Checking Cortex Core at {self.core_url}...")

        # 1. Is Hub already alive and responding?
        health = self.check_hub_health()
        if health:
            self._set_hub_status("online")
            self._apply_hub_metadata(health)
            self._emit_log("INFO", "Connected to existing Cortex Hub.")
            return True

        # 2. If Core is remote, we cannot spawn a local process for it.
        if not self.is_core_local:
            self._set_hub_status("error")
            msg = f"Remote Cortex Hub at {self.core_url} is unreachable."
            self._emit_log("ERROR", msg)
            self.errorOccurred.emit(msg)
            return False

        # 3. Check if local port is occupied by an unrelated service
        if self.is_port_occupied(self.core_host, self.core_port):
            self._set_hub_status("error")
            msg = f"Port {self.core_port} is occupied by another application. Cannot start Hub."
            self._emit_log("ERROR", msg)
            self.errorOccurred.emit(msg)
            return False

        # 4. Spawn local Hub
        self._set_hub_status("starting")
        self._emit_log("INFO", f"Spawning local Cortex Hub on port {self.core_port}...")

        try:
            cmd = [
                sys.executable, "-m", "uvicorn",
                "hub.main:app",
                "--host", "0.0.0.0",
                "--port", str(self.core_port),
            ]
            self.hub_proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            # Stream scrubbed logs
            self._stream_proc_logs(self.hub_proc, "HUB", self._hub_log_threads)

            # Poll for health with timeout (10 seconds)
            start_time = time.time()
            while time.time() - start_time < 10.0:
                if self.hub_proc.poll() is not None:
                    # Exited prematurely
                    self._set_hub_status("error")
                    self._emit_log("ERROR", f"Hub process exited with code {self.hub_proc.returncode}")
                    return False

                health = self.check_hub_health()
                if health:
                    self._set_hub_status("online")
                    self._apply_hub_metadata(health)
                    self._emit_log("INFO", "Cortex Hub started successfully.")
                    return True
                time.sleep(0.5)

            self._set_hub_status("error")
            self._emit_log("ERROR", "Hub startup timed out after 10 seconds.")
            return False

        except Exception as exc:
            self._set_hub_status("error")
            self._emit_log("ERROR", f"Failed to start Hub process: {exc}")
            return False

    def stop_hub(self) -> bool:
        """Gracefully stop the managed Hub process."""
        if not self.hub_proc or self.hub_proc.poll() is not None:
            self.hub_proc = None
            self._set_hub_status("offline")
            return True

        self._emit_log("INFO", "Stopping Cortex Hub gracefully...")
        try:
            self.hub_proc.terminate()
            try:
                self.hub_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._emit_log("WARN", "Hub did not exit in 5s; terminating forcefully.")
                self.hub_proc.kill()
                self.hub_proc.wait(timeout=2.0)
        except Exception as exc:
            self._emit_log("ERROR", f"Error stopping Hub: {exc}")
        finally:
            self.hub_proc = None
            self._set_hub_status("offline")

        return True

    def restart_hub(self) -> bool:
        """Gracefully stop Hub, wait for port release, and restart."""
        self._emit_log("INFO", "Restarting Cortex Hub...")
        self.stop_hub()
        # Wait for port release if local
        if self.is_core_local:
            for _ in range(15):
                if not self.is_port_occupied(self.core_host, self.core_port):
                    break
                time.sleep(0.2)
        return self.start_hub()

    # -----------------------------------------------------------------------
    # Agent Process Management
    # -----------------------------------------------------------------------
    def start_agent(self) -> bool:
        """
        Ensure the local Device Agent is running.
        - Prevent duplicate Agent processes.
        - Launch with --no-ui so Agent does not spawn a redundant UI window.
        """
        # 1. Is the managed process already running?
        if self.agent_proc and self.agent_proc.poll() is None:
            self._emit_log("INFO", "Local Agent process is already running.")
            return True

        # 2. Check if an external Agent is already authenticated on Hub
        health = self.check_hub_health()
        if health:
            connected = health.get("connected_devices", [])
            if self.device_id in connected:
                self._set_agent_status("connected")
                self._emit_log("INFO", f"Agent '{self.device_id}' is already connected to Hub.")
                return True

        # 3. Spawn local Agent process
        self._set_agent_status("starting")
        self._emit_log("INFO", f"Spawning local Agent for device '{self.device_id}'...")

        ws_url = self.core_url.replace("http://", "ws://").replace("https://", "wss://")
        cmd = [
            sys.executable, "-m", "agent.agent",
            "--hub", ws_url,
            "--device", self.device_id,
            "--no-ui",
        ]

        try:
            self.agent_proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            # Stream scrubbed logs
            self._stream_proc_logs(self.agent_proc, "AGENT", self._agent_log_threads)
            self._emit_log("INFO", f"Agent process started (PID: {self.agent_proc.pid}).")
            return True

        except Exception as exc:
            self._set_agent_status("disconnected")
            self._emit_log("ERROR", f"Failed to start Agent process: {exc}")
            return False

    def stop_agent(self) -> bool:
        """Gracefully stop the managed Agent process."""
        if not self.agent_proc or self.agent_proc.poll() is not None:
            self.agent_proc = None
            self._set_agent_status("disconnected")
            return True

        self._emit_log("INFO", "Stopping Device Agent gracefully...")
        try:
            self.agent_proc.terminate()
            try:
                self.agent_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._emit_log("WARN", "Agent did not exit in 5s; terminating forcefully.")
                self.agent_proc.kill()
                self.agent_proc.wait(timeout=2.0)
        except Exception as exc:
            self._emit_log("ERROR", f"Error stopping Agent: {exc}")
        finally:
            self.agent_proc = None
            self._set_agent_status("disconnected")

        return True

    def restart_agent(self) -> bool:
        """Gracefully stop and restart the local Agent."""
        self._emit_log("INFO", "Restarting Device Agent...")
        self.stop_agent()
        time.sleep(0.5)
        return self.start_agent()

    # -----------------------------------------------------------------------
    # Unified Lifecycle Controls
    # -----------------------------------------------------------------------
    def start_cortex(self) -> bool:
        """
        Unified single-click launch:
        1. Ensure Hub is running.
        2. Ensure local Agent is running.
        3. Connect & sync state.
        """
        self._emit_log("INFO", "════ Starting Cortex Unified Runtime ════")
        hub_ok = self.start_hub()
        if not hub_ok:
            self._emit_log("ERROR", "Cannot proceed with Agent launch: Hub failed to start.")
            return False

        agent_ok = self.start_agent()
        return agent_ok

    def stop_cortex(self) -> bool:
        """Unified shutdown: stop Agent then stop Hub (if locally managed)."""
        self._emit_log("INFO", "════ Stopping Cortex Unified Runtime ════")
        self.stop_agent()
        if self.is_core_local and self.hub_proc:
            self.stop_hub()
        return True

    # -----------------------------------------------------------------------
    # Periodic Health and State Polling
    # -----------------------------------------------------------------------
    def poll_health(self):
        """Periodic timer callback to inspect Hub, Agent, approvals, and tasks."""
        health = self.check_hub_health()
        if health:
            self._set_hub_status("online")
            self._apply_hub_metadata(health)

            # Inspect Agent status from Hub view
            connected_devices = health.get("connected_devices", [])
            devices_detail = health.get("devices_detail", {})
            if self.device_id in connected_devices:
                self._set_agent_status("connected")
            elif self.agent_proc and self.agent_proc.poll() is None:
                self._set_agent_status("reconnecting")
            else:
                self._set_agent_status("disconnected")

            # Fetch pending approvals from Hub
            self._fetch_pending_approvals()

        else:
            if self.hub_state == "starting":
                pass  # Keep starting status during startup window
            elif self.hub_proc and self.hub_proc.poll() is not None:
                self._set_hub_status("error")
            else:
                self._set_hub_status("offline")

            if self.agent_proc and self.agent_proc.poll() is not None:
                self._set_agent_status("disconnected")

    def _apply_hub_metadata(self, health: Dict[str, Any]):
        """Apply model, task, and metadata info from health response."""
        self.model_name = health.get("current_model") or self.model_name
        self.provider_name = health.get("current_provider") or self.provider_name
        self.task_count = health.get("active_tasks_count", 0)

        meta = {
            "model": self.model_name,
            "provider": self.provider_name,
            "tasks": self.task_count,
            "approvals": len(self.pending_approvals),
            "connected_devices": health.get("connected_devices", []),
        }
        self.metadataChanged.emit(meta)

    def _fetch_pending_approvals(self):
        """Fetch Tier 3 approvals from Hub authoritative endpoint."""
        secret = self.get_device_secret()
        if not secret:
            return

        headers = {
            "X-Cortex-Device": self.device_id,
            "X-Cortex-Secret": secret,
        }
        try:
            with httpx.Client(timeout=1.5) as client:
                resp = client.get(f"{self.core_url}/pending", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    rows = data.get("pending", [])
                    if rows != self.pending_approvals:
                        self.pending_approvals = rows
                        self.approvalsChanged.emit(rows)
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Approvals Execution
    # -----------------------------------------------------------------------
    def resolve_approval(self, command_id: str, decision: str) -> bool:
        """
        Submit Allow or Deny decision to authoritative Hub endpoint.
        decision must be 'approve' or 'deny'.
        """
        secret = self.get_device_secret()
        if not secret:
            self._emit_log("ERROR", "No device secret available to authorize approval resolution.")
            return False

        headers = {
            "X-Cortex-Device": self.device_id,
            "X-Cortex-Secret": secret,
        }
        payload = {"decision": decision}

        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.post(
                    f"{self.core_url}/pending/{command_id}/resolve",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 200:
                    self._emit_log(
                        "INFO",
                        f"Approval '{command_id[:8]}…' resolved as {decision.upper()} (authoritative)."
                    )
                    # Refresh pending list immediately
                    self._fetch_pending_approvals()
                    return True
                else:
                    self._emit_log("ERROR", f"Failed to resolve approval: HTTP {resp.status_code} - {resp.text}")
                    return False
        except Exception as exc:
            self._emit_log("ERROR", f"Error resolving approval: {exc}")
            return False

    # -----------------------------------------------------------------------
    # IPC Status Integration (from local Agent)
    # -----------------------------------------------------------------------
    def handle_ipc_message(self, msg: Dict[str, Any]):
        """Integrate real-time messages received over IPC from the Agent."""
        mtype = msg.get("type")
        if mtype == "state":
            vstate = msg.get("state", "idle")
            self._set_voice_status(vstate)
        elif mtype == "agent_status":
            if msg.get("authenticated"):
                self._set_agent_status("connected")
            elif msg.get("connected"):
                self._set_agent_status("reconnecting")
            else:
                self._set_agent_status("disconnected")
        elif mtype == "approval_request":
            # Add to pending approvals list if not already present
            req_id = msg.get("id")
            if req_id and not any(a.get("command_id") == req_id for a in self.pending_approvals):
                self.pending_approvals.append({
                    "command_id": req_id,
                    "command": msg.get("action", "Unknown"),
                    "target_device": msg.get("device", self.device_id),
                    "args": [],
                    "tier": 3,
                    "status": "pending",
                    "summary": msg.get("summary", ""),
                })
                self.approvalsChanged.emit(self.pending_approvals)

    # -----------------------------------------------------------------------
    # State Helpers
    # -----------------------------------------------------------------------
    def _set_hub_status(self, status: str):
        if self.hub_state != status:
            self.hub_state = status
            self.hubStatusChanged.emit(status)

    def _set_agent_status(self, status: str):
        if self.agent_state != status:
            self.agent_state = status
            self.agentStatusChanged.emit(status)

    def _set_voice_status(self, status: str):
        if self.voice_state != status:
            self.voice_state = status
            self.voiceStatusChanged.emit(status)

    def _emit_log(self, level: str, msg: str):
        scrubbed = scrub_log_message(msg)
        self.logMessageReceived.emit(level, scrubbed)

    def _stream_proc_logs(self, proc: subprocess.Popen, tag: str, thread_list: list):
        """Read stdout and stderr in background threads, scrub, and emit."""
        def reader(stream, level):
            try:
                for line in iter(stream.readline, ''):
                    if not line:
                        break
                    line_clean = line.strip()
                    if line_clean:
                        self._emit_log(level, f"[{tag}] {line_clean}")
            except Exception:
                pass

        t_out = threading.Thread(target=reader, args=(proc.stdout, "INFO"), daemon=True)
        t_err = threading.Thread(target=reader, args=(proc.stderr, "WARN"), daemon=True)
        thread_list.extend([t_out, t_err])
        t_out.start()
        t_err.start()
