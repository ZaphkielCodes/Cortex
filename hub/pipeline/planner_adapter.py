"""
hub/pipeline/planner_adapter.py
-------------------------------
Planner adapter stage for multi-step goals and plans (Section 6).
"""

import logging
from typing import Any, Dict, List, Optional

from ..capability_registry import CanonicalCapabilityRegistry
from ..goals import GoalManager
from ..planner import GoalPlanner
from .context_assembler import AssembledContext
from .semantic_interpreter import SemanticIntent

log = logging.getLogger("cortex.pipeline.planner_adapter")


class PipelinePlanner:
    """
    Constructs and validates multi-step plans using CanonicalCapabilityRegistry schemas.
    """

    @classmethod
    async def plan_goal(
        cls,
        intent: SemanticIntent,
        context: AssembledContext,
        control_layer: Any,
    ) -> Dict[str, Any]:
        planner = GoalPlanner.get_instance(control_layer=control_layer)
        cap_context = await planner.build_capability_context(target_device=context.source_device)

        plan = planner._reason_from_capabilities(
            request=intent.goal,
            capabilities=cap_context,
            target_device=context.source_device,
        )

        return plan
