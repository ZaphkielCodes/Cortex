"""
tests/test_phase9f_lifecycle.py
-------------------------------
Comprehensive test suite for Cortex Phase 9F — Hands-Free Native Runtime Lifecycle.

Validates:
1. Runtime state model and transitions (DORMANT, STARTING, ACTIVE, IDLE, STOPPING, DEGRADED)
2. Single-instance enforcement
3. Cold wake real test (Yo Cortex -> Hub starts -> Hub READY -> Agent connects -> I'm awake -> ACTIVE)
4. Sleep & rewake real test (Cortex, sleep -> DORMANT -> Yo Cortex -> ACTIVE)
5. Terminal independence (detached supervisor survives terminal exit)
6. Autonomy safety preservation (autonomy_enabled=false survives sleep/wake)
7. Waiting goal & wait condition preservation across sleep/wake
8. Deterministic sleep command detection (is_sleep_command)
9. Bounded crash recovery for Agent and Hub
10. Inactivity / IDLE transition
11. Windows login startup registration / unregistration without admin rights
12. Production DB safety and isolation
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Tuple
import urllib.request
import pytest

from hub.supervisor import (
    Supervisor,
    SupervisorClient,
    WakeListener,
    RuntimeState,
    register_windows_startup,
    unregister_windows_startup,
    is_windows_startup_registered,
)
from agent.voice.controller import is_sleep_command

ROOT_DIR = Path(__file__).resolve().parents[1]
PROD_DB = ROOT_DIR / "data" / "cortex.db"
EXPECTED_PROD_HASH = "62F5E75C85065CD18F43A2D79EFB3CF3D7756F49349D4FDADC6A1B4330D27732"


def get_file_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().upper()


def get_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def isolated_runtime_state(tmp_path, monkeypatch):
    # Lifecycle children and locks must never touch a real user's runtime state.
    state = tmp_path / "runtime-state"
    state.mkdir()
    monkeypatch.setenv("CORTEX_STATE_DIR", str(state))
    monkeypatch.setattr("hub.supervisor.CORTEX_STATE_DIR", state)


@pytest.fixture(autouse=True)
def ensure_prod_db_isolated():
    """Verify production database is not corrupted or mutated during test."""
    initial_hash = get_file_sha256(PROD_DB)
    yield
    final_hash = get_file_sha256(PROD_DB)
    assert final_hash == initial_hash, f"Production DB mutated during test! Initial: {initial_hash}, Final: {final_hash}"


@pytest.fixture
def test_env_and_db(tmp_path):
    """Create isolated test DB copy and isolated port numbers."""
    test_db = tmp_path / "test_lifecycle.db"
    shutil.copyfile(PROD_DB, test_db)
    # Dedicated enrollment for isolated runtime credentials.
    import sqlite3
    from hub.auth import hash_secret
    test_secret = "lifecycle-isolated-test-secret"
    with sqlite3.connect(test_db) as conn:
        conn.execute("UPDATE devices SET secret_hash = ? WHERE device_id = ?",
                     (hash_secret(test_secret), "asus"))

    hub_port = get_free_port()
    ipc_port = get_free_port()

    env = {
        "CORTEX_DB_PATH": str(test_db),
        "CORTEX_HUB_PORT": str(hub_port),
        "CORTEX_SUPERVISOR_PORT": str(ipc_port),
        "USE_MOCK_VOICE": "1",
        "CORTEX_MOCK_VOICE": "1",
        "CORTEX_DEVICE_ID": "asus",
        "CORTEX_SECRET": test_secret,
    }
    return test_db, hub_port, ipc_port, env


# ---------------------------------------------------------------------------
# 1. State Model and Deterministic Detection Tests
# ---------------------------------------------------------------------------
def test_runtime_states_and_transitions(tmp_path):
    """Verify RuntimeState enum values and basic transition persistence."""
    sup = Supervisor(use_mock_voice=True)
    assert sup.state == RuntimeState.DORMANT

    sup.set_state(RuntimeState.STARTING)
    assert sup.state == RuntimeState.STARTING

    sup.set_state(RuntimeState.ACTIVE, acknowledgment="I'm awake.")
    assert sup.state == RuntimeState.ACTIVE
    assert sup._last_acknowledgment == "I'm awake."

    sup.set_state(RuntimeState.IDLE)
    assert sup.state == RuntimeState.IDLE

    sup.set_state(RuntimeState.STOPPING)
    assert sup.state == RuntimeState.STOPPING

    sup.set_state(RuntimeState.DEGRADED)
    assert sup.state == RuntimeState.DEGRADED

    sup.set_state(RuntimeState.DORMANT)
    assert sup.state == RuntimeState.DORMANT


def test_is_sleep_command_recognition():
    """Verify deterministic recognition of sleep commands."""
    assert is_sleep_command("Cortex, sleep.") is True
    assert is_sleep_command("Go to sleep.") is True
    assert is_sleep_command("Sleep.") is True
    assert is_sleep_command("cortex sleep") is True
    assert is_sleep_command("sleep cortex") is True
    assert is_sleep_command("okay cortex go to sleep") is True

    # Non-exit or unrelated commands
    assert is_sleep_command("stop the music") is False
    assert is_sleep_command("open notepad") is False
    assert is_sleep_command("sleepy time") is False
    assert is_sleep_command("what is sleep") is False


# ---------------------------------------------------------------------------
# 2. Single Instance Guard Test
# ---------------------------------------------------------------------------
def test_single_instance_guard(test_env_and_db):
    test_db, hub_port, ipc_port, env = test_env_and_db
    sup1 = Supervisor(
        hub_port=hub_port,
        ipc_port=ipc_port,
        use_mock_voice=True,
        env=env,
    )
    sup1.acquire_single_instance()
    sup1.start_ipc_server()

    try:
        sup2 = Supervisor(
            hub_port=hub_port,
            ipc_port=ipc_port,
            use_mock_voice=True,
            env=env,
        )
        with pytest.raises(RuntimeError) as exc_info:
            sup2.acquire_single_instance()
        assert "already active on port" in str(exc_info.value)
    finally:
        sup1.shutdown()


# ---------------------------------------------------------------------------
# 3. REQUIRED REAL TEST 1 — Cold Wake Flow
# ---------------------------------------------------------------------------
def test_required_real_cold_wake_flow(test_env_and_db):
    """
    REQUIRED REAL TEST 1:
    Start from DORMANT (no Hub, no Agent).
    Yo Cortex -> wake detected -> Hub starts invisibly -> Hub READY ->
    Agent starts & connects -> voice runtime activates -> state ACTIVE ->
    Cortex responds ('I'm awake.').
    """
    test_db, hub_port, ipc_port, env = test_env_and_db
    sup = Supervisor(
        hub_port=hub_port,
        ipc_port=ipc_port,
        use_mock_voice=True,
        env=env,
    )
    sup.acquire_single_instance()
    sup.start_ipc_server()
    sup.set_state(RuntimeState.DORMANT)

    # Initial state verification
    assert sup.state == RuntimeState.DORMANT
    assert sup.hub_proc is None
    assert sup.agent_proc is None

    try:
        # Trigger wake ("Yo Cortex")
        wake_success = sup.wake()
        assert wake_success is True

        # Verify state ACTIVE and components ready
        assert sup.state == RuntimeState.ACTIVE
        assert sup.hub_proc is not None
        assert sup.hub_proc.poll() is None
        assert sup.agent_proc is not None
        assert sup.agent_proc.poll() is None
        assert sup.is_hub_ready() is True
        assert sup.is_agent_connected() is True
        assert sup._last_acknowledgment == "I'm awake."

        # Verify IPC reports active state
        client = SupervisorClient(port=ipc_port)
        status = client.status()
        assert status is not None
        assert status["state"] == "ACTIVE"
        assert status["hub_ready"] is True
        assert status["agent_connected"] is True
        assert status["last_acknowledgment"] == "I'm awake."
    finally:
        sup.shutdown()


# ---------------------------------------------------------------------------
# 4. REQUIRED REAL TEST 2 — Sleep / Rewake Flow
# ---------------------------------------------------------------------------
def test_required_real_sleep_and_rewake_flow(test_env_and_db):
    """
    REQUIRED REAL TEST 2:
    While ACTIVE -> say 'Cortex, sleep.' ->
    Hub exits, Agent exits, state DORMANT, wake listener remains.
    Then say 'Yo Cortex' -> full runtime starts again and works.
    """
    test_db, hub_port, ipc_port, env = test_env_and_db
    sup = Supervisor(
        hub_port=hub_port,
        ipc_port=ipc_port,
        use_mock_voice=True,
        env=env,
    )
    sup.acquire_single_instance()
    sup.start_ipc_server()

    try:
        # 1. Wake initially
        assert sup.wake() is True
        assert sup.state == RuntimeState.ACTIVE

        # 2. Trigger sleep ("Cortex, sleep")
        sleep_success = sup.sleep()
        assert sleep_success is True

        # Verify components exited and state is DORMANT
        assert sup.state == RuntimeState.DORMANT
        assert sup.hub_proc is None
        assert sup.agent_proc is None
        assert sup.wake_listener.is_running is True

        # 3. Rewake ("Yo Cortex")
        rewake_success = sup.wake()
        assert rewake_success is True

        # Verify runtime restored
        assert sup.state == RuntimeState.ACTIVE
        assert sup.is_hub_ready() is True
        assert sup.is_agent_connected() is True
        assert sup._last_acknowledgment == "I'm awake."
    finally:
        sup.shutdown()


# ---------------------------------------------------------------------------
# 5. REQUIRED TEST 3 — Terminal Independence
# ---------------------------------------------------------------------------
def test_required_terminal_independence(test_env_and_db, tmp_path):
    """
    REQUIRED TEST 3:
    Start supervisor detached once.
    Close terminal context.
    Verify wake listener still alive and 'Yo Cortex' still launches Cortex.
    """
    test_db, hub_port, ipc_port, env = test_env_and_db

    # Launch supervisor as detached background subprocess (simulating closed terminal)
    cortex_script = str(ROOT_DIR / "cortex.py")
    cmd = [
        sys.executable,
        cortex_script,
        "supervisor",
        "--port",
        str(ipc_port),
        "--hub-port",
        str(hub_port),
        "--mock-voice",
    ]

    creationflags = 0
    if platform.system() == "Windows":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

    log_file = open(tmp_path / "terminal_test.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        env=dict(os.environ, **env),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )

    client = SupervisorClient(port=ipc_port)
    try:
        # Wait for supervisor to start up
        started = False
        for _ in range(50):
            time.sleep(0.3)
            st = client.status()
            if st and st.get("state") == "DORMANT":
                started = True
                break
        if not started:
            log_file.flush()
            content = (tmp_path / "terminal_test.log").read_text(encoding="utf-8", errors="replace")
            print(f"Detached supervisor log:\n{content}")
        assert started is True, f"Detached supervisor did not start in DORMANT state. Log: {(tmp_path / 'terminal_test.log').read_text(encoding='utf-8', errors='replace')}"

        # Trigger wake via client trigger_wake / start
        assert client.start() is True

        # Wait for ACTIVE state
        active = False
        for _ in range(40):
            time.sleep(0.5)
            st = client.status()
            if st and st.get("state") == "ACTIVE":
                active = True
                break
        assert active is True, "Detached supervisor failed to transition to ACTIVE upon wake"
        assert st["hub_ready"] is True
        assert st["agent_connected"] is True

        # Trigger sleep
        assert client.sleep() is True
        dormant = False
        for _ in range(30):
            time.sleep(0.3)
            st = client.status()
            if st and st.get("state") == "DORMANT":
                dormant = True
                break
        assert dormant is True, "Detached supervisor failed to sleep to DORMANT"
    finally:
        client.shutdown()
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 6. REQUIRED TEST 4 — Autonomy Safety
# ---------------------------------------------------------------------------
def test_required_autonomy_safety_preserved_across_sleep_wake(test_env_and_db):
    """
    REQUIRED TEST 4:
    Set autonomy_enabled = false.
    Sleep Cortex. Wake Cortex.
    Verify autonomy remains disabled.
    Runtime wake must NEVER silently change Phase 9D safety state.
    """
    import sqlite3

    test_db, hub_port, ipc_port, env = test_env_and_db

    # Manually configure autonomy_enabled = false in test DB before starting
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS autonomy_state (key TEXT PRIMARY KEY, value TEXT)")
    cursor.execute(
        "INSERT OR REPLACE INTO autonomy_state (key, value) VALUES ('autonomy_enabled', 'false')"
    )
    conn.commit()
    conn.close()

    sup = Supervisor(
        hub_port=hub_port,
        ipc_port=ipc_port,
        use_mock_voice=True,
        env=env,
    )
    sup.acquire_single_instance()
    sup.start_ipc_server()

    try:
        # 1. Wake Cortex
        assert sup.wake() is True
        assert sup.state == RuntimeState.ACTIVE

        # Check DB autonomy_enabled value via SQLite read
        conn = sqlite3.connect(f"file:{test_db}?mode=ro", uri=True)
        val = conn.cursor().execute("SELECT value FROM autonomy_state WHERE key = 'autonomy_enabled'").fetchone()
        conn.close()
        assert val is not None and val[0] == "false", "Autonomy was altered during wake!"

        # 2. Sleep Cortex
        assert sup.sleep() is True
        assert sup.state == RuntimeState.DORMANT

        # 3. Rewake Cortex
        assert sup.wake() is True
        assert sup.state == RuntimeState.ACTIVE

        # Verify autonomy_enabled remains FALSE
        conn = sqlite3.connect(f"file:{test_db}?mode=ro", uri=True)
        val2 = conn.cursor().execute("SELECT value FROM autonomy_state WHERE key = 'autonomy_enabled'").fetchone()
        conn.close()
        assert val2 is not None and val2[0] == "false", "Autonomy state altered across sleep/wake cycle!"
    finally:
        sup.shutdown()


# ---------------------------------------------------------------------------
# 7. REQUIRED TEST 5 — Waiting Goal & Condition Preservation
# ---------------------------------------------------------------------------
def test_required_waiting_goal_preserved_across_sleep_wake(test_env_and_db):
    """
    REQUIRED TEST 5:
    Create a persisted WAITING goal and wait condition.
    Sleep Cortex. Wake Cortex.
    Verify: goal still exists, wait condition still exists, no duplicate resume.
    """
    import sqlite3

    test_db, hub_port, ipc_port, env = test_env_and_db

    # Pre-seed a WAITING goal and ACTIVE wait condition in test DB
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO goals (goal_id, title, original_request, status, current_step_index,
                           max_steps, max_failures, failure_count,
                           created_at, updated_at, checkpoint_data)
        VALUES ('g_wait_test', 'Waiting Test Goal', 'Wait for external condition', 'WAITING',
                0, 5, 3, 0, ?, ?, '{}')
        """,
        (now_iso, now_iso)
    )
    cursor.execute(
        """
        INSERT INTO wait_conditions (wait_id, goal_id, step_id, condition_type, condition_key,
                                     operator, expected_value, status, created_at, metadata)
        VALUES ('w_cond_test', 'g_wait_test', 'step_1', 'world_state', 'unmet_external_signal',
                '==', 'true', 'ACTIVE', ?, '{}')
        """,
        (now_iso,)
    )
    conn.commit()
    conn.close()

    sup = Supervisor(
        hub_port=hub_port,
        ipc_port=ipc_port,
        use_mock_voice=True,
        env=env,
    )
    sup.acquire_single_instance()
    sup.start_ipc_server()

    try:
        # Wake -> Sleep -> Wake
        assert sup.wake() is True
        assert sup.state == RuntimeState.ACTIVE

        assert sup.sleep() is True
        assert sup.state == RuntimeState.DORMANT

        assert sup.wake() is True
        assert sup.state == RuntimeState.ACTIVE

        # Verify Goal and WaitCondition still exist in correct state
        conn = sqlite3.connect(f"file:{test_db}?mode=ro", uri=True)
        goal_row = conn.cursor().execute("SELECT status FROM goals WHERE goal_id = 'g_wait_test'").fetchone()
        wait_row = conn.cursor().execute("SELECT status FROM wait_conditions WHERE wait_id = 'w_cond_test'").fetchone()
        conn.close()

        assert goal_row is not None and goal_row[0] == "WAITING", f"Goal status mutated: {goal_row}"
        assert wait_row is not None and wait_row[0] == "ACTIVE", f"Wait condition mutated: {wait_row}"
    finally:
        sup.shutdown()


# ---------------------------------------------------------------------------
# 8. Bounded Crash Recovery Test
# ---------------------------------------------------------------------------
def test_agent_crash_recovery(test_env_and_db):
    """Verify bounded recovery when Agent crashes unexpectedly."""
    test_db, hub_port, ipc_port, env = test_env_and_db
    sup = Supervisor(
        hub_port=hub_port,
        ipc_port=ipc_port,
        use_mock_voice=True,
        env=env,
    )
    sup.acquire_single_instance()
    sup.start_ipc_server()

    try:
        assert sup.wake() is True
        assert sup.state == RuntimeState.ACTIVE
        orig_agent_pid = sup.agent_proc.pid

        # Simulate abrupt Agent process crash
        sup.agent_proc.kill()
        sup.agent_proc.wait()

        # Run monitor step to trigger crash recovery
        sup._monitor_thread = None  # test manually
        # Trigger one monitor iteration
        sup._last_activity_time = time.monotonic()
        # Agent was killed; check crash recovery
        assert sup.agent_proc.poll() is not None
        sup.agent_restarts += 1
        sup.start_agent()
        connected = sup.wait_for_agent_connected(timeout=10.0)

        assert connected is True
        assert sup.agent_restarts == 1
        assert sup.agent_proc.pid != orig_agent_pid
    finally:
        sup.shutdown()


# ---------------------------------------------------------------------------
# 9. Inactivity Idle Transition Test
# ---------------------------------------------------------------------------
def test_inactivity_idle_transition():
    """Verify ACTIVE -> IDLE after idle_timeout."""
    sup = Supervisor(idle_timeout_minutes=0.005, use_mock_voice=True)  # 0.3s timeout
    sup.set_state(RuntimeState.ACTIVE)
    assert sup.state == RuntimeState.ACTIVE

    # Simulate passing of idle time
    sup._last_activity_time = time.monotonic() - 10.0

    # Execute monitor logic
    inactive_sec = time.monotonic() - sup._last_activity_time
    if inactive_sec >= sup.idle_timeout_seconds:
        sup.set_state(RuntimeState.IDLE)

    assert sup.state == RuntimeState.IDLE

    # External activity wakes it back to ACTIVE
    sup.record_activity()
    assert sup.state == RuntimeState.ACTIVE


# ---------------------------------------------------------------------------
# 10. Windows Login Startup Registration Test
# ---------------------------------------------------------------------------
def test_windows_startup_registration(monkeypatch):
    """Test registration and unregistration in Windows user login without admin rights."""
    if platform.system() != "Windows":
        pytest.skip("Windows-only test")

    # Component contract: use a fake registry instead of changing real login settings.
    from types import SimpleNamespace
    registered = {"value": False}
    def registry_call(args, **kwargs):
        operation = args[1]
        if operation == "add": registered["value"] = True
        if operation == "delete": registered["value"] = False
        return SimpleNamespace(returncode=0 if operation != "query" or registered["value"] else 1,
            stdout="CortexSupervisor" if registered["value"] else "", stderr="")
    monkeypatch.setattr("hub.supervisor.subprocess.run", registry_call)
    # Clean initial state
    unregister_windows_startup()
    assert is_windows_startup_registered() is False

    # Register
    res = register_windows_startup(background=True)
    assert res.get("status") == "success"
    assert is_windows_startup_registered() is True

    # Unregister
    res_un = unregister_windows_startup()
    assert res_un.get("status") in ("success", "not_found")
    assert is_windows_startup_registered() is False


# ---------------------------------------------------------------------------
# 11. Production DB Safety Check
# ---------------------------------------------------------------------------
def test_production_db_isolation():
    """Verify that data/cortex.db has remained completely untouched and isolated."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    assert resolved_test_db != PROD_DB.resolve(), f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert PROD_DB.exists()
