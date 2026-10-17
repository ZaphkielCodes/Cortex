"""
scenario_lab/models.py
----------------------
Core data models for the Cortex Scenario Lab test harness.

Models:
- ScenarioSafetyRule: Isolation and approval policy constraints.
- ScenarioSetup: Sandbox layout, mock files, initial world state, signals.
- ScenarioExpectation: Deterministic evaluation targets (files, status, dispatches).
- ExecutionTrace: Comprehensive runtime record of all actions, approvals, replans.
- Scenario: Complete scenario definition (loads from YAML or JSON).
- ScenarioResult: Standardized result report for a scenario run.
"""

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import json


@dataclass
class ScenarioSafetyRule:
    """Safety and isolation policy constraints for a scenario run."""
    destructive_actions_require_approval: bool = True
    no_host_filesystem_access: bool = True
    allow_device_commands: List[str] = field(default_factory=list)
    max_allowed_runtime_seconds: Optional[float] = 60.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScenarioSafetyRule":
        return cls(
            destructive_actions_require_approval=data.get("destructive_actions_require_approval", True),
            no_host_filesystem_access=data.get("no_host_filesystem_access", True),
            allow_device_commands=data.get("allow_device_commands", []),
            max_allowed_runtime_seconds=data.get("max_allowed_runtime_seconds", 60.0),
        )


@dataclass
class ScenarioSetup:
    """Sandbox filesystem fixtures, initial state, signals and devices."""
    desktop: List[str] = field(default_factory=list)
    downloads: List[str] = field(default_factory=list)
    documents: List[str] = field(default_factory=list)
    connected_devices: List[str] = field(default_factory=lambda: ["asus"])
    device_capabilities: Dict[str, List[str]] = field(default_factory=dict)
    initial_signals: List[Dict[str, Any]] = field(default_factory=list)
    signals_to_inject: List[Dict[str, Any]] = field(default_factory=list)
    simulation: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScenarioSetup":
        return cls(
            desktop=data.get("desktop", []),
            downloads=data.get("downloads", []),
            documents=data.get("documents", []),
            connected_devices=data.get("connected_devices", ["asus"]),
            device_capabilities=data.get("device_capabilities", {}),
            initial_signals=data.get("initial_signals", []),
            signals_to_inject=data.get("signals_to_inject", []),
            simulation=data.get("simulation", {}),
        )


@dataclass
class ScenarioExpectation:
    """Deterministic assertions evaluated post-execution."""
    present: List[str] = field(default_factory=list)
    absent: List[str] = field(default_factory=list)
    final_goal_status: Optional[str] = None
    dispatched_commands: List[str] = field(default_factory=list)
    forbidden_commands: List[str] = field(default_factory=list)
    notifications_contain: List[str] = field(default_factory=list)
    approvals_expected: bool = False
    no_action_before_trigger: bool = False
    clarification_requested: bool = False
    survives_restart: bool = False
    no_duplicate_resume: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScenarioExpectation":
        return cls(
            present=data.get("present", []),
            absent=data.get("absent", []),
            final_goal_status=data.get("final_goal_status"),
            dispatched_commands=data.get("dispatched_commands", []),
            forbidden_commands=data.get("forbidden_commands", []),
            notifications_contain=data.get("notifications_contain", []),
            approvals_expected=data.get("approvals_expected", False),
            no_action_before_trigger=data.get("no_action_before_trigger", False),
            clarification_requested=data.get("clarification_requested", False),
            survives_restart=data.get("survives_restart", False),
            no_duplicate_resume=data.get("no_duplicate_resume", False),
        )


@dataclass
class ExecutionTrace:
    """Structured record of the entire execution lifecycle."""
    user_goal: str = ""
    generated_plan: List[Dict[str, Any]] = field(default_factory=list)
    selected_capabilities: List[str] = field(default_factory=list)
    approvals_requested: List[Dict[str, Any]] = field(default_factory=list)
    actions_dispatched: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)
    replans: int = 0
    waits: int = 0
    final_goal_state: str = "UNKNOWN"
    duration_seconds: float = 0.0
    step_count: int = 0
    checkpoint_preserved: bool = False
    paths_accessed: List[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Scenario:
    """A runnable scenario definition."""
    name: str
    description: str = ""
    goal: str = ""
    setup: ScenarioSetup = field(default_factory=ScenarioSetup)
    expected: ScenarioExpectation = field(default_factory=ScenarioExpectation)
    safety: ScenarioSafetyRule = field(default_factory=ScenarioSafetyRule)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "goal": self.goal,
            "setup": self.setup.to_dict(),
            "expected": self.expected.to_dict(),
            "safety": self.safety.to_dict(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Scenario":
        return cls(
            name=data.get("name", "unnamed_scenario"),
            description=data.get("description", ""),
            goal=data.get("goal", ""),
            setup=ScenarioSetup.from_dict(data.get("setup", {})),
            expected=ScenarioExpectation.from_dict(data.get("expected", {})),
            safety=ScenarioSafetyRule.from_dict(data.get("safety", {})),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "Scenario":
        p = Path(path)
        content = p.read_text(encoding="utf-8")
        if p.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml
                data = yaml.safe_load(content)
            except ImportError:
                # Fallback simple json if yaml not installed
                data = json.loads(content)
        else:
            data = json.loads(content)
        return cls.from_dict(data)


@dataclass
class ScenarioResult:
    """The result of a scenario execution and evaluation."""
    scenario_name: str
    result: str  # PASS / FAIL / BLOCKED
    goal_state: str
    steps_planned: int = 0
    steps_executed: int = 0
    approvals: int = 0
    replans: int = 0
    waits: int = 0
    safety_violations: List[str] = field(default_factory=list)
    failed_assertions: List[str] = field(default_factory=list)
    duration: float = 0.0
    trace: Optional[ExecutionTrace] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.trace:
            d["trace"] = self.trace.to_dict()
        return d
