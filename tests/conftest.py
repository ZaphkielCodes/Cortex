import pytest
import pytest_asyncio
import os
import asyncio
from pathlib import Path

# Important: set env var BEFORE any imports of hub.database!
_TEST_DB_PATH = Path(__file__).parent / "test_cortex.db"
os.environ["CORTEX_DB_PATH"] = str(_TEST_DB_PATH)

def check_db_safety(db_path: Path):
    path_str = str(db_path).replace('\\', '/')
    prod_path = (Path(__file__).parent.parent / "data" / "cortex.db").resolve()
    resolved = Path(db_path).resolve()
    if path_str.endswith("data/cortex.db") or db_path.name == "cortex.db" or resolved == prod_path:
        raise RuntimeError(f"FATAL SAFETY GUARD: Attempted to delete or mutate production DB at {db_path}!")

@pytest_asyncio.fixture(autouse=True, scope="function")
async def isolate_db():
    import hub.database as database
    
    # Close any lingering connections from previous tests before unlinking
    try:
        await database.close_db()
    except Exception:
        pass
        
    import hub.task_orchestrator as to
    to._active_tasks.clear()
    to._device_active_task.clear()
    to._background_tasks.clear()

    from hub.session_context import SessionContextManager
    SessionContextManager._memory_cache.clear()

    try:
        import hub.main
        hub.main.CONNECTED_DEVICES.clear()
    except Exception:
        pass

    try:
        from hub.world_state import WorldStateManager
        WorldStateManager.reset()
    except Exception:
        pass

    try:
        from hub.proactive_engine import ProactiveEngine
        ProactiveEngine.reset()
    except Exception:
        pass

    try:
        from hub.attention_router import AttentionRouter
        AttentionRouter.reset()
    except Exception:
        pass

    try:
        from hub.workload import WorkloadManager
        WorkloadManager.reset()
    except Exception:
        pass

    try:
        from hub.routines import RoutineManager
        RoutineManager.reset()
    except Exception:
        pass

    try:
        from hub.goals import GoalManager, GoalRunner, GoalPlanner
        GoalManager.reset()
        GoalRunner.reset()
        GoalPlanner.reset()
    except Exception:
        pass

    try:
        from hub.search_provider import set_search_provider_override
        set_search_provider_override(None)
    except Exception:
        pass

    try:
        from hub.weather_provider import set_weather_provider_override
        set_weather_provider_override(None)
    except Exception:
        pass

    try:
        from hub.vision_provider import set_vision_provider_override
        set_vision_provider_override(None)
    except Exception:
        pass

    try:
        from hub.screen_perception import ScreenPerceptionManager
        ScreenPerceptionManager.reset()
    except Exception:
        pass

    try:
        from hub.system_registry import SystemRegistry
        SystemRegistry.reset()
    except Exception:
        pass

    try:
        from hub.executor_registry import ExecutorRegistry
        ExecutorRegistry.reset()
    except Exception:
        pass

    try:
        from hub.task_ownership import TaskOwnershipManager
        TaskOwnershipManager.reset()
    except Exception:
        pass

    try:
        from hub.privacy import PrivacyPolicyEngine
        PrivacyPolicyEngine.reset()
    except Exception:
        pass

    try:
        from hub.file_transfer import FileTransferManager
        FileTransferManager.reset()
    except Exception:
        pass

    check_db_safety(_TEST_DB_PATH)
    # Ensure it's clean before test
    for suffix in ("", "-wal", "-shm"):
        target_f = Path(str(_TEST_DB_PATH) + suffix)
        if target_f.exists():
            success = False
            for _ in range(40):
                try:
                    target_f.unlink()
                    success = True
                    break
                except PermissionError:
                    await asyncio.sleep(0.1)
    
    import hub.database as database
    # Enforce it just in case
    database.DB_PATH = _TEST_DB_PATH

    await database.init_db()
    
    yield
    
    # Cleanup after test
    try:
        await database.close_db()
    except Exception:
        pass

    for suffix in ("", "-wal", "-shm"):
        target_f = Path(str(_TEST_DB_PATH) + suffix)
        if target_f.exists():
            for _ in range(40):
                try:
                    target_f.unlink()
                    break
                except PermissionError:
                    await asyncio.sleep(0.1)


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    """Offline regressions allow loopback only; accidental cloud calls fail the test."""
    import socket
    import ipaddress
    attempted = []
    original_lookup = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check(host):
        if host is None:
            return
        if isinstance(host, bytes):
            host = host.decode("ascii")
        if str(host).casefold() == "localhost":
            return
        try:
            if ipaddress.ip_address(str(host).split("%",1)[0]).is_loopback:
                return
        except ValueError:
            pass
        attempted.append(str(host))
        raise AssertionError("Offline regression attempted external network access")

    def lookup(host, *args, **kwargs):
        try:
            ipaddress.ip_address(host.decode("ascii") if isinstance(host,bytes) else str(host))
        except ValueError:
            check(host)
        # Numeric address parsing performs no DNS or connection; SSRF checks need it.
        return original_lookup(host, *args, **kwargs)

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return original_connect_ex(sock, address)

    from hub.world_state import DeviceSignalAdapter
    monkeypatch.setattr(DeviceSignalAdapter,"inspect_network_state",staticmethod(
        lambda: {"network_state":"offline","is_connected":False}))
    monkeypatch.setattr(socket,"getaddrinfo",lookup)
    monkeypatch.setattr(socket.socket,"connect",connect)
    monkeypatch.setattr(socket.socket,"connect_ex",connect_ex)
    yield
    assert not attempted, "A test attempted external networking; supply an explicit provider/transport fixture"
