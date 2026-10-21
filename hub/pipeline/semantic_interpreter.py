"""
hub/pipeline/semantic_interpreter.py
------------------------------------
General Semantic Interpreter for ordinary requests (Section 5).
Maps user goals to structured intents over live capability schemas.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


log = logging.getLogger("cortex.pipeline.semantic_interpreter")


@dataclass
class SemanticIntent:
    """Structured semantic interpretation output according to Section 5."""
    type: str  # "ACTION" | "QUESTION" | "CONVERSATION" | "GOAL"
    goal: str
    required_effect: str
    candidate_capabilities: List[str] = field(default_factory=list)
    target_reference: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)
    needs_observation: bool = False
    confidence: float = 1.0
    missing_capability: Optional[str] = None
    response_text: Optional[str] = None
    is_conversational_followup: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "goal": self.goal,
            "required_effect": self.required_effect,
            "candidate_capabilities": self.candidate_capabilities,
            "target_reference": self.target_reference,
            "arguments": self.arguments,
            "constraints": self.constraints,
            "needs_observation": self.needs_observation,
            "confidence": self.confidence,
            "missing_capability": self.missing_capability,
            "response_text": self.response_text,
            "is_conversational_followup": self.is_conversational_followup,
        }


class SemanticInterpreter:
    """Model-only adapter for diagnostic callers. Production uses AgentTurn directly."""
    @classmethod
    async def interpret(cls, norm, context, model_router=None):
        if model_router is None:
            return SemanticIntent(type="CONVERSATION", goal=getattr(norm, "raw_text", str(norm)),
                required_effect="reasoning_unavailable", missing_capability="agent.reason",
                response_text="A reasoning model is required to interpret this request.")
        from ..agent_turn import AgentTurn, _get_model_decision, _build_capability_catalog, _build_conversation_history
        raw = context if isinstance(context, dict) else getattr(context, "raw_context", {})
        if not AgentTurn(None, model_router)._model_privacy(raw).is_allowed:
            raise ValueError("Interpretation requires privacy authorization")
        source = raw.get("device", {}).get("id") or getattr(context, "source_device", "unknown")
        text = getattr(norm, "raw_text", str(norm))
        catalog = await _build_capability_catalog(source)
        decision = await _get_model_decision(text, catalog, {}, _build_conversation_history(raw), model_router,
                                             allow_provider_fallback=False)
        return SemanticIntent(type="ACTION" if decision.capability else "CONVERSATION", goal=decision.goal or text,
            required_effect=decision.goal, candidate_capabilities=[decision.capability] if decision.capability else [],
            arguments=decision.arguments, response_text=decision.response_text, confidence=decision.confidence)
