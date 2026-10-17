"""
hub/pipeline/target_grounder.py
-------------------------------
Grounds structured targets after semantic interpretation (Section 4).
"""

import logging
from typing import Any, Dict, Optional

from ..target_resolver import ResolvedTarget, TargetResolver
from .context_assembler import AssembledContext
from .semantic_interpreter import SemanticIntent

log = logging.getLogger("cortex.pipeline.target_grounder")


class TargetGrounder:
    """
    Grounds target references (e.g. 'previous_app', 'current_app', 'it', 'Chrome')
    against world state, device registry, and session context.
    """

    @classmethod
    def ground(
        cls,
        intent: SemanticIntent,
        context: AssembledContext,
    ) -> ResolvedTarget:
        operation = intent.candidate_capabilities[0] if intent.candidate_capabilities else None
        target_ref = intent.target_reference

        session_ctx = dict(context.raw_context.get("session") or context.raw_context or {})
        world_st = dict(context.raw_context.get("world_state") or {})
        if context.foreground_app:
            session_ctx["foreground_app"] = context.foreground_app
            session_ctx.setdefault("references", {})["foreground_app"] = context.foreground_app
            world_st.setdefault("foreground_app", context.foreground_app)
            world_st.setdefault("foreground_activity", {})["app_name"] = context.foreground_app

        raw_req = context.raw_context.get("natural_request") or (f"{operation} {target_ref}" if target_ref else intent.goal)
        grounded = TargetResolver.ground_reference(
            target_reference=target_ref,
            operation=operation,
            session_context=session_ctx,
            world_state=world_st,
            current_device=context.source_device,
            raw_request=raw_req,
        )

        # Merge grounded target into intent arguments
        if grounded.target_name:
            if operation in ("app.close", "app.launch"):
                intent.arguments["app"] = grounded.target_name
            elif operation in ("window.focus", "window.minimize", "window.maximize", "window.restore"):
                intent.arguments["title"] = grounded.target_name
                intent.arguments["app"] = grounded.target_name
            elif operation and operation.startswith("fs."):
                if grounded.resolved_path:
                    intent.arguments["path"] = grounded.resolved_path
                elif "path" not in intent.arguments:
                    intent.arguments["path"] = grounded.target_name
            elif operation and operation.startswith("ui."):
                if "target" not in intent.arguments:
                    intent.arguments["target"] = grounded.target_name

        if grounded.target_device and grounded.target_device not in ("current", context.source_device):
            intent.arguments["target_device"] = grounded.target_device
        elif intent.arguments.get("target_device") and intent.arguments["target_device"] not in ("current", context.source_device):
            grounded.target_device = intent.arguments["target_device"]

        return grounded
