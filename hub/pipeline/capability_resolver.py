"""
hub/pipeline/capability_resolver.py
-----------------------------------
Resolves candidate capabilities against CanonicalCapabilityRegistry (Section 2 & 7).
Truthfully identifies missing capabilities.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..capability_registry import CanonicalCapabilityRegistry, RegisteredCapability
from .semantic_interpreter import SemanticIntent

log = logging.getLogger("cortex.pipeline.capability_resolver")


@dataclass
class ResolvedCapabilityPlan:
    """Capability resolution outcome."""
    is_supported: bool
    capability: Optional[RegisteredCapability] = None
    missing_reason: Optional[str] = None
    alternative_capabilities: List[str] = field(default_factory=list)


class CapabilityResolver:
    """
    Validates candidate capabilities against CanonicalCapabilityRegistry.
    Reports truthful capability gaps without hallucinating tools.
    """

    @classmethod
    def resolve_capability(
        cls,
        intent: SemanticIntent,
        target_device: str,
    ) -> ResolvedCapabilityPlan:
        # If intent explicitly reported a missing capability
        if intent.missing_capability:
            return ResolvedCapabilityPlan(
                is_supported=False,
                missing_reason=f"No capability registered for {intent.missing_capability}.",
            )

        if not intent.candidate_capabilities:
            # Check if this was a pure conversational or self-grounded intent
            if intent.type == "CONVERSATION" or intent.response_text:
                return ResolvedCapabilityPlan(is_supported=True)
            return ResolvedCapabilityPlan(
                is_supported=False,
                missing_reason=None,
            )

        reg = CanonicalCapabilityRegistry.get_instance()
        target_cap_name = intent.candidate_capabilities[0]
        cap = reg.get_capability(target_cap_name)

        if not cap:
            return ResolvedCapabilityPlan(
                is_supported=False,
                missing_reason=f"Capability '{target_cap_name}' is not registered in Cortex.",
            )

        if not cap.availability:
            return ResolvedCapabilityPlan(
                is_supported=False,
                capability=cap,
                missing_reason=f"Capability '{target_cap_name}' is currently unavailable on target environment.",
            )

        # Validate arguments schema if provided
        if intent.arguments:
            val_ok, val_reason = reg.validate_call(target_cap_name, intent.arguments, target_device=target_device)
            if not val_ok:
                log.warning(f"[CapabilityResolver] Schema warning on {target_cap_name}: {val_reason}")

        return ResolvedCapabilityPlan(
            is_supported=True,
            capability=cap,
        )
