"""
hub/planner.py
--------------
Phase 9E: Capability-Aware Planning Foundation.

Responsibilities:
- Build compact machine-readable lists of valid, available capabilities (CapabilitySchema)
- Reason over high-level user goals using only real capabilities Cortex actually has
- Strictly validate structured plans (capability existence, target availability, required args, no unknown args, risk metadata, max_steps bound)
- Persist validated plans through GoalManager
- Replan minimally after an observed step failure, preserving completed steps and checkpoints within bounded replan limits (max_replans)
- Cleanly block goals when capabilities are missing, without hallucinating tools

Architecture Rule:
GoalPlanner reasons. GoalManager persists state. GoalRunner executes.
Do not merge these responsibilities.
"""

import json
import logging
from typing import Optional, Dict, Any, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .goals import Goal, GoalStep, GoalManager

from .database import (
    get_device_capabilities,
)
from .control_layer import CortexControlLayer
from .llm_provider import get_llm_provider
from .config import get_hub_device_id
from .intent_schema import ModelResult

log = logging.getLogger("cortex.planner")


# ---------------------------------------------------------------------------
# Capability Schema Model
# ---------------------------------------------------------------------------

from .capability_registry import (
    CanonicalCapabilityRegistry,
    CapabilitySchema,
)


class _CapabilitySchemaDict(dict):
    """Proxy dictionary that always delegates to CanonicalCapabilityRegistry to eliminate divergence."""

    def __getitem__(self, key):
        return CanonicalCapabilityRegistry.get_instance().get_all()[key]

    def __iter__(self):
        return iter(CanonicalCapabilityRegistry.get_instance().get_all())

    def __len__(self):
        return len(CanonicalCapabilityRegistry.get_instance().get_all())

    def items(self):
        return CanonicalCapabilityRegistry.get_instance().get_all().items()

    def values(self):
        return CanonicalCapabilityRegistry.get_instance().get_all().values()

    def keys(self):
        return CanonicalCapabilityRegistry.get_instance().get_all().keys()

    def get(self, key, default=None):
        return CanonicalCapabilityRegistry.get_instance().get_all().get(key, default)

    def __contains__(self, key):
        return key in CanonicalCapabilityRegistry.get_instance().get_all()


BUILTIN_CAPABILITY_SCHEMAS: Dict[str, CapabilitySchema] = _CapabilitySchemaDict()



# ---------------------------------------------------------------------------
# Goal Planner
# ---------------------------------------------------------------------------

class GoalPlanner:
    BUILTIN_CAPABILITY_SCHEMAS = BUILTIN_CAPABILITY_SCHEMAS
    """
    Capability-aware reasoning layer for Cortex Goals.
    Converts high-level goals into validated GoalSteps using only existing capabilities.
    """

    _instance: Optional["GoalPlanner"] = None

    def __init__(
        self,
        goal_manager: Optional[Any] = None,
        control_layer: Optional[CortexControlLayer] = None,
        model_router: Optional[Any] = None,
    ):
        if goal_manager is None:
            from .goals import GoalManager
            goal_manager = GoalManager.get_instance()
        self._goal_manager = goal_manager
        self._control_layer = control_layer
        self._model_router = model_router

    @classmethod
    def get_instance(
        cls,
        goal_manager: Optional[Any] = None,
        control_layer: Optional[CortexControlLayer] = None,
        model_router: Optional[Any] = None,
    ) -> "GoalPlanner":
        if cls._instance is None:
            cls._instance = cls(
                goal_manager=goal_manager,
                control_layer=control_layer,
                model_router=model_router,
            )
        else:
            if goal_manager is not None:
                cls._instance._goal_manager = goal_manager
            if control_layer is not None:
                cls._instance._control_layer = control_layer
            if model_router is not None:
                cls._instance._model_router = model_router
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton instance (used for test isolation)."""
        cls._instance = None

    async def build_capability_context(
        self, target_device: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Build a compact machine-readable list of available capabilities.
        Include only capabilities currently valid for the target/runtime.
        Exposes structured metadata: name, description, arguments/schema,
        target type, permission tier/risk, read/write/destructive classification, availability.
        """
        target_caps: Optional[List[str]] = None
        if target_device:
            try:
                caps = await get_device_capabilities(target_device)
                if caps:
                    target_caps = caps
            except Exception:
                target_caps = None

        context_list: List[Dict[str, Any]] = []
        from .search_provider import get_search_provider
        from .weather_provider import get_weather_provider
        from .vision_provider import get_vision_provider

        search_avail = (get_search_provider() is not None)
        weather_avail = (get_weather_provider() is not None)
        _vp = get_vision_provider()
        vision_avail = (_vp is not None and getattr(_vp, "is_available", lambda: True)())

        from .executor_registry import ExecutorRegistry
        er = ExecutorRegistry.get_instance()
        handles = er.list_device_handles()

        for name, schema in BUILTIN_CAPABILITY_SCHEMAS.items():
            is_available = True
            dev_avail: Dict[str, bool] = {}
            if schema.target_type == "device":
                if handles:
                    dev_avail = {h.device_id: h.supports_capability(name) for h in handles}
                elif target_caps is not None:
                    dev_avail = {target_device: (name in target_caps)}

            if schema.target_type == "device" and target_caps is not None:
                is_available = name in target_caps
            elif schema.target_type == "device" and handles:
                is_available = any(dev_avail.values())
            elif name == "web.search":
                is_available = search_avail
            elif name in ("weather.current", "weather.forecast"):
                is_available = weather_avail
            elif name == "screen.analyze":
                is_available = vision_avail

            # Only include capabilities currently valid for the target/runtime
            if is_available:
                cap_dict = schema.to_dict()
                cap_dict["availability"] = True
                cap_dict["device_availability"] = dev_avail
                context_list.append(cap_dict)

        return context_list

    def validate_plan(
        self,
        plan: Dict[str, Any],
        capability_context: List[Dict[str, Any]],
        goal: Goal,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate every step before persistence:
        - capability exists
        - capability available on target
        - required arguments present
        - no unknown arguments if schema is strict
        - permission/risk metadata known
        - step count <= goal.max_steps
        Returns (is_valid, error_reason).
        """
        if not isinstance(plan, dict) or "steps" not in plan:
            return False, "Plan must be a dictionary containing a 'steps' list."

        steps = plan.get("steps")
        if not isinstance(steps, list):
            return False, "'steps' field must be a list."
        if not steps:
            return False, "Plan cannot have zero steps."
        if len(steps) > goal.max_steps:
            return (
                False,
                f"Plan step count ({len(steps)}) exceeds maximum allowed steps ({goal.max_steps}).",
            )

        available_map = {
            c["name"]: c for c in capability_context if c.get("availability", True)
        }

        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                return False, f"Step {idx} must be a dictionary."

            cap_name = step.get("capability") or step.get("action")
            if not cap_name or not isinstance(cap_name, str):
                return False, f"Step {idx} is missing a valid capability name."

            # 1. Capability exists & available on target
            if cap_name not in available_map:
                return (
                    False,
                    f"Capability '{cap_name}' does not exist or is not available on target.",
                )

            schema = available_map[cap_name]
            step_args = step.get("arguments")
            if step_args is None:
                step_args = {}
            if not isinstance(step_args, dict):
                return False, f"Step {idx} arguments must be a dictionary."

            # Executor-aware device validation
            target_dev = step_args.get("target_device") or step.get("target_device")
            dev_avail = schema.get("device_availability", {})
            if target_dev and target_dev in dev_avail and not dev_avail[target_dev]:
                return (
                    False,
                    f"Capability '{cap_name}' is not supported or available on device '{target_dev}'.",
                )

            # 2. Required arguments present
            arg_schemas = schema.get("arguments", {})
            for arg_k, arg_def in arg_schemas.items():
                if arg_def.get("required", False):
                    if (
                        arg_k not in step_args
                        or step_args[arg_k] is None
                        or step_args[arg_k] == ""
                    ):
                        return (
                            False,
                            f"Step {idx} ('{cap_name}') is missing required argument '{arg_k}'.",
                        )

            # 3. No unknown arguments if schema is strict (allow standard executor & privacy routing annotations)
            allowed_keys = set(arg_schemas.keys()) | {
                "target_device",
                "privacy_class",
                "data_categories",
                "required_data_locations",
                "owning_device",
            }
            for k in step_args.keys():
                if k not in allowed_keys:
                    return (
                        False,
                        f"Step {idx} ('{cap_name}') contains unknown argument '{k}'.",
                    )

            # 4. Permission / risk metadata known
            tier = schema.get("permission_tier")
            risk = schema.get("risk")
            if tier not in (1, 2, 3) or risk not in ("low", "medium", "high"):
                return (
                    False,
                    f"Step {idx} ('{cap_name}') has unknown permission/risk metadata.",
                )

        return True, None

    async def persist_plan(
        self,
        goal_id: str,
        plan: Dict[str, Any],
        start_index: int = 0,
    ) -> List[GoalStep]:
        """
        Persist validated steps through GoalManager.
        Never partially persist an invalid plan.
        """
        persisted_steps: List[GoalStep] = []
        steps = plan.get("steps", [])

        for i, s in enumerate(steps):
            desc = s.get("description") or f"Execute {s['capability']}"
            step = await self._goal_manager.add_step(
                goal_id=goal_id,
                description=desc,
                capability=s["capability"],
                arguments=s.get("arguments", {}),
                index=start_index + i,
            )
            persisted_steps.append(step)

        log.info(
            f"[GoalPlanner] Successfully persisted {len(persisted_steps)} steps for goal '{goal_id}'"
        )
        return persisted_steps

    async def plan_goal(
        self,
        goal_id: str,
        target_device: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Convert a high-level user goal into bounded GoalSteps using only Cortex capabilities.
        Flow:
        Goal PENDING/ACTIVE
        -> build capability context
        -> call model reasoning
        -> validate plan
        -> if valid: persist plan
        -> if invalid or unachievable: goal -> BLOCKED with clear reason
        """
        goal = await self._goal_manager.get_goal(goal_id)
        if not goal:
            return {"status": "error", "reason": f"Goal '{goal_id}' not found"}

        resolved_target = (
            target_device
            or goal.metadata.get("target_device")
            or get_hub_device_id()
        )

        # 1. Build machine-readable capability context
        capability_context = await self.build_capability_context(
            target_device=resolved_target
        )

        # 2. Invoke model routing infrastructure to reason and generate plan
        plan_output = await self._generate_plan(
            goal=goal,
            capability_context=capability_context,
            target_device=resolved_target,
        )

        # 3. Check for planning failure / unachievable goal
        if not plan_output or plan_output.get("unachievable_reason"):
            reason = (
                plan_output.get("unachievable_reason")
                if plan_output
                else "Planner could not produce a valid plan with available capabilities."
            )
            await self._goal_manager.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason}

        # 4. Strict plan validation
        is_valid, val_err = self.validate_plan(
            plan=plan_output,
            capability_context=capability_context,
            goal=goal,
        )
        if not is_valid:
            reason = f"Plan validation rejected: {val_err}"
            await self._goal_manager.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason}

        if plan_output.get("resolved_target"):
            meta = dict(goal.metadata or {})
            meta["resolved_target"] = plan_output["resolved_target"]
            goal.metadata = meta
            await self._goal_manager.update_goal(goal)

        # 5. Persist plan
        persisted = await self.persist_plan(
            goal_id=goal_id,
            plan=plan_output,
            start_index=goal.current_step_index,
        )

        return {
            "status": "planned",
            "goal_id": goal_id,
            "step_count": len(persisted),
            "steps": [s.to_dict() for s in persisted],
        }

    async def replan_after_observation(
        self,
        goal_id: str,
        failed_step_id: Optional[str] = None,
        observation: Optional[Dict[str, Any]] = None,
        target_device: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Observe + Replan:
        - Preserves completed steps and checkpoints
        - Removes remaining pending/failed steps
        - Enforces max_replans limit
        - Increments replan_count
        - Produces and persists alternative plan if available, else marks BLOCKED
        """
        goal = await self._goal_manager.get_goal(goal_id)
        if not goal:
            return {"status": "error", "reason": f"Goal '{goal_id}' not found"}

        # 1. Enforce bounded replan limit
        if goal.replan_count >= goal.max_replans:
            reason = f"Exceeded maximum replan limit ({goal.max_replans})"
            await self._goal_manager.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason}

        # 2. Increment replan counter
        goal.replan_count += 1
        await self._goal_manager.update_goal(goal)

        # 3. Preserve COMPLETED steps and remove remaining uncompleted steps
        all_steps = await self._goal_manager.list_steps(goal_id)
        completed_steps = [s for s in all_steps if s.status == "COMPLETED"]

        # Fetch failed step info BEFORE deleting uncompleted steps
        failed_step_info = None
        if failed_step_id:
            f_step = await self._goal_manager.get_step(failed_step_id)
            if f_step:
                failed_step_info = f_step.to_dict()

        uncompleted_steps = [
            s for s in all_steps if s.index >= goal.current_step_index and s.status != "COMPLETED"
        ]
        for s in uncompleted_steps:
            await self._goal_manager.delete_step(s.step_id)

        resolved_target = (
            target_device
            or goal.metadata.get("target_device")
            or get_hub_device_id()
        )
        capability_context = await self.build_capability_context(
            target_device=resolved_target
        )

        # 4. Reason about remaining plan replacement
        replan_output = await self._generate_plan(
            goal=goal,
            capability_context=capability_context,
            target_device=resolved_target,
            completed_steps=[s.to_dict() for s in completed_steps],
            failed_step=failed_step_info,
            observation=observation,
            is_replan=True,
        )

        if not replan_output or replan_output.get("unachievable_reason"):
            reason = (
                replan_output.get("unachievable_reason")
                if replan_output
                else "No valid alternative capability available to fulfill remaining goal."
            )
            await self._goal_manager.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason}

        # 5. Validate replacement plan
        is_valid, val_err = self.validate_plan(
            plan=replan_output,
            capability_context=capability_context,
            goal=goal,
        )
        if not is_valid:
            reason = f"Replanned steps rejected by validation: {val_err}"
            await self._goal_manager.mark_blocked(goal_id, reason=reason)
            return {"status": "blocked", "reason": reason}

        # 6. Persist replacement steps continuing from checkpoint
        new_steps = await self.persist_plan(
            goal_id=goal_id,
            plan=replan_output,
            start_index=goal.current_step_index,
        )

        return {
            "status": "replanned",
            "goal_id": goal_id,
            "replan_count": goal.replan_count,
            "step_count": len(new_steps),
            "steps": [s.to_dict() for s in new_steps],
        }

    async def _generate_plan(
        self,
        goal: Goal,
        capability_context: List[Dict[str, Any]],
        target_device: str,
        completed_steps: Optional[List[Dict[str, Any]]] = None,
        failed_step: Optional[Dict[str, Any]] = None,
        observation: Optional[Dict[str, Any]] = None,
        is_replan: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Invokes model routing infrastructure to reason over capability context.
        Operates from:
        - user goal
        - current context & available capability metadata
        """
        provider = self._model_router or get_llm_provider()

        context = {
            "mode": "planning",
            "routing_lane": "STRUCTURED_MODEL",
            "goal": goal.to_dict(),
            "target_device": target_device,
            "available_capabilities": capability_context,
            "completed_steps": completed_steps or [],
            "failed_step": failed_step,
            "observation": observation,
            "is_replan": is_replan,
            "privacy_class": "SENSITIVE" if observation or completed_steps or failed_step else goal.metadata.get("privacy_class", "INTERNAL"),
            "allow_provider_fallback": False,
        }

        try:
            selector = provider
            while hasattr(selector, "provider") and not hasattr(selector, "select_for_turn"):
                selector = selector.provider
            if hasattr(selector, "select_for_turn"):
                provider, context = await selector.select_for_turn(goal.original_request, context, [])
            from .agent_turn import AgentTurn
            if not AgentTurn(None, provider)._model_privacy(context, "goal.plan").is_allowed:
                return {"steps": [], "unachievable_reason": "Planning requires a privacy-authorized reasoning provider."}
            res: ModelResult = await provider.generate_result(
                goal.original_request, context
            )
            if hasattr(res, "plan") and res.plan is not None:
                if res.plan.get("steps"):
                    return res.plan
                reason = res.plan.get("unachievable_reason", "")
                if reason:
                    return res.plan
            if hasattr(res, "step_decision") and res.step_decision is not None:
                return res.step_decision
            if res.response_text and res.type == "PLAN":
                cleaned = res.response_text.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.split("\n")
                    lines = [l for l in lines if not l.strip().startswith("```")]
                    cleaned = "\n".join(lines)
                if cleaned.startswith("{"):
                    try:
                        parsed = json.loads(cleaned)
                        if isinstance(parsed, dict) and "steps" in parsed:
                            return parsed
                    except Exception:
                        pass
        except Exception as e:
            log.warning("goal_planning_failed reason=%s", type(e).__name__)

        return {"steps": [], "unachievable_reason": "The reasoning provider did not return a valid plan. No actions were inferred."}

    def _reason_from_capabilities(self, *args, **kwargs):
        """Deprecated synchronous interface cannot perform model inference."""
        return {"steps": [], "unachievable_reason": "A reasoning model is required; use the asynchronous plan_goal API."}
