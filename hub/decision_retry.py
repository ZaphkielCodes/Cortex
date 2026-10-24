"""One correction shared by router/transport and AgentTurn proposal validation."""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass

current_decision = ContextVar('current_decision', default=None)


@dataclass
class DecisionRetry:
    invalid: int = 0
    selected: tuple | None = None
    privacy: str | None = None

    def select(self, provider, context):
        key = (provider.name, getattr(provider,"model",None))
        privacy = context.get('privacy_class','INTERNAL')
        if self.invalid and (key != self.selected or privacy != self.privacy):
            from .llm_provider import ProviderHTTPError
            raise ProviderHTTPError('correction_identity_guard',400)
        self.selected, self.privacy = key, privacy

    def reject(self):
        self.invalid += 1
        return self.invalid <= 1

    @contextmanager
    def scope(self):
        token=current_decision.set(self)
        try: yield
        finally: current_decision.reset(token)
