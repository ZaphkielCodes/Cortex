import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .intent_schema import ActionPlan, ModelResult
from .llm_provider import GeminiLLMProvider, MockLLMProvider, CircuitBreakerProvider

log = logging.getLogger("cortex.model_router")

class ModelRouter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    async def generate_result(self, user_text: str, context: Dict[str, Any], on_chunk: Optional[callable] = None) -> ModelResult:
        pass

class LocalModelRouter(ModelRouter):
    def __init__(self):
        from .local_model import OllamaLLMProvider
        self.provider = OllamaLLMProvider()

    @property
    def name(self) -> str:
        return "local_model"

    async def generate_result(self, user_text: str, context: Dict[str, Any], on_chunk: Optional[callable] = None) -> ModelResult:
        return await self.provider.generate_result(user_text, context, on_chunk)

class ProviderRouterAdapter(ModelRouter):
    def __init__(self, provider):
        self.provider = provider
        
    @property
    def name(self) -> str:
        return self.provider.name
        
    async def generate_result(self, user_text: str, context: Dict[str, Any], on_chunk: Optional[callable] = None) -> ModelResult:
        return await self.provider.generate_result(user_text, context, on_chunk)

def get_model_router() -> ModelRouter:
    from .llm_provider import get_llm_provider
    provider = get_llm_provider()
    return ProviderRouterAdapter(provider)
