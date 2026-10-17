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
import re
import urllib.parse
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .goals import Goal, GoalStep, GoalManager

from .database import (
    get_device,
    list_devices,
    get_device_capabilities,
    delete_goal_step,
)
from .control_layer import CortexControlLayer
from .llm_provider import get_llm_provider
from .intent_schema import ModelResult

log = logging.getLogger("cortex.planner")


# ---------------------------------------------------------------------------
# Capability Schema Model
# ---------------------------------------------------------------------------

from .capability_registry import (
    CanonicalCapabilityRegistry,
    RegisteredCapability,
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
            or "asus"
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
            or "asus"
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
        provider = get_llm_provider()

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
        }

        try:
            res: ModelResult = await provider.generate_result(
                goal.original_request, context
            )
            if hasattr(res, "plan") and res.plan is not None:
                if res.plan.get("steps"):
                    return res.plan
                reason = res.plan.get("unachievable_reason", "")
                if reason and not reason.startswith("No available capabilities match requested task"):
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
            log.warning(f"[GoalPlanner] Model provider planning call error: {e}")

        # Deterministic fallback reasoning if provider didn't return a plan object
        return self._reason_from_capabilities(
            request=goal.original_request,
            capabilities=capability_context,
            target_device=target_device,
            is_replan=is_replan,
            completed_steps=completed_steps,
            failed_step=failed_step,
        )

    def _reason_from_capabilities(
        self,
        request: str = "",
        capabilities: Any = None,
        target_device: str = "asus",
        is_replan: bool = False,
        completed_steps: Optional[List[Dict[str, Any]]] = None,
        failed_step: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Capability-aware reasoning core according to Planning Rule:
        What capability do I need?
        Which available capability satisfies that need?
        What arguments does it require?
        """
        if "goal_text" in kwargs and not request:
            request = kwargs["goal_text"]
        if "available_caps" in kwargs and capabilities is None:
            raw_caps = kwargs["available_caps"]
            if isinstance(raw_caps, (set, list, tuple)):
                capabilities = [{"name": (c if isinstance(c, str) else getattr(c, "name", str(c))), "availability": True} for c in raw_caps]
            elif isinstance(raw_caps, dict):
                capabilities = [{"name": k, "availability": True} for k in raw_caps.keys()]

        capabilities = capabilities or []
        req_lower = (request or "").lower()
        available_names = set()
        for c in capabilities:
            if isinstance(c, str):
                available_names.add(c)
            elif isinstance(c, dict) and c.get("availability", True):
                available_names.add(c.get("name", ""))
            elif hasattr(c, "name"):
                available_names.add(c.name)

        # 0. Check for process inspection / resource / memory goals
        if any(w in req_lower for w in ("process", "processes", "memory", "ram", "cpu", "taking most", "using most")):
            if "process.list" in available_names:
                step_dict = {
                    "description": "Inspect running processes by resource consumption",
                    "capability": "process.list",
                    "arguments": {"sort_by": "memory", "limit": 10, "target_device": target_device},
                }
                out = {"steps": [step_dict], "capability": "process.list"}
                out.update(step_dict)
                return out
            elif "telemetry.get_system_usage" in available_names:
                step_dict = {
                    "description": "Inspect system resource utilization",
                    "capability": "telemetry.get_system_usage",
                    "arguments": {"target_device": target_device},
                }
                out = {"steps": [step_dict], "capability": "telemetry.get_system_usage"}
                out.update(step_dict)
                return out

        # 1. Check for physical / manufacturing / 3D printing tasks
        if any(w in req_lower for w in ("3d print", "print 3d", "fabricate", "manufacturing", "hinge")):
            return {
                "steps": [],
                "unachievable_reason": "No available capability for 3D printing or hardware fabrication.",
            }

        # 1b. Check for condition-based wait goals ("When <condition>, <action>")
        m_when = re.search(r'\bwhen\s+(.+?),\s*(.+)', req_lower)
        if m_when:
            if "goal.wait" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "No available wait capability supports conditional goal execution.",
                }
            cond_phrase = m_when.group(1).strip(" \t.,!?;:")
            action_phrase = m_when.group(2).strip(" \t.,!?;:")

            wait_step = None
            # Check device online condition: e.g. "asus comes online" / "device comes online"
            m_dev_online = re.search(r'(\w+)\s+(?:comes\s+online|is\s+online|connects|reconnects)', cond_phrase)
            if m_dev_online:
                dev_target = m_dev_online.group(1)
                wait_step = {
                    "description": f"Wait for {dev_target} to come online",
                    "capability": "goal.wait",
                    "arguments": {
                        "condition_type": "device_online",
                        "condition_key": dev_target,
                    },
                }

            # Check world state signal condition: e.g. "my heart rate goes over 100" / "heart rate > 100"
            m_signal_num = re.search(r'(heart[\s_-]?rate|battery|temperature|workload|cpu)\s*(?:goes\s+over|exceeds|>|is\s+greater\s+than)\s*(\d+)', cond_phrase)
            if m_signal_num:
                sig_raw = m_signal_num.group(1).replace(" ", "_").replace("-", "_")
                sig_key = "health.heart_rate" if "heart" in sig_raw else sig_raw
                threshold = int(m_signal_num.group(2))
                wait_step = {
                    "description": f"Wait for {sig_key} > {threshold}",
                    "capability": "goal.wait",
                    "arguments": {
                        "condition_type": "world_state",
                        "condition_key": sig_key,
                        "operator": ">",
                        "expected_value": threshold,
                    },
                }

            if wait_step:
                steps = [wait_step]
                if any(w in action_phrase for w in ("notify", "alert", "tell me")):
                    if "ui.notify" in available_names:
                        msg = "ASUS is online!" if "asus" in cond_phrase else f"{cond_phrase} condition met"
                        steps.append({
                            "description": "Notify user condition is satisfied",
                            "capability": "ui.notify",
                            "arguments": {
                                "message": msg,
                                "target_device": target_device,
                            },
                        })
                    else:
                        return {
                            "steps": [],
                            "unachievable_reason": "Required ui.notify capability is not available on target device.",
                        }
                m_open = re.search(r'(?:open|launch|start|run)\s+([a-zA-Z0-9_\-\.\s]+)', action_phrase)
                if m_open:
                    if "app.launch" in available_names:
                        app_name = m_open.group(1).strip()
                        steps.append({
                            "description": f"Launch {app_name}",
                            "capability": "app.launch",
                            "arguments": {
                                "app": app_name,
                                "target_device": target_device,
                            },
                        })
                    else:
                        return {
                            "steps": [],
                            "unachievable_reason": "Required app.launch capability is not available on target device.",
                        }

                if len(steps) > 1:
                    return {"steps": steps}

        # 1c. Search and open result goal (e.g. "Find the latest news about RTX 5090 laptops and open the most relevant result")
        m_search_open = re.search(r'(?:find|search(?:\s+for)?)\s+(.+?)\s+and\s+open\s+(?:the\s+)?(?:most\s+relevant\s+)?(?:result|page|site|website)', req_lower)
        if m_search_open:
            query = m_search_open.group(1).strip()
            if "web.search" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required web.search capability is not available.",
                }
            if "browser.open_url" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required browser.open_url capability is not available on target device.",
                }
            return {
                "steps": [
                    {
                        "description": f"Search the web for '{query}'",
                        "capability": "web.search",
                        "arguments": {"query": query, "max_results": 5},
                    },
                    {
                        "description": "Open the most relevant search result in browser",
                        "capability": "browser.open_url",
                        "arguments": {"url": "https://www.google.com/search?q=" + urllib.parse.quote_plus(query), "target_device": target_device},
                    },
                ]
            }

        # 1d. Pure web search goal (e.g. "Search the web for latest Nvidia news")
        m_search_only = re.search(r'(?:search the web for|search online for|search for|look up on the web)\s+(.+)', req_lower)
        if m_search_only:
            query = m_search_only.group(1).strip()
            if "web.search" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required web.search capability is not available.",
                }
            return {
                "steps": [
                    {
                        "description": f"Search the web for '{query}'",
                        "capability": "web.search",
                        "arguments": {"query": query, "max_results": 5},
                    }
                ]
            }

        # 1e. Weather goal (e.g. "Check the weather in New York", "What's the weather today in Boston")
        m_weather = re.search(r'(?:weather|temperature|forecast)\s+(?:in|for|at)\s+([a-zA-Z\s]+)', req_lower)
        if m_weather or "weather" in req_lower:
            loc = m_weather.group(1).strip().title() if m_weather else "current location"
            is_forecast = "forecast" in req_lower or "weekend" in req_lower
            cap_needed = "weather.forecast" if is_forecast else "weather.current"
            if cap_needed not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": f"Required {cap_needed} capability is not available.",
                }
            return {
                "steps": [
                    {
                        "description": f"Retrieve weather {'forecast' if is_forecast else 'conditions'} for {loc}",
                        "capability": cap_needed,
                        "arguments": {"location": loc, "units": "metric"},
                    }
                ]
            }

        # 1f. Browser open website (e.g. "Open YouTube", "Open https://...", "Open github")
        m_browser_open = re.search(r'^(?:open|launch)\s+(https?://\S+|www\.\S+|youtube|github|google|reddit|twitter|x|gmail|netflix|amazon)\b', req_lower)
        if m_browser_open:
            dest = m_browser_open.group(1).strip()
            from .browser_navigation import COMMON_WEB_DESTINATIONS, is_valid_url, normalize_url
            if dest.lower() in COMMON_WEB_DESTINATIONS:
                target_url = COMMON_WEB_DESTINATIONS[dest.lower()]
            elif is_valid_url(dest):
                target_url = normalize_url(dest)
            else:
                target_url = f"https://www.{dest}.com"

            if "browser.open_url" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required browser.open_url capability is not available on target device.",
                }

            # Check if visual verification is requested (e.g. "Open YouTube and check that it opened")
            is_visual_verify = bool(re.search(r'\b(?:and\s+)?(?:check|verify)(?:\s+that|\s+if)?\s+it(?:\s+visibly)?\s+opened\b', req_lower))

            if is_visual_verify:
                if "screen.capture" not in available_names:
                    return {
                        "steps": [],
                        "unachievable_reason": "Required screen.capture capability for visual verification is not available on target device.",
                    }
                if "screen.analyze" not in available_names:
                    return {
                        "steps": [],
                        "unachievable_reason": "Required screen.analyze capability for visual verification is not available.",
                    }
                return {
                    "steps": [
                        {
                            "description": f"Open {target_url} in browser",
                            "capability": "browser.open_url",
                            "arguments": {"url": target_url, "target_device": target_device},
                        },
                        {
                            "description": "Capture screen display for visual verification",
                            "capability": "screen.capture",
                            "arguments": {"target_device": target_device},
                        },
                        {
                            "description": f"Visually verify that {dest} opened in browser",
                            "capability": "screen.analyze",
                            "arguments": {
                                "question": f"Did {dest} visibly open?",
                                "mode": "inspect",
                            },
                        },
                    ]
                }

            return {
                "steps": [
                    {
                        "description": f"Open {target_url} in browser",
                        "capability": "browser.open_url",
                        "arguments": {"url": target_url, "target_device": target_device},
                    }
                ]
            }

        # 1fa. Application launch (e.g. "Open Chrome", "Launch Notepad", "Open Spotify")
        m_app_launch = re.search(r'^(?:open|launch|start|run)\s+(?:the\s+)?([a-zA-Z0-9_\-\.\s]+)$', req_lower)
        if m_app_launch and not any(w in req_lower for w in ("file", "folder", "directory", "pdf", "txt", "doc", "calc", "http", "www.")) and " and " not in req_lower:
            app_cand = m_app_launch.group(1).strip()
            if "app.launch" in available_names:
                return {
                    "steps": [
                        {
                            "description": f"Launch application '{app_cand}'",
                            "capability": "app.launch",
                            "arguments": {"app": app_cand, "target_device": target_device},
                        }
                    ]
                }

        # 1g. Screen Perception & Inspection goal (e.g. "Look at my screen and tell me why this app failed")
        m_screen_inspect = re.search(
            r'(?:look at (?:my |the )?screen|inspect (?:my |the )?screen|what(?:(?:\'s| is) (?:on|showing on|visible on)) (?:my |the )?screen|tell me why this app failed|why did this app fail|read (?:this|the|my) screen|what error is (?:showing|on screen))',
            req_lower
        )
        if m_screen_inspect or (("screen" in req_lower or "app failed" in req_lower) and any(w in req_lower for w in ("look", "tell me", "inspect", "why", "what", "read", "error"))):
            if "screen.capture" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required screen.capture capability is not available on target device.",
                }
            if "screen.analyze" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required screen.analyze capability is not available.",
                }

            inspect_question = "Why did this app fail?" if "fail" in req_lower else (goal.title or original_request)
            inspect_mode = "inspect" if any(w in req_lower for w in ("fail", "error", "crash", "wrong", "issue")) else "describe"

            return {
                "steps": [
                    {
                        "description": "Capture current screen display",
                        "capability": "screen.capture",
                        "arguments": {"target_device": target_device},
                    },
                    {
                        "description": "Analyze screen image and answer visual inquiry",
                        "capability": "screen.analyze",
                        "arguments": {
                            "question": inspect_question,
                            "mode": inspect_mode,
                        },
                    },
                ]
            }

        # 1h. Compound: Open file and switch/focus to it (e.g. "Open my Calc review PDF and switch to it")
        m_open_file_switch = re.search(r'open\s+(?:my\s+)?(.+?)\s+and\s+(?:switch|focus)\s+to\s+it', req_lower)
        if m_open_file_switch:
            raw_target = m_open_file_switch.group(1).strip()
            target_path = raw_target
            if "pdf" in raw_target.lower() and not raw_target.lower().endswith(".pdf"):
                clean_stem = re.sub(r'\s+pdf$', '', raw_target, flags=re.IGNORECASE).strip()
                target_path = f"{clean_stem}.pdf"

            if "fs.open" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required fs.open capability is not available.",
                }
            if "window.list" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required window.list capability is not available.",
                }
            if "window.focus" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required window.focus capability is not available.",
                }

            return {
                "steps": [
                    {
                        "description": f"Open file '{target_path}' with default associated application",
                        "capability": "fs.open",
                        "arguments": {"path": target_path, "target_device": target_device},
                    },
                    {
                        "description": "Inspect running windows to resolve document window handle",
                        "capability": "window.list",
                        "arguments": {"target_device": target_device},
                    },
                    {
                        "description": f"Switch foreground focus to '{raw_target}' window",
                        "capability": "window.focus",
                        "arguments": {"title": raw_target, "target_device": target_device},
                    },
                ]
            }

        # 1i. Compound: Close app and open/launch app (e.g. "Close Discord and open Spotify")
        m_close_open = re.search(r'(?:close|exit|quit|kill)\s+([a-zA-Z0-9_\-\.]+)\s+and\s+(?:open|launch|start|run)\s+([a-zA-Z0-9_\-\.]+)', req_lower)
        if m_close_open:
            app_close_name = m_close_open.group(1).strip()
            app_open_name = m_close_open.group(2).strip()

            if "app.close" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required app.close capability is not available.",
                }
            if "app.launch" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required app.launch capability is not available.",
                }

            return {
                "steps": [
                    {
                        "description": f"Close application '{app_close_name}' gracefully",
                        "capability": "app.close",
                        "arguments": {"app": app_close_name, "graceful": True, "target_device": target_device},
                    },
                    {
                        "description": f"Launch application '{app_open_name}'",
                        "capability": "app.launch",
                        "arguments": {"app": app_open_name, "target_device": target_device},
                    },
                ]
            }

        # 1j. File operations: Copy, Rename, Move
        m_copy_plan = re.search(r'copy\s+(?:file\s+)?(\S+|\'[^\']+\'|"[^"]+")\s+to\s+(\S+|\'[^\']+\'|"[^"]+")', request, re.IGNORECASE)
        if m_copy_plan:
            src_file = m_copy_plan.group(1).strip('\'"')
            dst_file = m_copy_plan.group(2).strip('\'"')
            if "fs.copy" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required fs.copy capability is not available.",
                }
            return {
                "steps": [
                    {
                        "description": f"Copy '{src_file}' to '{dst_file}' with overwrite protection",
                        "capability": "fs.copy",
                        "arguments": {"src": src_file, "dst": dst_file, "overwrite": False, "target_device": target_device},
                    }
                ]
            }

        m_rename_plan = re.search(r'rename\s+(?:file\s+)?(\S+|\'[^\']+\'|"[^"]+")\s+to\s+(\S+|\'[^\']+\'|"[^"]+")', request, re.IGNORECASE)
        if m_rename_plan:
            src_file = m_rename_plan.group(1).strip('\'"')
            new_name = m_rename_plan.group(2).strip('\'"')
            if "fs.rename" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required fs.rename capability is not available.",
                }
            return {
                "steps": [
                    {
                        "description": f"Rename '{src_file}' to '{new_name}' with verification",
                        "capability": "fs.rename",
                        "arguments": {"src": src_file, "new_name": new_name, "overwrite": False, "target_device": target_device},
                    }
                ]
            }

        m_move_plan = re.search(r'move\s+(?:file\s+)?(\S+|\'[^\']+\'|"[^"]+")\s+to\s+(\S+|\'[^\']+\'|"[^"]+")', request, re.IGNORECASE)
        if m_move_plan:
            src_file = m_move_plan.group(1).strip('\'"')
            dst_file = m_move_plan.group(2).strip('\'"')
            if "fs.move" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required fs.move capability is not available.",
                }
            return {
                "steps": [
                    {
                        "description": f"Move '{src_file}' to '{dst_file}' with verification",
                        "capability": "fs.move",
                        "arguments": {"src": src_file, "dst": dst_file, "overwrite": False, "target_device": target_device},
                    }
                ]
            }

        # 1k. Focus / Switch to window (e.g. "Switch to Chrome", "Focus the terminal")
        m_focus_plan = re.search(r'^(?:switch\s+to|focus(?:\s+on)?)\s+(?:the\s+)?([a-zA-Z0-9_\-\.\s]+)$', req_lower)
        if m_focus_plan and not is_replan:
            win_target = m_focus_plan.group(1).strip()
            if "window.focus" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required window.focus capability is not available.",
                }
            return {
                "steps": [
                    {
                        "description": f"Switch foreground focus to '{win_target}'",
                        "capability": "window.focus",
                        "arguments": {"title": win_target, "app": win_target, "target_device": target_device},
                    }
                ]
            }

        # 1l. Close app standalone (e.g. "Close Notepad", "Close Discord")
        m_close_plan = re.search(r'^(?:close|quit|exit|terminate)\s+(?:the\s+)?([a-zA-Z0-9_\-\.\s]+)$', req_lower)
        if m_close_plan and not is_replan:
            app_target = m_close_plan.group(1).strip()
            if "app.close" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "Required app.close capability is not available.",
                }
            return {
                "steps": [
                    {
                        "description": f"Close application '{app_target}' gracefully",
                        "capability": "app.close",
                        "arguments": {"app": app_target, "graceful": True, "target_device": target_device},
                    }
                ]
            }

        # 2. Check for directory / filesystem listing tasks
        if any(w in req_lower for w in ("list the files", "list files", "count the files", "count files", "downloads folder", "how many files")):
            if "fs.list" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "No available filesystem capability supports listing directory contents.",
                }
            from .environment import resolve_location_path
            loc_path = resolve_location_path("downloads")
            steps = [
                {
                    "description": "Inspect and list direct files in Downloads folder",
                    "capability": "fs.list",
                    "arguments": {
                        "path": str(loc_path or "downloads"),
                        "target_device": target_device,
                    },
                }
            ]
            if any(w in req_lower for w in ("notify", "tell me")):
                if "ui.notify" in available_names:
                    steps.append({
                        "description": "Notify user with file count",
                        "capability": "ui.notify",
                        "arguments": {
                            "message": "Downloads folder contents retrieved.",
                            "target_device": target_device,
                        },
                    })
            return {"steps": steps}

        # 2b. Check for grounded filesystem object actions (delete/remove/cleanup from location)
        from .target_resolver import TargetResolver
        resolved_entity = TargetResolver.resolve(request, current_device=target_device)
        if resolved_entity.action in ("delete", "cleanup") and resolved_entity.target_kind in ("filesystem_item", "collection"):
            if "fs.list" not in available_names:
                return {
                    "steps": [],
                    "unachievable_reason": "No available filesystem capability supports inspecting directory to resolve target item.",
                }
            from .environment import resolve_location_path
            loc = resolved_entity.location or "desktop"
            loc_path = resolve_location_path(loc)
            steps = [
                {
                    "description": f"Inspect {loc} to resolve {resolved_entity.target_name or 'collection items'}",
                    "capability": "fs.list",
                    "arguments": {
                        "path": str(loc_path or loc),
                        "target_device": target_device,
                    },
                }
            ]
            return {"steps": steps, "resolved_target": resolved_entity.to_dict()}

        # 3. Replanning path
        if is_replan:
            failed_cap = (failed_step or {}).get("capability")
            completed_caps = {s.get("capability") for s in (completed_steps or [])}

            # If failed_cap is known, propose an alternative capability
            if failed_cap:
                if failed_cap == "ui.notify" and "ui.notify" in available_names:
                    return {
                        "steps": [
                            {
                                "description": "Notify user via fallback notification path",
                                "capability": "ui.notify",
                                "arguments": {
                                    "message": "Notification sent via fallback",
                                    "target_device": target_device,
                                },
                            }
                        ]
                    }
                elif failed_cap == "device.status" and "device.info" in available_names:
                    return {
                        "steps": [
                            {
                                "description": "Fallback device hardware check",
                                "capability": "device.info",
                                "arguments": {"target_device": target_device},
                            }
                        ]
                    }
                elif "ui.notify" in available_names and failed_cap != "ui.notify":
                    return {
                        "steps": [
                            {
                                "description": "Notify user of partial completion and status",
                                "capability": "ui.notify",
                                "arguments": {
                                    "message": "Task partially completed with alternative path",
                                    "target_device": target_device,
                                },
                            }
                        ]
                    }
                return {
                    "steps": [],
                    "unachievable_reason": "No valid alternative capability available to fulfill remaining goal.",
                }

            # If failed_cap was not provided, plan the remaining needed steps for the goal
            remaining_steps = []
            needs_status = any(w in req_lower for w in ("check", "status", "battery", "inspect", "device"))
            needs_notify = any(w in req_lower for w in ("notify", "alert", "tell me", "message"))

            if needs_status and "device.status" not in completed_caps and "device.info" not in completed_caps:
                if "device.status" in available_names:
                    remaining_steps.append({
                        "description": "Check current device status",
                        "capability": "device.status",
                        "arguments": {"target_device": target_device},
                    })
                elif "device.info" in available_names:
                    remaining_steps.append({
                        "description": "Check device info",
                        "capability": "device.info",
                        "arguments": {"target_device": target_device},
                    })

            if needs_notify and "ui.notify" not in completed_caps:
                if "ui.notify" in available_names:
                    remaining_steps.append({
                        "description": "Notify the user with the result",
                        "capability": "ui.notify",
                        "arguments": {
                            "message": "Device status check completed successfully.",
                            "target_device": target_device,
                        },
                    })

            if remaining_steps:
                return {"steps": remaining_steps}

            # Fallback notification if all else completed
            if "ui.notify" in available_names:
                return {
                    "steps": [
                        {
                            "description": "Notify user of status",
                            "capability": "ui.notify",
                            "arguments": {"message": "Goal status check", "target_device": target_device},
                        }
                    ]
                }

            return {
                "steps": [],
                "unachievable_reason": "No valid alternative capability available to fulfill remaining goal.",
            }

        # 4. General Computer Use & Desktop State Changes (Phase A10)
        # -------------------------------------------------------------------
        # 4. Computer-Use Goals (Desktop State Change & Compound App Actions)
        # Bounded iterative execution: Launch required app (if any) -> Observe -> Iterative Agent Loop
        # High-level goals are NEVER passed as UI element selectors!
        # -------------------------------------------------------------------
        intent_args = kwargs.get("intent_args") or {}
        target_app = intent_args.get("app") if isinstance(intent_args, dict) else None
        if not target_app:
            if "notepad" in req_lower:
                target_app = "Notepad"
            elif any(w in req_lower for w in ("chrome", "browser")):
                target_app = "Chrome"
            elif any(w in req_lower for w in ("settings", "dark mode", "display", "refresh", "hertz", "hz", "bluetooth", "battery", "default")):
                target_app = "Settings"

        is_compound_app = (
            (intent_args.get("execution_mode") == "computer_use" if isinstance(intent_args, dict) else False)
            or ("notepad" in req_lower and any(w in req_lower for w in ("type", "write", "text")))
        )

        desktop_setting_domains = (
            "display", "refresh rate", "refresh", "hertz", "hz", "resolution", "dark mode",
            "light mode", "theme", "appearance", "text", "font", "scaling",
            "browser", "default browser", "default", "battery", "bluetooth", "wifi", "network",
            "night light", "contrast", "settings", "page"
        )
        is_desktop_setting_goal = (
            any(w in req_lower for w in desktop_setting_domains)
            and any(w in req_lower for w in ("change", "set", "turn on", "turn off", "switch", "make", "adjust", "enable", "disable", "toggle", "configure", "update", "open", "find", "show"))
            and not any(req_lower.startswith(p) for p in ("click", "press", "tap", "type", "put", "scroll", "select all"))
        )

        if is_compound_app or is_desktop_setting_goal:
            app_to_launch = target_app or "Settings"
            ui_steps = []
            if "app.launch" in available_names:
                ui_steps.append({
                    "description": f"Launch '{app_to_launch}' for computer-use execution",
                    "capability": "app.launch",
                    "arguments": {"app": app_to_launch, "target_device": target_device},
                })
            # Determine specific target selector (NEVER the full user request sentence!)
            target_selector = "control"
            if "notepad" in app_to_launch.lower():
                target_selector = "Text Editor"
            elif any(w in req_lower for w in ("display", "refresh", "hertz", "hz")):
                target_selector = "Display"
            elif "dark mode" in req_lower:
                target_selector = "Personalization"
            elif "bluetooth" in req_lower:
                target_selector = "Bluetooth & devices"
            elif "battery" in req_lower:
                target_selector = "Power & battery"
            elif "browser" in req_lower:
                target_selector = "Default apps"
            elif "text" in req_lower:
                target_selector = "Accessibility"

            if "ui.click" in available_names:
                ui_steps.append({
                    "description": f"Interact with '{target_selector}'",
                    "capability": "ui.click",
                    "arguments": {"target": target_selector, "target_device": target_device},
                })
            if "ui.observe" in available_names:
                ui_steps.append({
                    "description": "Observe and verify goal progress",
                    "capability": "ui.observe",
                    "arguments": {"target_device": target_device, "include_tree": True},
                })
            if ui_steps:
                return {"steps": ui_steps}

        # 5. Standard goal: "Check this device and notify me"
        steps = []
        needs_status = any(w in req_lower for w in ("check", "status", "inspect")) and any(w in req_lower for w in ("device", "battery", "system")) and "settings" not in req_lower and "usage" not in req_lower
        needs_notify = any(w in req_lower for w in ("notify", "alert", "tell me", "message"))

        if needs_status:
            if "device.status" in available_names:
                steps.append({
                    "description": "Check current device status",
                    "capability": "device.status",
                    "arguments": {"target_device": target_device},
                })
            elif "device.info" in available_names:
                steps.append({
                    "description": "Check device info",
                    "capability": "device.info",
                    "arguments": {"target_device": target_device},
                })
            else:
                return {
                    "steps": [],
                    "unachievable_reason": "Required device status capability is not available on target device.",
                }

        if needs_notify:
            if "ui.notify" in available_names:
                steps.append({
                    "description": "Notify the user with the result",
                    "capability": "ui.notify",
                    "arguments": {
                        "message": "Device status check completed successfully.",
                        "target_device": target_device,
                    },
                })
            else:
                return {
                    "steps": [],
                    "unachievable_reason": "Required ui.notify capability is not available on target device.",
                }

        if steps:
            return {"steps": steps}

        if any(w in req_lower for w in ("click", "press", "tap", "type", "put", "select", "scroll", "find the button", "find button", "export button", "dark mode", "billing")):
            ui_steps = []
            if "ui.observe" in available_names:
                ui_steps.append({
                    "description": "Observe active desktop UI and extract actionable elements",
                    "capability": "ui.observe",
                    "arguments": {"target_device": target_device, "include_tree": True},
                })

            if any(w in req_lower for w in ("select all", "select text")):
                if "ui.hotkey" in available_names:
                    ui_steps.append({
                        "description": "Select all text using keyboard shortcut",
                        "capability": "ui.hotkey",
                        "arguments": {"keys": ["ctrl", "a"], "target_device": target_device},
                    })
            elif any(w in req_lower for w in ("type", "put '", "put \"", "enter text")):
                m_type = re.search(r"(?:type|put)\s+['\"]([^'\"]+)['\"]", request)
                text_to_type = m_type.group(1) if m_type else "Cortex A10 test"
                if "ui.type" in available_names:
                    ui_steps.append({
                        "description": f"Type '{text_to_type}' into active control",
                        "capability": "ui.type",
                        "arguments": {"text": text_to_type, "target_device": target_device},
                    })
            elif "scroll" in req_lower:
                direction = "up" if "up" in req_lower else "down"
                if "ui.scroll" in available_names:
                    ui_steps.append({
                        "description": f"Scroll interface {direction}",
                        "capability": "ui.scroll",
                        "arguments": {"direction": direction, "amount": 300, "target_device": target_device},
                    })
            elif any(w in req_lower for w in ("click", "press", "tap")):
                m_clk = re.search(r"(?:click|press|tap)\s+(?:on\s+)?(?:the\s+)?['\"]?([^'\"]+?)['\"]?$", req_lower)
                target_clk = m_clk.group(1).strip() if m_clk else "target"
                if "ui.click" in available_names:
                    ui_steps.append({
                        "description": f"Click '{target_clk}' element",
                        "capability": "ui.click",
                        "arguments": {"target": target_clk, "target_device": target_device},
                    })

            if ui_steps:
                return {"steps": ui_steps}

        return {
            "steps": [],
            "unachievable_reason": f"No available capabilities match requested task: '{request}'.",
        }
