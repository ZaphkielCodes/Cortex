"""Execute exactly one authorized canonical capability and preserve its real result."""
import logging
from .event_bus import ActionExecuted, ActionFailed, EventBus

log = logging.getLogger("cortex.pipeline.execution_dispatcher")

class ExecutionDispatcher:
    @classmethod
    async def dispatch(cls, intent, target_device, control_layer, request_id="unknown", source_device=None, privacy_context=None):
        if len(intent.candidate_capabilities) != 1:
            return {"status": "failure", "message": "One canonical capability is required for execution."}
        action, args = intent.candidate_capabilities[0], intent.arguments
        log.info("capability_dispatch request_id=%s capability=%s target=%s", request_id, action, target_device)
        bus = EventBus.get_instance()
        try:
            from ..integrations.ecosystem import IntegrationRegistry
            integrations=IntegrationRegistry.get_instance()
            if integrations.get(action):
                result=await integrations.execute(action,args,source_device,privacy_context)
            elif action == "system.runtime_state":
                if not source_device:
                    return {"status": "denied", "message": "An authenticated requesting device is required."}
                result = await control_layer.execute_and_observe(target_device, action, args, source_device_id=source_device)
            elif action == "hub.transfer_file":
                if not source_device:
                    return {"status": "denied", "message": "A verified requesting device is required."}
                from ..peer_transfer import copy_between_workers
                result = await copy_between_workers(control_layer, source_device, args, privacy_context)
            elif hasattr(control_layer, "execute_and_observe"):
                result = await control_layer.execute_and_observe(target_device, action, args)
            else:
                return {"status": "failure", "message": "Verified execution is unavailable."}
            if not isinstance(result, dict) or not isinstance(result.get("status"), str):
                return {"status": "failure", "message": "The executor returned an invalid result."}
            if result["status"] == "success":
                # Source attribution is execution metadata, never model-generated prose.
                from datetime import datetime, timezone
                from ..capability_registry import CanonicalCapabilityRegistry
                cap = CanonicalCapabilityRegistry.get_instance().get_capability(action)
                data = result.get("data") if isinstance(result.get("data"), dict) else {}
                urls = [row["url"] for row in data.get("results", [])
                        if isinstance(row, dict) and isinstance(row.get("url"), str)]
                if isinstance(data.get("url"), str):
                    urls.append(data["url"])
                result = dict(result)
                result.setdefault("provenance", {
                    "capability": action, "executor_device": target_device,
                    "provider": data.get("provider") or getattr(cap, "provider", None) or "device",
                    "source_type": getattr(cap, "result_source", None) or
                        ("device_action" if cap and cap.target_type == "device" else "runtime_state"),
                    "retrieved_at": data.get("retrieved_at") or data.get("observed_at") or datetime.now(timezone.utc).isoformat(),
                    "source_urls": list(dict.fromkeys(urls)),
                })
                await bus.publish(ActionExecuted(action=action, target_device=target_device, success=True, result=result))
            else:
                await bus.publish(ActionFailed(action=action, target_device=target_device, error=result.get("message", "")))
            return result
        except Exception as exc:
            log.warning("capability_execution_failed capability=%s reason=%s", action, type(exc).__name__)
            await bus.publish(ActionFailed(action=action, target_device=target_device, error=type(exc).__name__))
            return {"status": "failure", "message": "Execution failed; completion could not be verified."}
