"""
hub/pipeline/result_observer.py
-------------------------------
Observes execution outcomes, updates session references, and tracks world state (Section 1).
"""

import logging
from typing import Any, Dict, Optional

from ..world_state import WorldStateManager
from .context_assembler import AssembledContext
from .semantic_interpreter import SemanticIntent

log = logging.getLogger("cortex.pipeline.result_observer")


class ResultObserver:
    """
    Observes execution results and updates system state and session references.
    """

    @classmethod
    def observe(
        cls,
        intent: SemanticIntent,
        raw_result: Dict[str, Any],
        context: AssembledContext,
    ) -> None:
        action = intent.candidate_capabilities[0] if intent.candidate_capabilities else None
        session = context.raw_context.setdefault("session", {})
        refs = session.setdefault("references", {})

        wsm = WorldStateManager.get_instance()

        if action == "app.close":
            closed_app = intent.arguments.get("app")
            if closed_app:
                refs["last_closed_app"] = closed_app
                refs["last_app"] = closed_app

        elif action in ("app.launch", "window.focus"):
            launched_app = intent.arguments.get("app") or intent.arguments.get("title")
            if launched_app:
                refs["last_app"] = launched_app
                refs["last_confirmed_app"] = launched_app
                wsm.record_foreground_switch(launched_app, device_id=context.source_device)

        elif action and action.startswith("fs."):
            if action == "fs.copy":
                refs["last_file"] = intent.arguments.get("src")
                refs["last_copied_dst"] = intent.arguments.get("dst")
                refs["last_copied_file"] = intent.arguments.get("dst")
            elif action == "fs.rename":
                refs["last_file"] = intent.arguments.get("new_name")
                refs["last_copied_dst"] = intent.arguments.get("new_name")
            elif action == "fs.move":
                refs["last_file"] = intent.arguments.get("dst")
            else:
                path = intent.arguments.get("path") or intent.arguments.get("dst") or intent.arguments.get("src")
                if path:
                    refs["last_file"] = path

        elif action and action.startswith("ui."):
            tgt = intent.arguments.get("target") or raw_result.get("target") or raw_result.get("target_label")
            if tgt:
                refs["last_ui_element"] = tgt
            obs_id = raw_result.get("observation_id") or intent.arguments.get("observation_id")
            if obs_id:
                refs["last_observation_id"] = obs_id
            refs["last_ui_action"] = action
