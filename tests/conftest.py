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
