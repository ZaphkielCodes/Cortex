"""Direct, bounded model calls. Models propose; Cortex still validates and executes."""
import asyncio
import json
import logging
import os
import re
import time

import httpx

from .intent_schema import ModelResult
from .context_compression import compact_json, compress_history
from .llm_provider import LLMProvider, ProviderHTTPError, ReasoningProviderUnavailable
from .tracing import observe_model_attempt

log = logging.getLogger("cortex.direct_provider")

# Explicit development/free-tier candidates, not an arbitrary paid-model gateway.
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY",
             {"qwen/qwen3.8-27b", "openai/gpt-oss-120b"}),
    "nvidia": ("https://integrate.api.nvidia.com/v1/chat/completions", "NVIDIA_API_KEY",
               {"deepseek-ai/deepseek-v4.1-flash", "z-ai/glm-5.3"}),
}


class DirectLLMProvider(LLMProvider):
    locality = "cloud_worker"

    def __init__(self, provider="groq", model_name=None, api_key=None, transport=None):
        if provider not in PROVIDERS:
            raise ValueError("Unsupported direct provider")
        self._name = provider
        self.endpoint, key_name, allowed = PROVIDERS[provider]
        self._model = model_name or ("qwen/qwen3.8-27b" if provider == "groq"
                                    else "deepseek-ai/deepseek-v4.1-flash")
        if self._model not in allowed:
            raise ValueError("Model is not an enabled free-tier/development candidate")
        self._api_key = api_key if api_key is not None else os.getenv(key_name, "")
        self.transport = transport
        self.supports_vision = self.model in {"qwen/qwen3.8-27b", "deepseek-ai/deepseek-v4.1-flash"}
        # NVIDIA trial terms prohibit personal/confidential input, even with user approval.
        self.max_privacy_class = "PUBLIC" if provider == "nvidia" else "SENSITIVE"
        self.last_usage = None
        self.last_rate_limits = {}
        self.last_error_metadata = None
        self.last_payload_metrics = None

    @property
    def name(self):
        return self._name

    @property
    def model(self):
        return self._model

    @property
    def configured(self):
        return bool(self._api_key)

    def payload(self, user_text, context):
        from .llm_provider import _GEMINI_SYSTEM_PROMPT
        from .reasoning_context import bounded_context
        mode = context.get("mode")
        raw = mode in {"agent_decision", "result_interpretation", "privacy_classification", "vision"}
        structured = not context.get("force_response_only") and mode != "result_interpretation"
        system = context.get("system_prompt")
        if not system:
            system = ("Answer concisely using supplied facts; never invent tool results."
                      if context.get("force_response_only") else _GEMINI_SYSTEM_PROMPT)
        if mode == "planning":
            system += ('\nReturn JSON {"type":"PLAN","plan":{"steps":[{"description":"...",'
                       '"capability":"exact catalog name","arguments":{}}]}}. '
                       'Use only available_capabilities; otherwise return an empty plan with unachievable_reason.')
        if context.get("force_task_orchestration"):
            system += "\nReturn a JSON ModelResult with type TASK_STEP and step_decision."
        a14_contract = context.get('a14_inference_contract') is True
        native = self.name=='groq' and mode=='agent_decision' and context.get('decision_schema')
        if structured and a14_contract and not native:
            from .orchestration_inference import contract_instruction
            system = contract_instruction(system, 'Return only a valid JSON object.')
        elif structured and not a14_contract:
            system += "\nReturn only a valid JSON object."
        # AgentTurn has already minimized evidence and built its catalog in system_prompt.
        # Preserve its full bounded history rather than truncating context a second time.
        message = {"request": user_text, "conversation_history": compress_history(context.get("conversation_history", [])),
                   "tool_result": context.get("tool_result"), "capability": context.get("capability"),
                   "goal": context.get("goal")}
        if not raw:
            message["context"] = bounded_context({k: context[k] for k in (
                "device", "memory", "people", "speaker", "current_speaker", "social_context",
                "world_state", "session", "available_capabilities", "task", "previous_result") if k in context})
        if mode == "planning":
            # Registry-generated schemas are not conversation lists: truncating to
            # twelve entries would silently remove most available capabilities.
            message["available_capabilities"] = context.get("available_capabilities", [])[:128]
            message["planning_state"] = bounded_context({k: context[k] for k in (
                "completed_steps", "failed_step", "observation", "is_replan", "target_device") if k in context})
        content = compact_json(message)
        if context.get("image_data_url"):
            if not self.supports_vision:
                raise ValueError("Selected model does not support images")
            content = [{"type": "text", "text": content},
                       {"type": "image_url", "image_url": {"url": context["image_data_url"]}}]
        payload = {"model": self.model, "stream": False,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                   "temperature": 0.1, "max_tokens": 1024 if structured else 768}
        if self.name == "groq" and mode == "agent_decision" and context.get("decision_schema"):
            schema = context['decision_schema']
            if context.get('a14_inference_contract') is True:
                from .orchestration_inference import compact_schema
                schema = dict(compact_schema(schema), **({'title':schema['title']} if 'title' in schema else {}))
                # Only remove an exact duplicate of this trusted schema. Text-only
                # transports still receive the complete schema in their prompt.
                marker = '\nA14_OUTPUT_SCHEMA:\n'
                prompt = payload['messages'][0]['content']
                if marker in prompt:
                    head, tail = prompt.rsplit(marker,1)
                    duplicate = tail.removesuffix('\nReturn only a valid JSON object.')
                    try: same = compact_schema(json.loads(duplicate)) == compact_schema(schema)
                    except ValueError: same = False
                    if same:
                        payload['messages'][0]['content'] = head + '\nUse the native function parameter schema; return only its JSON arguments.'
            payload["tools"]=[{"type":"function","function":{"name":"agent_decision",
                "description":"Return one validated Cortex decision proposal. The local runtime alone authorizes execution.",
                "parameters":schema}}]
            payload["tool_choice"]={"type":"function","function":{"name":"agent_decision"}}
            if context.get('a14_inference_contract') is True:
                from .orchestration_inference import contract_instruction
                instruction = (
                    'Native output contract: call the provided agent_decision function exactly once. '
                    'Pass the JSON proposal matching its parameter schema as function arguments. '
                    'Do not emit a text response or a JSON object outside that function call. '
                    'This function only submits a proposal; it does not execute any Cortex capability.')
                payload['messages'][0]['content'] = contract_instruction(payload['messages'][0]['content'], instruction)
        elif self.name == "groq" and structured:
            payload["response_format"] = {"type": "json_object"}
            if mode == "agent_decision":
                log.info("model_protocol_compat provider=%s reason=decision_schema_not_provided",self.name)
        return payload

    @observe_model_attempt
    async def generate_result(self, user_text, context, on_chunk=None):
        if not self.configured:
            raise ReasoningProviderUnavailable("PROVIDER_NOT_CONFIGURED")
        if self.name == "nvidia" and context.get("privacy_class") != "PUBLIC":
            raise ReasoningProviderUnavailable("PRIVATE_MODEL_REQUIRED")
        payload = self.payload(user_text, context)
        from .inference_metrics import payload_metrics
        self.last_payload_metrics = payload_metrics(payload)
        self.last_error_metadata = None
        log.info('model_wire_budget provider=%s model=%s metrics=%s', self.name,self.model,self.last_payload_metrics)
        text_bytes = sum(len(m["content"].encode("utf-8")) if isinstance(m["content"], str)
                         else len(m["content"][0]["text"].encode("utf-8")) for m in payload["messages"])
        log.info("model_prompt_budget provider=%s model=%s text_bytes=%s estimated_input_tokens=%s",
                 self.name, self.model, text_bytes, (text_bytes + 3) // 4)
        from .config import get_lane_deadlines
        from .routing_profiles import request_timeout
        timeout = request_timeout(context,get_lane_deadlines(context.get("routing_lane"))[1])
        started = time.monotonic()
        trace = context.get("trace")
        metrics = context.get("latency_metrics")
        self.last_usage = None
        if trace and hasattr(trace, "record_stage"):
            trace.record_stage("model_request_started", started)
        if metrics is not None:
            metrics["model_request_started"] = started
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                                             follow_redirects=False, timeout=timeout) as client:
                    response = await client.post(self.endpoint, headers={
                        "Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}, json=payload)
                    self.last_rate_limits = {k: response.headers[k] for k in (
                        "retry-after", "x-ratelimit-scope", "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
                        "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
                        "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens") if k in response.headers}
                    if response.status_code != 200:
                        error = ProviderHTTPError(self.name, response.status_code)
                        error.retry_after = response.headers.get("retry-after")
                        error.failure_scope = response.headers.get("x-ratelimit-scope") if response.headers.get("x-ratelimit-scope") in {"model","provider"} else None
                        error.quota_scopes = []
                        error.quota_metadata = {}
                        if response.status_code == 429 and not error.retry_after:
                            # Honor authoritative reset headers when Retry-After is absent.
                            # A nonzero request balance rules out a request-quota reset.
                            exhausted_requests = response.headers.get("x-ratelimit-remaining-requests") == "0"
                            reset = "x-ratelimit-reset-requests" if exhausted_requests else "x-ratelimit-reset-tokens"
                            error.retry_after = response.headers.get(reset)
                        # Never log provider error messages: they can echo prompts or credentials.
                        try:
                            provider_error = response.json().get("error") or {}
                            failed = provider_error.get('failed_generation')
                            failed_shape = None
                            if isinstance(failed,str):
                                failed_shape={'bytes':len(failed.encode()),'format':'unparsed'}
                                try:
                                    decoded=json.loads(failed)
                                    if isinstance(decoded,dict):
                                        failed_shape['format']='json_object'
                                        failed_shape['recognized_fields']=sorted(set(decoded)&{
                                            'decision','capability','arguments','response_text','name','parameters','tool_calls','function'})
                                except ValueError:
                                    if failed.lstrip().startswith(('<function=', '<tool_call>', '<|python_tag|>')):
                                        failed_shape['format']='tool_markup'
                            code = provider_error.get("code")
                            # Extract numeric quota metadata only; never retain/print raw messages.
                            message = provider_error.get("message") or ""
                            scope = re.findall(r"\b(?:TPM|TPD|RPM|RPD)\b", message)
                            quantities = re.findall(r"\b(Limit|Used|Requested)\s+(\d+)", message)
                            error.quota_scopes = sorted(set(scope))
                            numbers = {key:int(value) for key,value in quantities}
                            error.quota_metadata = {"quota_scope": ",".join(sorted(set(scope))) or None,
                                "known_limit":numbers.get("Limit"),"known_used":numbers.get("Used"),
                                "known_requested":numbers.get("Requested"),
                                "known_remaining":max(0,numbers["Limit"]-numbers["Used"]) if "Limit" in numbers and "Used" in numbers else None}
                            if response.status_code == 429:
                                if "TPD" in scope and not response.headers.get("retry-after"):
                                    # Minute token-reset headers cannot promise daily allowance.
                                    error.retry_after = None
                                elif "RPD" in scope and not response.headers.get("retry-after"):
                                    error.retry_after = response.headers.get("x-ratelimit-reset-requests")
                                log.warning("provider_quota provider=%s model=%s scope=%s quantities=%s", self.name,
                                            self.model, sorted(set(scope)),
                                            {key: int(value) for key,value in quantities})
                        except (ValueError, AttributeError, TypeError):
                            code = None
                            failed_shape = None
                        safe_codes = {"tool_use_failed", "json_validate_failed", "rate_limit_exceeded",
                            "context_length_exceeded", "model_not_found", "invalid_api_key", "invalid_request_error"}
                        error.provider_error_code = code if isinstance(code, str) and code in safe_codes else "unclassified"
                        # Preserve observations without rewriting status/classification
                        # or retaining bodies, organization IDs, keys, or prompts.
                        from .inference_metrics import safe_rate_headers
                        self.last_error_metadata = {'http_status':response.status_code,
                            'provider_error_code':error.provider_error_code,
                            'failed_generation_shape':failed_shape,
                            'quota_scopes':error.quota_scopes, 'quota_metadata':error.quota_metadata,
                            'rate_limit_headers':safe_rate_headers(self.last_rate_limits)}
                        log.warning('provider_failure_evidence provider=%s model=%s metadata=%s',
                                    self.name,self.model,self.last_error_metadata)
                        log.warning("direct_http_failure provider=%s status=%s code=%s", self.name,
                                    response.status_code, error.provider_error_code)
                        raise error
                    body = response.json()
                    choices = body.get("choices") or []
                    choice = choices[0] if choices else {}
                    native_proposal=bool(payload.get("tools"))
                    if native_proposal:
                        calls=choice.get("message",{}).get("tool_calls") or []
                        if (choice.get("finish_reason") != "tool_calls" or len(calls)!=1 or
                            calls[0].get("function",{}).get("name")!="agent_decision"):
                            raise ValueError("Expected exactly one native decision proposal")
                        text=calls[0]["function"].get("arguments")
                    else:
                        if choice.get("finish_reason") in {"length", "content_filter", "tool_calls"}:
                            raise ValueError("Incomplete or non-text model output")
                        text = choice.get("message", {}).get("content")
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("Empty model output")
                    self.last_usage = body.get("usage")
        except httpx.TimeoutException as exc:
            raise TimeoutError("Direct model deadline exceeded") from exc
        completed = time.monotonic()
        if trace:
            trace.model_provider = self.name
            if hasattr(trace, "record_stage"):
                trace.record_stage("model_complete", completed)
        if metrics is not None:
            metrics["model_complete"] = completed
        # This transport is non-streaming: never pretend completion time is TTFT.
        text = text.strip()
        if context.get("mode") in {"agent_decision", "result_interpretation", "privacy_classification", "vision"} or context.get("force_response_only"):
            if context.get("mode") in {"agent_decision", "privacy_classification", "vision"}:
                self._json(text)
            result = ModelResult(type="RESPONSE", response_text=text)
        else:
            parsed = self._json(text)
            try:
                result = ModelResult.model_validate(parsed)
                if result.type not in {"RESPONSE", "ACTION", "TASK", "TASK_STEP", "PLAN"}:
                    raise ValueError("Unknown ModelResult type")
            except ValueError as exc:
                raise ValueError("LLM output does not match ModelResult schema") from exc
        if on_chunk:
            await on_chunk(text)
        log.info("direct_model_complete provider=%s model=%s elapsed=%.3f", self.name, self.model, completed - started)
        return result

    @staticmethod
    def _json(text):
        if text.startswith("```") and text.endswith("```"):
            text = "\n".join(text.splitlines()[1:-1])
        try:
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError("Expected JSON object")
            return result
        except ValueError as exc:
            # Do not include model output or provider response bodies in errors/logs.
            raise ValueError("LLM returned invalid JSON") from exc
