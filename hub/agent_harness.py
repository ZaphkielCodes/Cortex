"""Model-first request tracing. No phrase-routing or legacy execution path."""
import logging
import re
import time
import uuid
from typing import Any, Dict, Optional
from .intent_schema import ConversationalResponse, ActionPlan
from .model_router import ModelRouter, get_model_router
from .control_layer import CortexControlLayer
from .tracing import InteractionTrace
from .metrics import MetricsCollector
from .llm_provider import classify_failure, get_user_friendly_error

log = logging.getLogger("cortex.agent_harness")


def sanitize_for_speech(text):
    if not isinstance(text, str) or not text.strip():
        return "The request ended without a reliable response."
    clean = text.strip()
    if "Supported:" in clean or ("['" in clean and "']" in clean):
        return "I couldn't complete that action because the requested capability is not supported."
    if (clean.startswith(("{", "[", "```")) or "Traceback (most recent call last)" in clean
            or re.search(r"\b(?:KeyError|ValueError|TypeError|AttributeError|FileNotFoundError|Exception):\s*", clean)):
        return "I encountered a system error while processing that request."
    return clean


def format_action_approval_phrase(plan: ActionPlan) -> str:
    # Display the model-provided action description; do not conjugate verbs by guessing.
    return sanitize_for_speech(plan.user_facing_summary or "perform the requested action")


class AgentHarness:
    def __init__(self, control_layer: CortexControlLayer, model_router: Optional[ModelRouter] = None):
        self.control = control_layer
        self.router = model_router or get_model_router()
        from .task_orchestrator import TaskOrchestrator
        self.task_orchestrator = TaskOrchestrator(self.router, self.control)
        from .pipeline import RequestPipeline
        self.pipeline = RequestPipeline(self.control, self.router)

    async def process_request(
        self, 
        source_device_id: str, 
        natural_request: str, 
        context: Dict[str, Any],
        log_action_callback: Optional[Any] = None, # Function to log actions back to db/hub
        latency_metrics: dict = None,
        on_chunk: Optional[callable] = None,
        request_id: str = "unknown"
    ) -> ConversationalResponse:
        if latency_metrics is None:
            latency_metrics = {}
    
        t0 = time.monotonic()
        trace_id = latency_metrics.get("trace_id") or f"tr_{uuid.uuid4().hex[:12]}"
        session_id = context.get("session_id") or latency_metrics.get("session_id") or f"ses_{uuid.uuid4().hex[:8]}"
        interaction_id = request_id if request_id != "unknown" else (latency_metrics.get("interaction_id") or f"ix_{uuid.uuid4().hex[:6]}")
    
        trace = InteractionTrace(
            trace_id=trace_id,
            session_id=session_id,
            interaction_id=interaction_id
        )
        for stage in ["wake_detected", "microphone_open", "speech_first_detected", "speech_end", "stt_complete"]:
            if stage in latency_metrics and latency_metrics[stage] is not None:
                trace.record_stage(stage, float(latency_metrics[stage]))
    
        trace.record_stage("hub_request_received", t0)
        latency_metrics["intent_route_start"] = t0
        latency_metrics["trace_id"] = trace_id
        latency_metrics["session_id"] = session_id
        latency_metrics["interaction_id"] = interaction_id
        context["trace"] = trace
        context["interaction_trace"] = trace
        context["request_id"] = interaction_id
    
        log.info("hub_request_received trace=%s device=%s", trace_id, source_device_id)
    
        try:
            res = await self._process_request_impl(
                source_device_id=source_device_id,
                natural_request=natural_request,
                context=context,
                log_action_callback=log_action_callback,
                latency_metrics=latency_metrics,
                on_chunk=on_chunk,
                request_id=interaction_id,
                trace=trace,
                t0=t0
            )
        except Exception as e:
            log.warning("request_failed reason=%s", type(e).__name__)
            trace.record_stage("hub_response_emitted")
            trace.success = False
            trace.failure_category = classify_failure(e)
            trace.internal_error = type(e).__name__
            res = ConversationalResponse(status="error", message=get_user_friendly_error(trace.failure_category))
    
        # Finalize trace and metrics
        trace.record_stage("hub_response_emitted")
        trace.compute_durations()
        if res.status in ("success", "approval_required", "clarification_needed", "in_progress"):
            trace.success = True
        else:
            trace.success = False
            failure = res.data if isinstance(res.data, dict) else {}
            if failure.get("error_code") == "model_provider_unavailable":
                # AgentTurn returns structured provider errors instead of raising them.
                trace.failure_category = failure.get("primary_category") or "PROVIDER_ERROR"
            elif not trace.failure_category:
                trace.failure_category = "HARNESS_ERROR"
        trace.user_facing_message = res.message
        trace.log_summary()
    
        MetricsCollector.get_instance().record_trace(trace)
        trace_dict = trace.to_dict()
        latency_metrics.update(trace_dict)
        res.latency_metrics = trace_dict
        return res

    async def _process_request_impl(self, source_device_id, natural_request, context,
                                    log_action_callback, latency_metrics, on_chunk, request_id, trace, t0):
        trace.record_stage("routing_start")
        self.pipeline.router = self.router
        result = await self.pipeline.execute(source_device_id, natural_request, context, request_id)
        trace.routing_lane = "AGENT_TURN"
        trace.record_stage("routing_complete")
        return result

    def _map_intent_to_capability(self, intent):
        # Technical callers must provide a canonical registry identifier.
        from .capability_registry import CanonicalCapabilityRegistry
        capability = CanonicalCapabilityRegistry.get_instance().get_capability(intent)
        return capability.name if capability else ""

