"""Routing hints, never benchmark guarantees or natural-language classifiers."""
from dataclasses import dataclass, asdict
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class RoutingHints(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: Literal["general", "reasoning", "coding", "computer_use", "vision", "planning"] = "general"
    complexity: Literal["low", "medium", "high", "unknown"] = "unknown"
    requires_code_reasoning: bool = False
    requires_long_context: bool = False


@dataclass(frozen=True)
class TaskProfile:
    category: str = "general"
    complexity: str = "unknown"
    requires_tools: bool = False
    requires_computer_use: bool = False
    requires_code_reasoning: bool = False
    requires_long_context: bool = False
    requires_structured_output: bool = False
    requires_vision: bool = False
    context_tokens: int = 0
    latency_priority: float = .9
    background_allowed: bool = False
    privacy_class: str = "INTERNAL"
    target_device: str | None = None

    @classmethod
    def from_context(cls, context, *, privacy_floor="PUBLIC", context_tokens=0):
        from .privacy import PRIVACY_ORDER
        data = context.get("task_profile") or {}
        data = data if isinstance(data, dict) else {}
        category = data.get("category", context.get("routing_profile", "general"))
        if context.get("mode") == "planning" or context.get("force_task_orchestration"):
            category = "planning"
        if category not in RoutingHints.model_fields["category"].annotation.__args__:
            category = "general"
        complexity = data.get("complexity", "high" if category in {"reasoning", "planning"} else "unknown")
        if complexity not in {"low", "medium", "high", "unknown"}: complexity = "unknown"
        classes = [privacy_floor, context.get("privacy_class", "INTERNAL"), data.get("privacy_class", "PUBLIC")]
        privacy = max(classes, key=lambda p: PRIVACY_ORDER.get(p, 99))
        background = context.get("execution_mode") == "background" or data.get("background_allowed") is True
        latency = data.get("latency_priority", .15 if background else .9)
        if isinstance(latency, bool) or not isinstance(latency, (int,float)): latency = .9
        tokens = data.get("context_tokens", 0)
        tokens = tokens if type(tokens) is int and tokens >= 0 else 0
        return cls(category=category, complexity=complexity,
            requires_tools=bool(data.get("requires_tools") or data.get("requires_computer_use") or context.get("decision_schema") or category in {"planning","computer_use"}),
            requires_computer_use=bool(data.get("requires_computer_use") or category == "computer_use"),
            requires_code_reasoning=bool(data.get("requires_code_reasoning") or category == "coding"),
            requires_long_context=bool(data.get("requires_long_context")),
            requires_structured_output=bool(context.get("decision_schema") or context.get("mode") in {"agent_decision","planning","privacy_classification"}),
            requires_vision=bool(context.get("image_data_url") or category == "vision"),
            context_tokens=max(tokens,context_tokens), latency_priority=max(0.,min(1.,latency)),
            background_allowed=background, privacy_class=privacy,
            target_device=context.get("requested_device") or data.get("target_device"))

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    provider_id: str
    modalities: tuple = ("text",)
    tool_calling: bool = True  # Includes Cortex's validated structured decision protocol.
    structured_output: bool = True
    context_window: int | None = None  # Unknown is not an invented context limit.
    reasoning: float = .6
    coding: float = .6
    computer_use: float = .6
    tool_reliability: float = .7
    expected_latency: float | None = None
    interactive_suitability: float = .8
    background_suitability: float = .8
    configured_priority: float = 0.

    def __post_init__(self):
        import math
        if not self.model_id or len(self.model_id)>128 or not self.provider_id or len(self.provider_id)>64:
            raise ValueError("Invalid model identity")
        for value in (self.reasoning,self.coding,self.computer_use,self.tool_reliability,
                      self.interactive_suitability,self.background_suitability):
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("Routing strengths are bounded qualitative hints")
        if self.expected_latency is not None and (type(self.expected_latency) not in (int,float) or not math.isfinite(self.expected_latency) or not 0 < self.expected_latency <= 300):
            raise ValueError("Invalid latency hint")
        if self.context_window is not None and (type(self.context_window) is not int or not 0 < self.context_window <= 10_000_000):
            raise ValueError("Invalid context window")
        if not math.isfinite(self.configured_priority) or not -1 <= self.configured_priority <= 1:
            raise ValueError("Invalid configured preference")


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    configured: bool
    locality: str = "cloud_worker"
    max_privacy_class: str = "SENSITIVE"


# Qualitative, editable priors only. Runtime measurements supersede latency hints.
MODEL_HINTS = {
    "qwen/qwen3.8-27b": dict(reasoning=.7,coding=.7,computer_use=.85,expected_latency=2.),
    "openai/gpt-oss-120b": dict(reasoning=.98,coding=.85,computer_use=.85,expected_latency=4.),
    "deepseek-ai/deepseek-v4.1-flash": dict(reasoning=.95,coding=.8,computer_use=.7,expected_latency=12.,background_suitability=1.),
    "z-ai/glm-5.3": dict(reasoning=.92,coding=.99,computer_use=.7,expected_latency=15.,background_suitability=1.),
}


def model_profile(provider):
    hints = dict(MODEL_HINTS.get(provider.model, {}))
    if getattr(provider,"supports_vision",False): hints["modalities"] = ("text","image")
    return ModelProfile(provider.model,provider.name,**hints)


def request_timeout(context, default):
    """Honor the router's bounded per-attempt deadline at the transport too."""
    import math
    value=context.get("_router_timeout_seconds")
    return value if type(value) in (int,float) and math.isfinite(value) and 0 < value <= 120 else default
