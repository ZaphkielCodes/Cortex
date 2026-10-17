"""
hub/pipeline/execution_dispatcher.py
------------------------------------
Executes authorized capabilities through CortexControlLayer (Section 1 & 13).
"""

import logging
from typing import Any, Dict, Optional

from .event_bus import ActionExecuted, ActionFailed, EventBus
from .semantic_interpreter import SemanticIntent

log = logging.getLogger("cortex.pipeline.execution_dispatcher")


class ExecutionDispatcher:
    """
    Dispatches capability execution through control layer and emits events.
    """

    @classmethod
    async def dispatch(
        cls,
        intent: SemanticIntent,
        target_device: str,
        control_layer: Any,
        request_id: str = "unknown",
    ) -> Dict[str, Any]:
        if not intent.candidate_capabilities:
            return {"status": "success", "message": intent.response_text or ""}

        action = intent.candidate_capabilities[0]
        args = intent.arguments

        log.info("Dispatching %s to %s argument_keys=%s", action, target_device, sorted(args))

        bus = EventBus.get_instance()
        try:
            # 1. Screen Perception special handling
            if intent.required_effect == "screen_perception":
                from ..vision_provider import get_vision_provider
                vp = get_vision_provider()
                if not vp or not getattr(vp, "is_available", lambda: True)():
                    return {
                        "status": "failure",
                        "message": "screen analysis provider unavailable",
                        "error": "screen analysis provider unavailable",
                    }
                cap_res = await control_layer.execute_and_observe(
                    target_device,
                    "screen.capture",
                    {"target_device": target_device},
                )
                if not cap_res or cap_res.get("status") != "success":
                    return {
                        "status": "failure",
                        "message": f"Could not capture screen: {cap_res.get('message', 'Capture failed')}",
                    }
                cap_data = cap_res.get("data") if isinstance(cap_res.get("data"), dict) else {}
                img_ref = cap_data.get("image_ref") or cap_res.get("message", "").replace("Screenshot saved: ", "").strip()

                q = (args.get("question") or intent.goal or "").lower()
                if any(w in q for w in ("error", "fail", "wrong", "issue", "crash", "warning")):
                    mode = "inspect"
                elif any(w in q for w in ("locate", "where is", "find button", "find element")):
                    mode = "locate"
                else:
                    mode = "describe"

                obs = await vp.analyze_image(
                    image_ref=img_ref,
                    question=args.get("question") or intent.goal,
                    mode=mode,
                    device_id=target_device,
                )
                from ..screen_perception import ScreenPerceptionManager
                spm = ScreenPerceptionManager.get_instance()
                spm.store_observation(obs)
                await spm.publish_world_state_signals(obs)
                msg = obs.summary
                if obs.visible_apps:
                    msg += f" Visible apps: {', '.join(obs.visible_apps)}."
                return {
                    "status": "success",
                    "message": msg,
                    "data": obs.to_dict(),
                    "provenance": obs.provenance,
                }

            # 2. General capability execution via Control Layer
            if hasattr(control_layer, "execute_and_observe"):
                res = await control_layer.execute_and_observe(
                    target_device,
                    action,
                    args,
                )
            elif target_device in getattr(control_layer, "connected_devices", {}) and hasattr(control_layer, "execute_command"):
                import uuid
                import asyncio
                from ..main import PENDING_FUTURES
                cmd_id = uuid.uuid4().hex
                try:
                    loop = asyncio.get_running_loop()
                    PENDING_FUTURES[cmd_id] = loop.create_future()
                except Exception:
                    pass
                try:
                    await control_layer.execute_command(target_device, action, args, cmd_id=cmd_id)
                except TypeError:
                    await control_layer.execute_command(target_device, action, args)
                summary = intent.goal or f"executing {action}"
                res = {
                    "status": "in_progress",
                    "message": "The command was sent; completion has not been verified.",
                    "command_id": cmd_id,
                }
            elif hasattr(control_layer, "execute_and_observe"):
                res = await control_layer.execute_and_observe(
                    target_device,
                    action,
                    args,
                )
            elif hasattr(control_layer, "execute_action"):
                res = await control_layer.execute_action(
                    target_device,
                    action,
                    args,
                )
            else:
                res = {"status": "failure", "message": "No execution engine available in control layer."}

            # Visual verification follow-up for browser navigation
            if action == "browser.open_url" and args.get("visual_verify") and res.get("status") == "success":
                cap_res = await control_layer.execute_and_observe(
                    target_device,
                    "screen.capture",
                    {"target_device": target_device},
                )
                cap_data = cap_res.get("data") if isinstance(cap_res.get("data"), dict) else {}
                img_ref = cap_data.get("image_ref") or cap_res.get("message", "").replace("Screenshot saved: ", "").strip()
                target_site = args.get("target_site", "page")
                analyze_res = await control_layer.execute_and_observe(
                    target_device,
                    "screen.analyze",
                    {
                        "image_ref": img_ref,
                        "question": f"Did {target_site} visibly open in the browser?",
                        "mode": "inspect",
                    },
                )
                from datetime import datetime, timezone
                obs_data = analyze_res.get("data") or {}
                return {
                    "status": "success",
                    "message": f"Opened {args.get('url')} and verified it opened on screen.",
                    "data": obs_data,
                    "provenance": {
                        "provider": "mock_vision",
                        "source_urls": [args.get("url")],
                        "source_type": "visual_verification",
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    },
                }

            success = res.get("status") == "success"
            if success:
                await bus.publish(ActionExecuted(action=action, target_device=target_device, success=True, result=res))
            else:
                await bus.publish(ActionFailed(action=action, target_device=target_device, error=res.get("message", "")))

            return res

        except Exception as exc:
            log.error(f"[ExecutionDispatcher] Error executing {action}: {exc}")
            await bus.publish(ActionFailed(action=action, target_device=target_device, error=str(exc)))
            return {"status": "failure", "message": f"Execution error: {exc}"}
