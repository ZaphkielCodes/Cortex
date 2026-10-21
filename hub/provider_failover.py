"""Safe error metadata determines retry scope, not user wording."""
from dataclasses import dataclass, field
from .llm_provider import ReasoningProviderUnavailable, classify_failure


@dataclass(frozen=True)
class Failure:
    kind: str
    category: str
    scope: str = "model"
    delay: float = 30
    retryable: bool = True
    safe_retry_after: bool = False
    reset_known: bool = False
    quota: dict = field(default_factory=dict)


class FailoverManager:
    MAX_ATTEMPTS = 6
    MAX_SAFE_RETRIES = 2

    @staticmethod
    def http_status(exc):
        value=getattr(exc,"status_code",None) or getattr(exc,"code",None)
        return value if type(value) is int and 100 <= value <= 599 else None

    @staticmethod
    def classify(exc):
        from .direct_router import retry_delay
        category = exc.primary_category if isinstance(exc,ReasoningProviderUnavailable) else classify_failure(exc)
        status = FailoverManager.http_status(exc)
        code = getattr(exc,"provider_error_code",None)
        if status == 429: category = "RATE_LIMIT_429"
        if status in {401,403}: category = "AUTH_ERROR"
        if status == 503: category = "SERVICE_UNAVAILABLE_503"
        if status in {408,504}: category = "DEADLINE_EXCEEDED"
        retry = getattr(exc,"retry_after",None)
        scopes = getattr(exc,"quota_scopes",[])
        quota = getattr(exc,"quota_metadata",{})
        model_scope = getattr(exc,"failure_scope",None) == "model"
        # HTTP payload status takes precedence over a contradictory provider code.
        # Oversized requests require verified capacity, never a quota retry.
        if status == 413:
            return Failure("CONTEXT_TOO_LARGE","CONTEXT_TOO_LARGE",delay=30)
        if category == "RATE_LIMIT_429":
            daily = bool(set(scopes)&{"TPD","RPD"})
            return Failure("DAILY_QUOTA_EXHAUSTED" if daily else "RATE_LIMIT",category,
                "model" if model_scope else "provider",retry_delay(retry,category) if retry else 900 if daily else 300,
                safe_retry_after=bool(retry) and not daily, reset_known=bool(retry) and daily,quota=quota)
        if category in {"AUTH_ERROR","PROVIDER_NOT_CONFIGURED"}:
            return Failure("AUTH_FAILURE",category,"provider",3600)
        if code in {"context_length_exceeded"} or category == "CONTEXT_TOO_LARGE" or status == 413:
            return Failure("CONTEXT_TOO_LARGE","CONTEXT_TOO_LARGE",delay=30)
        if code == "tool_use_failed" or category == "UNSUPPORTED_TOOL_USE": return Failure("UNSUPPORTED_TOOL_USE","INVALID_OUTPUT",delay=30)
        if code in {"model_not_found","json_validate_failed"} or category == "INVALID_OUTPUT" or isinstance(exc,ValueError):
            return Failure("MODEL_SPECIFIC_FAILURE","INVALID_OUTPUT",delay=30)
        if category == "DEADLINE_EXCEEDED": return Failure("TIMEOUT",category,delay=30)
        if category == "PROVIDER_COOLDOWN": return Failure("TEMPORARY_COOLDOWN",category,delay=30)
        if status in {400,422} or category == "INVALID_REQUEST":
            return Failure("INVALID_REQUEST","INVALID_REQUEST",retryable=False)
        if category.startswith(("PRIVATE_MODEL","PRIVACY_")):
            return Failure("PRIVACY_INELIGIBLE",category,retryable=False)
        return Failure("PROVIDER_SPECIFIC_FAILURE",category,"provider",60)
