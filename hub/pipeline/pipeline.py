"""One production path: safety controls, then the model-driven AgentTurn."""
import logging
from ..intent_schema import ConversationalResponse
from .normalizer import InputNormalizer
from .deterministic_router import DeterministicSafetyRouter

log = logging.getLogger("cortex.pipeline")

class RequestPipeline:
    def __init__(self, control_layer, model_router=None):
        self.control, self.router = control_layer, model_router

    async def execute(self, source_device_id, natural_request, context, request_id="unknown"):
        norm = InputNormalizer.normalize(natural_request)
        if not norm.raw_text:
            return ConversationalResponse(status="clarification_needed", message="Please enter a request.")
        response = await DeterministicSafetyRouter.route(norm, source_device_id, context, self.control)
        if response is not None:
            return response
        if self.router is None:
            return ConversationalResponse(status="error", message="The reasoning provider is unavailable.")
        from ..agent_turn import AgentTurn
        result = await AgentTurn(self.control, self.router).run(natural_request, source_device_id, context, request_id)
        return ConversationalResponse(status=result.status, message=result.message, data=result.data,
            command_id=result.command_id, provenance=result.provenance, latency_metrics=result.latency_metrics)
