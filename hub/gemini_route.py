"""Existing Gemini transport adapted to the same routing/health protocol."""
from .llm_provider import GeminiLLMProvider


class GeminiRoute(GeminiLLMProvider):
    locality="cloud_worker"
    max_privacy_class="SENSITIVE"
    # The existing transport does not project image_data_url; do not advertise it.
    supports_vision=False

    @property
    def configured(self): return bool(self._api_key)

    async def generate_result(self,user_text,context,on_chunk=None):
        # Retry/failover belongs to the canonical router, not nested SDK loops.
        result = await super().generate_result(user_text,dict(context,router_managed_retries=True))
        if on_chunk: await on_chunk(result.response_text or "")
        return result
