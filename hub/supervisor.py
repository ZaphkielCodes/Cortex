"""
hub/supervisor.py
-----------------
Cortex Phase 9F — Hands-Free Native Runtime Lifecycle.

Coordinates:
- Lightweight persistent WakeListener ("Yo Cortex")
- Core Hub process management & readiness checking
- Local Device Agent process management & connection verification
- Explicit runtime states: DORMANT, STARTING, ACTIVE, IDLE, STOPPING, DEGRADED
- Deterministic sleep flow (runtime.sleep) preserving goals, waits, checkpoints, and autonomy safety
- Bounded crash recovery and idle timeout handling
- Per-user Windows login startup registration
- Single-instance enforcement
"""

import asyncio
from enum import Enum
import http.server
import json
from .config import get_hub_device_id
import logging
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, Any, Optional
import urllib.request
import urllib.error

# Setup directory paths
ROOT_DIR = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
CORTEX_STATE_DIR = Path(os.environ.get("CORTEX_STATE_DIR", str(Path.home() / ".cortex")))
CORTEX_STATE_DIR.mkdir(parents=True, exist_ok=True)

# Logger setup
log = logging.getLogger("cortex.supervisor")
if not log.handlers:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [SUPERVISOR] %(levelname)s - %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(LOGS_DIR / "supervisor.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


# ---------------------------------------------------------------------------
# Runtime States
# ---------------------------------------------------------------------------
class RuntimeState(str, Enum):
    DORMANT = "DORMANT"       # Only lightweight wake listener / supervisor alive
    STARTING = "STARTING"     # Hub, Agent, voice runtime starting up
    ACTIVE = "ACTIVE"         # Full Cortex runtime available
    IDLE = "IDLE"             # Runtime alive but no active interaction
    STOPPING = "STOPPING"     # Safely transitioning to dormant
    DEGRADED = "DEGRADED"     # Startup/runtime failure requiring intervention/recovery


# ---------------------------------------------------------------------------
# Lightweight Wake Listener
# ---------------------------------------------------------------------------
class WakeListener:
    """Lightweight persistent wake listener for 'Yo Cortex'."""

    def __init__(self, wake_word: str = "yo cortex", use_mock: bool = False):
        self.wake_word = wake_word.lower()
        self.use_mock = use_mock or (os.environ.get("USE_MOCK_VOICE") == "1") or (os.environ.get("CORTEX_MOCK_VOICE") == "1")
        self._provider = None
        self._running = False
        self._on_wake: Optional[Callable[[], None]] = None

    def start(self, on_wake: Callable[[], None]) -> None:
        if self._running:
            return
        self._on_wake = on_wake
        self._running = True

        if not self.use_mock:
            try:
                from agent.voice.real_providers import LocalWakeWordProvider
                self._provider = LocalWakeWordProvider(wake_word=self.wake_word)
                self._provider.start(self._handle_wake)
                log.info(f"LocalWakeWordProvider started listening for '{self.wake_word}'")
                return
            except Exception as exc:
                log.warning(f"Real wake word provider unavailable ({exc}), falling back to mock provider.")
                self.use_mock = True

        from agent.voice.mock_providers import MockWakeWordProvider
        self._provider = MockWakeWordProvider(wake_word=self.wake_word)
        self._provider.start(self._handle_wake)
        log.info(f"MockWakeWordProvider started listening for '{self.wake_word}'")

    def stop(self) -> None:
        self._running = False
        if self._provider:
            try:
                self._provider.stop()
            except Exception as e:
                log.debug(f"Error stopping wake provider: {e}")
            self._provider = None
        log.info("WakeListener stopped.")

    def _handle_wake(self) -> None:
        if not self._running:
            return
        log.info(f"[WakeListener] Detected wake phrase: '{self.wake_word}'")
        if self._on_wake:
            self._on_wake()

    def trigger(self) -> None:
        """Programmatic trigger for testing and external events."""
        log.info(f"[WakeListener] Programmatic wake triggered.")
        self._handle_wake()

    @property
    def is_running(self) -> bool:
        return self._running


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------
class Supervisor:
    """
    Supervises the native Cortex lifecycle without manual PowerShell tabs.
    Owns: WakeListener, Hub process, Agent process, lifecycle state, crash recovery.
    """

    _instance: Optional["Supervisor"] = None

    @classmethod
    def get_instance(cls, **kwargs) -> "Supervisor":
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        if cls._instance:
            try:
                cls._instance.shutdown()
            except Exception:
                pass
            cls._instance = None

    def __init__(
        self,
        hub_host: str = "127.0.0.1",
        hub_port: int = 8000,
        device_id: Optional[str] = None,
        ipc_port: int = 8008,
        use_mock_voice: bool = False,
        idle_timeout_minutes: float = 15.0,
        max_restarts: int = 3,
        env: Optional[Dict[str, str]] = None,
    ):
        self.hub_host = os.environ.get("CORTEX_HUB_HOST", hub_host)
        self.hub_port = int(os.environ.get("CORTEX_HUB_PORT", str(hub_port)))
        self.device_id = device_id or os.environ.get("CORTEX_DEVICE_ID") or get_hub_device_id()
        self.ipc_port = int(os.environ.get("CORTEX_SUPERVISOR_PORT", str(ipc_port)))
        self.use_mock_voice = use_mock_voice
        self.idle_timeout_seconds = idle_timeout_minutes * 60.0
        self.max_restarts = max_restarts
        self.custom_env = env or {}

        self.state: RuntimeState = RuntimeState.DORMANT
        self.hub_proc: Optional[subprocess.Popen] = None
        self.agent_proc: Optional[subprocess.Popen] = None
        self.wake_listener: WakeListener = WakeListener(wake_word="yo cortex", use_mock=use_mock_voice)

        self.hub_restarts = 0
        self.agent_restarts = 0
        self._start_time = time.monotonic()
        self._last_activity_time = time.monotonic()
        self._last_acknowledgment: Optional[str] = None
        self.state_file = CORTEX_STATE_DIR / f"runtime_state_{self.ipc_port}.json"
        self.lock_file = CORTEX_STATE_DIR / f"supervisor_{self.ipc_port}.lock"

        self._ipc_server: Optional[http.server.ThreadingHTTPServer] = None
        self._ipc_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stopping_event = threading.Event()
        self._lock = threading.Lock()

    # -----------------------------------------------------------------------
    # Single Instance Guard
    # -----------------------------------------------------------------------
    def acquire_single_instance(self) -> None:
        """Enforce single running supervisor instance via port check and lock file."""
        # 1. Check if supervisor HTTP server is already responding on ipc_port
        client_host = "127.0.0.1" if self.hub_host == "0.0.0.0" else self.hub_host
        try:
            req = urllib.request.Request(f"http://{client_host}:{self.ipc_port}/status")
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                if resp.status == 200:
                    raise RuntimeError(
                        f"Another supervisor instance is already active on port {self.ipc_port}"
                    )
        except Exception:
            pass  # not responding, good

        # 2. Check lock file
        if self.lock_file.exists():
            try:
                data = json.loads(self.lock_file.read_text(encoding="utf-8"))
                existing_pid = data.get("pid")
                existing_port = data.get("ipc_port")
                import psutil
                if existing_pid and psutil.pid_exists(existing_pid) and existing_port == self.ipc_port:
                    raise RuntimeError(
                        f"Another supervisor instance is already active on port {self.ipc_port} (PID {existing_pid})"
                    )
            except json.JSONDecodeError:
                pass

        # 3. Test socket binding
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE") and platform.system() == "Windows":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((client_host, self.ipc_port))
            s.close()
        except OSError as exc:
            s.close()
            raise RuntimeError(
                f"Another supervisor instance is already active on port {self.ipc_port}: {exc}"
            )

        # 4. Write lock file
        lock_data = {
            "pid": os.getpid(),
            "ipc_port": self.ipc_port,
            "hub_port": self.hub_port,
            "started_at": time.time(),
        }
        self.lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")

    def release_single_instance(self) -> None:
        if self.lock_file.exists():
            try:
                self.lock_file.unlink()
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # State Persistence & Notification
    # -----------------------------------------------------------------------
    def set_state(self, new_state: RuntimeState, acknowledgment: Optional[str] = None) -> None:
        with self._lock:
            old_state = self.state
            self.state = new_state
            if acknowledgment:
                self._last_acknowledgment = acknowledgment
            self._last_activity_time = time.monotonic()
            log.info(f"[LifecycleState] {old_state.value} -> {new_state.value}")
            self._persist_state()

    def _persist_state(self) -> None:
        try:
            status = {
                "state": self.state.value,
                "hub_pid": self.hub_proc.pid if (self.hub_proc and self.hub_proc.poll() is None) else None,
                "agent_pid": self.agent_proc.pid if (self.agent_proc and self.agent_proc.poll() is None) else None,
                "hub_ready": self.is_hub_ready(),
                "agent_connected": self.is_agent_connected(),
                "device_id": self.device_id,
                "hub_restarts": self.hub_restarts,
                "agent_restarts": self.agent_restarts,
                "last_acknowledgment": self._last_acknowledgment,
                "updated_at": time.time(),
            }
            self.state_file.write_text(json.dumps(status, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug(f"Could not persist runtime state: {e}")

    # -----------------------------------------------------------------------
    # Subprocess Management
    # -----------------------------------------------------------------------
    def _build_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["CORTEX_SUPERVISOR_PORT"] = str(self.ipc_port)
        env["CORTEX_HUB_PORT"] = str(self.hub_port)
        env["CORTEX_DEVICE_ID"] = self.device_id
        if self.use_mock_voice:
            env["USE_MOCK_VOICE"] = "1"
            env["CORTEX_MOCK_VOICE"] = "1"
        env.update(self.custom_env)
        return env

    def start_hub(self) -> bool:
        """Start Hub invisibly with logs redirected to logs/hub.log."""
        if self.hub_proc and self.hub_proc.poll() is None:
            log.info(f"Hub process already running (PID {self.hub_proc.pid})")
            return True

        hub_log_file = open(LOGS_DIR / "hub.log", "a", encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "hub.main:app",
            "--host",
            self.hub_host,
            "--port",
            str(self.hub_port),
        ]
        log.info(f"Starting Hub process: {' '.join(cmd)}")

        creationflags = 0
        if platform.system() == "Windows":
            creationflags = subprocess.CREATE_NO_WINDOW

        self.hub_proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            env=self._build_env(),
            stdout=hub_log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        log.info(f"Hub process launched (PID: {self.hub_proc.pid})")
        return True

    def stop_hub(self, timeout: float = 5.0) -> None:
        """Gracefully stop Hub process."""
        if not self.hub_proc:
            return
        if self.hub_proc.poll() is None:
            log.info(f"Stopping Hub (PID: {self.hub_proc.pid})...")
            try:
                self.hub_proc.terminate()
                self.hub_proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning(f"Hub did not terminate gracefully within {timeout}s; killing.")
                self.hub_proc.kill()
                self.hub_proc.wait()
            except Exception as e:
                log.debug(f"Error terminating Hub: {e}")
        self.hub_proc = None
        log.info("Hub stopped.")

    def is_hub_ready(self) -> bool:
        """Check if Hub reports status 200 and ready."""
        if not self.hub_proc or self.hub_proc.poll() is not None:
            return False
        client_host = "127.0.0.1" if self.hub_host == "0.0.0.0" else self.hub_host
        url = f"http://{client_host}:{self.hub_port}/health"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Cortex-Supervisor"})
            with urllib.request.urlopen(req, timeout=0.25) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("status") in ("running", "ready")
        except Exception:
            return False
        return False

    def wait_for_hub_ready(self, timeout: float = 20.0) -> bool:
        """Poll GET /health until Hub reports ready."""
        log.info(f"Waiting for Hub readiness on port {self.hub_port} (timeout {timeout}s)...")
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.hub_proc and self.hub_proc.poll() is not None:
                log.error(f"Hub process exited unexpectedly with code {self.hub_proc.returncode}")
                return False
            if self.is_hub_ready():
                log.info(f"Hub is READY on port {self.hub_port} ✓")
                return True
            time.sleep(0.3)
        log.error(f"Timed out waiting for Hub readiness after {timeout}s")
        return False

    def start_agent(self) -> bool:
        """Start local Agent invisibly with logs redirected to logs/agent.log."""
        if self.agent_proc and self.agent_proc.poll() is None:
            log.info(f"Agent process already running (PID {self.agent_proc.pid})")
            return True

        agent_log_file = open(LOGS_DIR / "agent.log", "a", encoding="utf-8")
        client_host = "127.0.0.1" if self.hub_host == "0.0.0.0" else self.hub_host
        hub_ws_url = f"ws://{client_host}:{self.hub_port}"
        cmd = [
            sys.executable,
            "-m",
            "agent.agent",
            "--hub",
            hub_ws_url,
            "--device",
            self.device_id,
            "--no-ui",
        ]
        log.info(f"Starting Agent process: {' '.join(cmd)}")

        creationflags = 0
        if platform.system() == "Windows":
            creationflags = subprocess.CREATE_NO_WINDOW

        self.agent_proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            env=self._build_env(),
            stdout=agent_log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        log.info(f"Agent process launched (PID: {self.agent_proc.pid})")
        return True

    def stop_agent(self, timeout: float = 5.0) -> None:
        """Gracefully stop Agent process."""
        if not self.agent_proc:
            return
        if self.agent_proc.poll() is None:
            log.info(f"Stopping Agent (PID: {self.agent_proc.pid})...")
            try:
                self.agent_proc.terminate()
                self.agent_proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning(f"Agent did not terminate gracefully within {timeout}s; killing.")
                self.agent_proc.kill()
                self.agent_proc.wait()
            except Exception as e:
                log.debug(f"Error terminating Agent: {e}")
        self.agent_proc = None
        log.info("Agent stopped.")

    def is_agent_connected(self) -> bool:
        """Check if local device is in Hub's connected_devices."""
        if not self.agent_proc or self.agent_proc.poll() is not None:
            return False
        if not self.hub_proc or self.hub_proc.poll() is not None:
            return False
        client_host = "127.0.0.1" if self.hub_host == "0.0.0.0" else self.hub_host
        url = f"http://{client_host}:{self.hub_port}/health"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Cortex-Supervisor"})
            with urllib.request.urlopen(req, timeout=0.25) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    connected = data.get("connected_devices", [])
                    return self.device_id in connected
        except Exception:
            return False
        return False

    def wait_for_agent_connected(self, timeout: float = 20.0) -> bool:
        """Poll GET /health until local Agent is connected."""
        log.info(f"Waiting for Agent '{self.device_id}' connection verification (timeout {timeout}s)...")
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.agent_proc and self.agent_proc.poll() is not None:
                log.error(f"Agent process exited unexpectedly with code {self.agent_proc.returncode}")
                return False
            if self.is_agent_connected():
                log.info(f"Agent '{self.device_id}' connected and verified ✓")
                return True
            time.sleep(0.3)
        log.error(f"Timed out waiting for Agent connection after {timeout}s")
        return False

    # -----------------------------------------------------------------------
    # Core Lifecycle Orchestration
    # -----------------------------------------------------------------------
    def wake(self) -> bool:
        """
        Handle 'Yo Cortex' wake event:
        DORMANT -> STARTING -> Hub READY -> Agent connected -> Voice acknowledgment -> ACTIVE.
        """
        if self.state in (RuntimeState.STARTING, RuntimeState.ACTIVE):
            log.info(f"Ignoring duplicate wake detection while state is {self.state.value}")
            return False

        log.info("Wake sequence initiated.")
        self.set_state(RuntimeState.STARTING)

        # Pause supervisor's wake listener during heavy runtime
        self.wake_listener.stop()

        # 1. Start Hub & wait for readiness
        if not self.start_hub() or not self.wait_for_hub_ready():
            log.error("Failed to start Hub or Hub not ready.")
            self.stop_hub()
            self.set_state(RuntimeState.DEGRADED)
            self.wake_listener.start(self.on_wake_detected)
            return False

        # 2. Start Agent & verify connection
        if not self.start_agent() or not self.wait_for_agent_connected():
            log.error("Failed to start Agent or Agent connection unverified.")
            self.stop_agent()
            self.stop_hub()
            self.set_state(RuntimeState.DEGRADED)
            self.wake_listener.start(self.on_wake_detected)
            return False

        # 3. Voice runtime active acknowledgment
        ack_text = "I'm awake."
        self.set_state(RuntimeState.ACTIVE, acknowledgment=ack_text)
        log.info(f"Cortex runtime is ACTIVE. Acknowledgment emitted: '{ack_text}'")
        return True

    def sleep(self) -> bool:
        """
        Handle 'Cortex, sleep' command:
        Safely stop heavy runtime (Agent & Hub), preserve goals/checkpoints/waits,
        and return to lightweight wake listener mode (DORMANT).
        """
        if self.state in (RuntimeState.DORMANT, RuntimeState.STOPPING):
            log.info(f"Sleep called while already {self.state.value}.")
            return True

        log.info("Sleep sequence initiated.")
        self.set_state(RuntimeState.STOPPING)

        # Stop local Agent first so it disconnects cleanly and releases audio
        self.stop_agent()

        # Stop Hub gracefully (SQLite state, goals, waits, checkpoints preserved)
        self.stop_hub()

        # Reset restart counters on clean sleep
        self.hub_restarts = 0
        self.agent_restarts = 0

        # Transition to DORMANT and resume lightweight wake listener
        self.set_state(RuntimeState.DORMANT)
        self.wake_listener.start(self.on_wake_detected)
        log.info("Cortex is DORMANT. Lightweight wake listener is listening for 'Yo Cortex'.")
        return True

    def restart(self) -> bool:
        """Restart runtime cleanly."""
        log.info("Restart sequence initiated.")
        self.sleep()
        time.sleep(0.5)
        return self.wake()

    def shutdown(self) -> None:
        """Terminate supervisor, heavy runtime, and wake listener completely."""
        log.info("Full shutdown initiated.")
        self._stopping_event.set()
        self.wake_listener.stop()
        self.stop_agent()
        self.stop_hub()
        self.set_state(RuntimeState.DORMANT)
        self.release_single_instance()
        if self._ipc_server:
            try:
                self._ipc_server.shutdown()
            except Exception:
                pass
        log.info("Supervisor completely shut down.")

    def on_wake_detected(self) -> None:
        """Callback from WakeListener."""
        threading.Thread(target=self.wake, daemon=True).start()

    def record_activity(self) -> None:
        """Record external activity to keep runtime active."""
        self._last_activity_time = time.monotonic()
        if self.state == RuntimeState.IDLE:
            self.set_state(RuntimeState.ACTIVE)

    # -----------------------------------------------------------------------
    # Background Crash Recovery & Idle Monitor
    # -----------------------------------------------------------------------
    def _monitor_loop(self) -> None:
        """Monitors subprocess health and idle timeouts."""
        while not self._stopping_event.is_set():
            time.sleep(0.5)
            if self.state == RuntimeState.ACTIVE:
                # 1. Check Hub crash
                if self.hub_proc and self.hub_proc.poll() is not None:
                    exit_code = self.hub_proc.returncode
                    log.error(f"[Crash] Hub process died unexpectedly (exit code {exit_code})")
                    self.hub_restarts += 1
                    if self.hub_restarts > self.max_restarts:
                        log.critical(f"Hub exceeded max restarts ({self.max_restarts}). State -> DEGRADED.")
                        self.stop_agent()
                        self.set_state(RuntimeState.DEGRADED)
                    else:
                        log.warning(f"Attempting Hub crash recovery (attempt {self.hub_restarts}/{self.max_restarts})...")
                        self.stop_agent()
                        self.start_hub()
                        if self.wait_for_hub_ready():
                            self.start_agent()
                            self.wait_for_agent_connected()
                        else:
                            self.set_state(RuntimeState.DEGRADED)

                # 2. Check Agent crash
                elif self.agent_proc and self.agent_proc.poll() is not None:
                    exit_code = self.agent_proc.returncode
                    log.error(f"[Crash] Agent process died unexpectedly (exit code {exit_code})")
                    self.agent_restarts += 1
                    if self.agent_restarts > self.max_restarts:
                        log.critical(f"Agent exceeded max restarts ({self.max_restarts}). State -> DEGRADED.")
                        self.set_state(RuntimeState.DEGRADED)
                    else:
                        log.warning(f"Attempting Agent crash recovery (attempt {self.agent_restarts}/{self.max_restarts})...")
                        self.start_agent()
                        if not self.wait_for_agent_connected():
                            self.set_state(RuntimeState.DEGRADED)

                # 3. Check Inactivity / Idle timeout
                inactive_sec = time.monotonic() - self._last_activity_time
                if inactive_sec >= self.idle_timeout_seconds:
                    log.info(f"Inactivity timeout reached ({inactive_sec:.0f}s >= {self.idle_timeout_seconds:.0f}s). State -> IDLE.")
                    self.set_state(RuntimeState.IDLE)

    # -----------------------------------------------------------------------
    # IPC HTTP Control Server
    # -----------------------------------------------------------------------
    def start_ipc_server(self) -> None:
        """Start local HTTP server on 127.0.0.1:{ipc_port} for CLI/voice control."""
        supervisor_ref = self

        class IPCHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # suppress standard access logs

            def _send_json(self, status_code: int, data: Dict[str, Any]):
                body = json.dumps(data).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/status", "/"):
                    status = {
                        "state": supervisor_ref.state.value,
                        "hub_pid": supervisor_ref.hub_proc.pid if (supervisor_ref.hub_proc and supervisor_ref.hub_proc.poll() is None) else None,
                        "agent_pid": supervisor_ref.agent_proc.pid if (supervisor_ref.agent_proc and supervisor_ref.agent_proc.poll() is None) else None,
                        "hub_ready": supervisor_ref.is_hub_ready(),
                        "agent_connected": supervisor_ref.is_agent_connected(),
                        "device_id": supervisor_ref.device_id,
                        "hub_restarts": supervisor_ref.hub_restarts,
                        "agent_restarts": supervisor_ref.agent_restarts,
                        "last_acknowledgment": supervisor_ref._last_acknowledgment,
                        "uptime_seconds": time.monotonic() - supervisor_ref._start_time,
                    }
                    self._send_json(200, status)
                else:
                    self._send_json(404, {"error": "Not found"})

            def do_POST(self):
                supervisor_ref.record_activity()
                if self.path in ("/wake", "/start"):
                    threading.Thread(target=supervisor_ref.wake, daemon=True).start()
                    self._send_json(200, {"status": "ok", "action": "wake"})
                elif self.path == "/sleep":
                    threading.Thread(target=supervisor_ref.sleep, daemon=True).start()
                    self._send_json(200, {"status": "ok", "action": "sleep"})
                elif self.path == "/restart":
                    threading.Thread(target=supervisor_ref.restart, daemon=True).start()
                    self._send_json(200, {"status": "ok", "action": "restart"})
                elif self.path == "/shutdown":
                    self._send_json(200, {"status": "ok", "action": "shutdown"})
                    threading.Thread(target=supervisor_ref.shutdown, daemon=True).start()
                elif self.path == "/trigger_wake":
                    supervisor_ref.wake_listener.trigger()
                    self._send_json(200, {"status": "ok", "action": "trigger_wake"})
                else:
                    self._send_json(404, {"error": "Unknown command"})

        class CustomThreadingHTTPServer(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                exc_type, exc_val, _ = sys.exc_info()
                if exc_type and issubclass(exc_type, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                    return
                super().handle_error(request, client_address)

        self._ipc_server = CustomThreadingHTTPServer(
            (self.hub_host, self.ipc_port), IPCHandler
        )
        self._ipc_thread = threading.Thread(
            target=self._ipc_server.serve_forever, daemon=True
        )
        self._ipc_thread.start()
        log.info(f"IPC HTTP server listening on http://{self.hub_host}:{self.ipc_port}")

    # -----------------------------------------------------------------------
    # Run Forever Loop (Main Entrypoint)
    # -----------------------------------------------------------------------
    def run_forever(self) -> None:
        """Start supervisor in DORMANT state and wait forever."""
        self.acquire_single_instance()
        self.start_ipc_server()
        self.set_state(RuntimeState.DORMANT)

        # Start wake listener
        self.wake_listener.start(self.on_wake_detected)

        # Start monitor thread
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

        log.info(f"Cortex Supervisor active in DORMANT state. Waiting for 'Yo Cortex'...")

        try:
            while not self._stopping_event.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            log.info("Interrupted by user.")
        finally:
            self.shutdown()


# ---------------------------------------------------------------------------
# Client Helper (For CLI cortex commands)
# ---------------------------------------------------------------------------
class SupervisorClient:
    """Helper client to interact with the running supervisor."""

    def __init__(self, port: Optional[int] = None):
        self.port = int(port or os.environ.get("CORTEX_SUPERVISOR_PORT", "8008"))
        self.base_url = f"http://127.0.0.1:{self.port}"

    def status(self) -> Optional[Dict[str, Any]]:
        try:
            req = urllib.request.Request(f"{self.base_url}/status")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        return None

    def start(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/start", data=b"{}", method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def sleep(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/sleep", data=b"{}", method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def restart(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/restart", data=b"{}", method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def shutdown(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/shutdown", data=b"{}", method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def trigger_wake(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/trigger_wake", data=b"{}", method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Windows Login Startup Registration
# ---------------------------------------------------------------------------
def register_windows_startup(background: bool = True) -> Dict[str, Any]:
    """
    Register the supervisor to launch automatically on user login without Administrator rights.
    Uses HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.
    """
    if platform.system() != "Windows":
        return {"status": "unsupported", "message": "Only supported on Windows"}

    python_exe = sys.executable
    if background:
        # Prefer pythonw.exe if available to suppress console window
        pythonw = python_exe.replace("python.exe", "pythonw.exe")
        if os.path.exists(pythonw):
            python_exe = pythonw

    cortex_script = str(ROOT_DIR / "cortex.py")
    cmd_str = f'"{python_exe}" "{cortex_script}" supervisor'

    # Try HKCU Run key (always accessible without admin privileges)
    try:
        reg_cmd = [
            "reg",
            "add",
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
            "/v",
            "CortexSupervisor",
            "/t",
            "REG_SZ",
            "/d",
            cmd_str,
            "/f",
        ]
        res = subprocess.run(reg_cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return {
                "status": "success",
                "method": "HKCU_Run",
                "command": cmd_str,
                "message": "Registered CortexSupervisor in user login registry (HKCU)",
            }
        else:
            return {"status": "error", "message": res.stderr.strip()}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def unregister_windows_startup() -> Dict[str, Any]:
    """Unregister the supervisor from user login."""
    if platform.system() != "Windows":
        return {"status": "unsupported", "message": "Only supported on Windows"}

    try:
        reg_cmd = [
            "reg",
            "delete",
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
            "/v",
            "CortexSupervisor",
            "/f",
        ]
        res = subprocess.run(reg_cmd, capture_output=True, text=True)
        return {
            "status": "success" if res.returncode == 0 else "not_found",
            "message": "Unregistered CortexSupervisor from user login registry",
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def is_windows_startup_registered() -> bool:
    """Check if supervisor is registered in HKCU Run."""
    if platform.system() != "Windows":
        return False
    try:
        query_cmd = [
            "reg",
            "query",
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
            "/v",
            "CortexSupervisor",
        ]
        res = subprocess.run(query_cmd, capture_output=True, text=True)
        return res.returncode == 0
    except Exception:
        return False
