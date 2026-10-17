"""
hub/pipeline/runtime.py
-----------------------
Lightweight dependency container for Cortex Hub runtime services (Section 9).
"""

from typing import Any, Optional


class CortexRuntime:
    """
    Explicit dependency container connecting core Hub services without
    heavy DI frameworks or mutable circular global imports.
    """

    _instance: Optional["CortexRuntime"] = None

    def __init__(
        self,
        database: Any = None,
        capability_registry: Any = None,
        control_layer: Any = None,
        executor_registry: Any = None,
        privacy_engine: Any = None,
        planner: Any = None,
        task_orchestrator: Any = None,
        session_manager: Any = None,
        world_state_manager: Any = None,
        event_bus: Any = None,
    ):
        self.database = database
        self.capability_registry = capability_registry
        self.control_layer = control_layer
        self.executor_registry = executor_registry
        self.privacy_engine = privacy_engine
        self.planner = planner
        self.task_orchestrator = task_orchestrator
        self.session_manager = session_manager
        self.world_state_manager = world_state_manager
        self.event_bus = event_bus

    @classmethod
    def get_instance(cls) -> "CortexRuntime":
        if cls._instance is None:
            cls._instance = cls.create_default()
        return cls._instance

    @classmethod
    def set_instance(cls, runtime: "CortexRuntime") -> None:
        cls._instance = runtime

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    @classmethod
    def create_default(cls, control_layer: Any = None) -> "CortexRuntime":
        """Instantiate a runtime with standard singletons."""
        from ..capability_registry import CanonicalCapabilityRegistry
        from ..executor_registry import ExecutorRegistry
        from ..privacy import PrivacyPolicyEngine
        from ..world_state import WorldStateManager
        from .event_bus import EventBus

        reg = CanonicalCapabilityRegistry.get_instance()
        er = ExecutorRegistry.get_instance()
        pe = PrivacyPolicyEngine.get_instance()
        wsm = WorldStateManager.get_instance()
        bus = EventBus.get_instance()

        planner = None
        task_orch = None
        if control_layer is not None:
            from ..planner import GoalPlanner
            from ..task_orchestrator import TaskOrchestrator
            from ..model_router import get_model_router
            router = get_model_router()
            planner = GoalPlanner.get_instance(control_layer=control_layer)
            task_orch = TaskOrchestrator(router, control_layer)

        return cls(
            database=None,
            capability_registry=reg,
            control_layer=control_layer,
            executor_registry=er,
            privacy_engine=pe,
            planner=planner,
            task_orchestrator=task_orch,
            session_manager=None,
            world_state_manager=wsm,
            event_bus=bus,
        )
