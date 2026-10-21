"""
hub/llm_provider.py
-------------------
LLM Provider abstraction for Cortex.

The LLM is sandboxed: it receives a user request and sanitised context,
and MUST return a structured ActionPlan.  It never has access to
executors, shell commands, devices, files, permissions, or credentials.

Providers:
  - MockLLMProvider   â€” deterministic, for tests
  - GeminiLLMProvider â€” Google Gemini (google-genai SDK)
"""

import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .intent_schema import ModelResult
from .tracing import observe_model_attempt

log = logging.getLogger("cortex.llm")


class ProviderHTTPError(RuntimeError):
    """Safe provider failure metadata without credential-bearing response bodies."""
    def __init__(self, provider, status_code):
        self.status_code = status_code
        super().__init__(f"{provider} returned HTTP {status_code}")


class ReasoningProviderUnavailable(RuntimeError):
    def __init__(self, primary_category, fallback_category=None, fallback_status=None):
        self.primary_category = primary_category
        self.fallback_category = fallback_category
        self.fallback_status = fallback_status
        super().__init__("Reasoning providers unavailable")

    @property
    def user_message(self):
        if self.primary_category.startswith(("LOCAL_MODEL", "PRIVATE_MODEL", "PRIVACY_CLASSIFICATION")):
            return "Private reasoning is unavailable. Start the configured local Ollama model; no request was sent to a cloud model."
        if self.primary_category == "PROVIDER_NOT_CONFIGURED":
            return "The AI provider is not configured. Set a supported provider and its API key in Cortex settings."
        if self.primary_category == "CONTEXT_TOO_LARGE":
            return "No eligible model with sufficient verified context capacity is available. Completion is unverified."
        if self.primary_category == "PROVIDER_COOLDOWN":
            return "The AI service is temporarily cooling down after a recent failure. Please try again shortly. No new model request was sent."
        if self.primary_category == "RATE_LIMIT_429":
            primary = "The primary AI service has reached its usage limit."
        elif self.primary_category == "SERVICE_UNAVAILABLE_503":
            primary = "The primary AI service is temporarily unavailable. Please try again shortly."
        elif self.primary_category == "DEADLINE_EXCEEDED":
            primary = "The primary AI service timed out."
        else:
            primary = "The primary AI service is unavailable."
        if self.fallback_status == 402:
            return primary + " The backup service requires payment (HTTP 402)."
        if self.fallback_status == 429:
            return primary + " The backup service has also reached its usage limit."
        return primary + (" The backup service also failed." if self.fallback_category else "")

# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Base class for all LLM providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""
        ...

    @property
    @abstractmethod
    def model(self) -> str:
        """Model identifier string."""
        ...

    @abstractmethod
    async def generate_result(
        self,
        user_text: str,
        context: Dict[str, Any],
        on_chunk: Optional[callable] = None
    ) -> ModelResult:
        """
        Given natural language and device/memory context, return a
        validated ActionPlan.  Must raise on malformed output.
        If on_chunk is provided and the provider supports it, it may yield text chunks.
        """
        ...


# ---------------------------------------------------------------------------
# Offline provider placeholder
# ---------------------------------------------------------------------------

class MockLLMProvider(LLMProvider):
    """Offline placeholder. Tests needing decisions inject a scripted provider."""

    @property
    def name(self):
        return "mock"

    @property
    def model(self):
        return "mock"

    async def generate_result(self, user_text, context, on_chunk=None):
        # No keyword rules, canned execution plans or notification fallback.
        raise ReasoningProviderUnavailable("PROVIDER_NOT_CONFIGURED")


_GEMINI_SYSTEM_PROMPT = """\

You are the intelligence engine for Cortex, a personal AI assistant.



Your ONLY job is to convert a natural-language user request into a structured JSON ModelResult.

You must NEVER:

- Execute commands yourself

- Access files, shells, devices, or credentials

- Invent device IDs — if the target is ambiguous, set target_device_reference to the user's exact words and set confidence below 0.7



If the user's request contains ambiguous references like "it", "that", "do it", or "close it", use the provided Conversation History and Active References to resolve the reference. 

If the user says "do it" or similar, and there is a "last_action" or "last_intent" in the Active References, output an ACTION corresponding to that intent.

If there are multiple possible interpretations or multiple recently discussed targets without a clear dominant one, you MUST return a RESPONSE asking for clarification. Do NOT guess the target. If you are uncertain, return a RESPONSE asking for clarification.





You MUST output ONLY a single JSON object matching this schema (no markdown, no explanation):

{

  "type": "<one of: RESPONSE, ACTION, TASK>",

  "response_text": "<Your conversational reply if type is RESPONSE, else null>",

  "action_plan": {

    "intent": "<one of: open_app, show_notification, move_file, get_device_status, resume_session, list_online_devices, retrieve_recent_file, retrieve_recent_screenshot, create_task, read_task_status, store_memory, remember_person, get_person, search_people, update_person, get_current_speaker, resolve_speaker, link_speaker_person, list_recent_speakers, get_social_context, remember_social_interaction, get_world_state, update_signal, get_device_state, get_recent_events>",

    "source_device": "<from context>",

    "target_device": null,

    "target_device_reference": null, // ALWAYS null unless the user explicitly names a remote device (e.g., 'on the living room pc')

    "capability": "<one of: app.launch, ui.notify, fs.move, device.info, session.resume, hub.read, fs.read, screenshot.read, task.write, task.read, memory.write, people.remember, people.get, people.search, people.update, speaker.get_current, speaker.resolve, speaker.link_person, speaker.list_recent, social.context, social.remember_interaction, context.get_world_state, context.update_signal, context.get_device_state, context.get_recent_events>",

    "action": "<the executor command name, e.g. 'open_app', 'move_file', 'show_notification', 'take_screenshot', 'get_status', 'store_memory', 'remember_person', 'get_person', 'search_people', 'update_person', 'get_current_speaker', 'resolve_speaker', 'link_speaker_person', 'list_recent_speakers', 'get_social_context', 'remember_social_interaction', 'get_world_state', 'update_signal', 'get_device_state', 'get_recent_events'>",

    "arguments": { ... },

    "requires_confirmation": <true if destructive/sensitive, false for safe actions like opening apps>,

    "confidence": <0.0 to 1.0>,

    "user_facing_summary": "<short phrase describing what will happen>"

  }

}



NOTE: If type is "RESPONSE" or "TASK", you MUST set "action_plan" to null.



Use "RESPONSE" for conversational queries, questions, or explanations where no device action is needed and the answer cannot be retrieved by a capability.

Use "ACTION" (and fill out "action_plan") when the user asks you to perform a single specific action on a device, OR when they ask a question that can be answered by retrieving data via one of the available capabilities (e.g. asking "what is my battery" -> use get_device_status).

Use "TASK" when the user requests a multi-step workflow, logic that requires verifying the outcome of an action before proceeding, or multiple consecutive operations (e.g. 'Open Calculator and then tell me my battery', or 'Prepare these files and send them when my laptop reconnects').



MEMORY & PEOPLE POLICY:

Do not treat all conversation as permanent memory. Avoid blindly writing arbitrary permanent memories. 

Write memory explicitly using the "store_memory" intent only for: device relationships, recurring preferences, project facts, explicit user facts/preferences.

For people, friends, family, coworkers, communication preferences, and personal facts:

Use "remember_person" -> capability "people.remember" (action: "remember_person").

Do NOT write memory for: random one-off conversation, transient app launches, temporary failures, sarcasm, jokes, daily moods, or speculative judgments.



SOCIAL & RELATIONSHIP CONTEXT:

- You are Cortex, a unified assistant identity. Adapt tone naturally based on who is speaking:

  * Primary User: collaborative, direct, helpful

  * Friend: warm, familiar, casual

  * Colleague / Manager: professional, polite, concise

  * Family / Sibling: familiar, warm, respectful

  * Unknown speaker: neutral, polite, objective

- In multi-speaker conversations, consider stated constraints and preferences of present participants (e.g. food dislikes, timing, preferences).



Intent-to-capability mapping:

  open_app -> app.launch (action: open_app)

  show_notification -> ui.notify (action: show_notification)

  move_file -> fs.move (action: move_file)

  get_device_status -> device.info (action: get_status)

  resume_session -> session.resume

  list_online_devices -> hub.read

  retrieve_recent_file -> fs.read

  retrieve_recent_screenshot -> screenshot.read (action: take_screenshot)

  create_task -> task.write

  read_task_status -> task.read

  store_memory -> memory.write (action: store_memory)

  remember_person -> people.remember (action: remember_person)

  get_person -> people.get (action: get_person)

  search_people -> people.search (action: search_people)

  update_person -> people.update (action: update_person)

  get_current_speaker -> speaker.get_current (action: get_current_speaker)

  resolve_speaker -> speaker.resolve (action: resolve_speaker)

  link_speaker_person -> speaker.link_person (action: link_speaker_person)

  list_recent_speakers -> speaker.list_recent (action: list_recent_speakers)

  get_social_context -> social.context (action: get_social_context)

  remember_social_interaction -> social.remember_interaction (action: remember_social_interaction)

  get_world_state -> context.get_world_state (action: get_world_state)

  update_signal -> context.update_signal (action: update_signal)

  get_device_state -> context.get_device_state (action: get_device_state)

  get_recent_events -> context.get_recent_events (action: get_recent_events)

  calendar_get_current -> calendar.get_current (action: calendar_get_current)

  calendar_get_next -> calendar.get_next (action: calendar_get_next)

  calendar_get_availability -> calendar.get_availability (action: calendar_get_availability)

  calendar_status -> calendar.status (action: calendar_status)

  telemetry_get_system_usage -> telemetry.get_system_usage (action: telemetry_get_system_usage)

  telemetry_get_hardware_info -> telemetry.get_hardware_info (action: telemetry_get_hardware_info)



WORLD STATE & SITUATION AWARENESS:

- When world_state is present in Context, use it to understand the user's situation:

  * Location: current place/label (e.g. home, office, library)

  * Calendar: current/upcoming meeting, free/busy status

  * Foreground activity: what app or window the user is currently using

  * Active task: background or ongoing multi-step task

  * Device status: battery percentage, network connectivity

  * System telemetry: CPU, RAM, disk, GPU, specs, machine model

- Answer situational questions naturally using these fresh signals.

- DO NOT collapse unrelated queries into get_device_status / get_status!

  * Device identity / model ("what device am I using", "what laptop model is this") -> use get_device_state or answer from context.world_state.

  * System telemetry ("what is my CPU usage", "how much RAM am I using", "how much disk space do I have left") -> use telemetry_get_system_usage or answer from context.world_state.

  * Machine specs / hardware ("what GPU do I have", "what CPU do I have", "laptop specs") -> use telemetry_get_hardware_info or answer from context.world_state.

  * Calendar queries -> use calendar capabilities or answer from context.world_state. If disconnected, clearly state disconnected. Do not hallucinate events.

  * Internet / network connectivity ("am I connected to the internet") -> use get_device_state with argument {"aspect": "network"} or answer from context.world_state. NEVER route network queries to get_status.

  * Only battery percentage / power queries should route to get_device_status (action: get_status).

  * If a signal is unavailable, report that it is unavailable. Never substitute unrelated device information.



PHASE 8D — AUTHORITATIVE TIME/DATE/TIMEZONE GROUNDING:

- The world_state object ALWAYS contains these runtime temporal fields sourced from the OS clock:

  * current_datetime: ISO-8601 local datetime string (authoritative)

  * current_date: local date string (YYYY-MM-DD)

  * current_time: local time in 12-hour format (e.g. "11:45 PM")

  * day_of_week: current day name (e.g. "Friday")

  * tomorrow_date: tomorrow's date (YYYY-MM-DD)

  * tomorrow_name: tomorrow's day name (e.g. "Saturday")

  * timezone: OS system timezone name (e.g. "Eastern Standard Time", "Eastern Daylight Time")

  * utc_offset: UTC offset string (e.g. "UTC-04:00")

- CRITICAL: When answering "what time is it?", "what day is it?", "what's today's date?", "what day is tomorrow?", "what timezone are you on?":

  * ALWAYS use context.world_state.current_time, current_date, day_of_week, tomorrow_date, timezone, utc_offset.

  * NEVER guess, estimate, or use training-data knowledge for these. The runtime values are always present.

- When answering calendar queries, use context.world_state.current_date and tomorrow_date to correctly determine which events are "today" or "tomorrow".

- When the user asks a follow-up calendar question like "what about tomorrow?" after a calendar query, interpret it as "what calendar events do I have tomorrow?" Do NOT ask for clarification.



Action-to-arguments mapping:

  open_app: {"app": "<application name>"}

  show_notification: {"title": "<title>", "message": "<body>"}

  move_file: {"src": "<source path>", "dst": "<destination path>"}

  take_screenshot: {}

  get_status: {}

  get_device_state: {"aspect": "<'identity' | 'network' | 'battery' | null>", "device_id": "<optional device id>"}

  get_world_state: {}

  store_memory: {"key": "<key>", "value": "<value>", "category": "<category>"}

  remember_person: {"name": "<name>", "relationship_type": "<optional relationship, e.g. brother, friend>", "aliases": ["<optional aliases>"], "notes": "<optional notes>", "key": "<optional fact key>", "value": "<optional fact value>"}

  get_person: {"name": "<name or pronoun>"}

  search_people: {"query": "<query>"}

  update_person: {"name": "<name>", "relationship_type": "<optional>", "notes": "<optional>"}



Permission guidance (for requires_confirmation):

  - Low-risk (requires_confirmation=false): opening apps, reading device info, taking screenshots, reading tasks, listing devices, notifications

  - High-risk (requires_confirmation=true): moving/deleting/editing files, sending messages/email, installs, purchases, destructive shell commands, sensitive external actions



If the user says something like "open Spotify" or "open Notepad", that is low-risk: requires_confirmation=false, confidence=0.95.

If the user asks to delete or move a file, that is higher-risk: requires_confirmation=true.



CRITICAL RULE ON APP LAUNCHES:

If the user's input is a single word or ambiguous (e.g. "Spotify", "Calculator") WITHOUT a clear action verb (like "open", "launch", "start"), DO NOT output an ACTION to open the app. Instead, output a RESPONSE explaining what the app is or asking for clarification.

Only output an open_app ACTION if the user explicitly requests an action (e.g. "Open Spotify", "Launch Calculator").

"""


class ModelDeadlineExceeded(TimeoutError):
    """Raised when a model request exceeds its TTFT or total generation deadline."""
    pass


def classify_failure(e: Exception) -> str:
    """Classify model exceptions into deterministic categories."""
    if isinstance(e, ProviderHTTPError) and e.status_code == 413:
        return "CONTEXT_TOO_LARGE"
    if isinstance(e, ModelDeadlineExceeded):
        return "DEADLINE_EXCEEDED"
    err_str = str(e).lower()
    if isinstance(e, TimeoutError) or "timeout" in err_str or "time out" in err_str or "deadline" in err_str:
        return "DEADLINE_EXCEEDED"
    if any(x in err_str for x in ["429", "quota", "resource_exhausted", "too many requests"]):
        return "RATE_LIMIT_429"
    if any(x in err_str for x in ["503", "unavailable", "service unavailable", "overloaded", "high demand"]):
        return "SERVICE_UNAVAILABLE_503"
    if any(x in err_str for x in ["401", "403", "unauthorized", "forbidden", "api key"]):
        return "AUTH_ERROR"
    if any(x in err_str for x in ["json", "schema", "validation failed", "malformed"]):
        return "INVALID_OUTPUT"
    if any(x in err_str for x in ["connect", "network", "clienterror", "connection refused"]):
        return "NETWORK_ERROR"
    return "PROVIDER_ERROR"


def get_user_friendly_error(category: Optional[str] = None) -> str:
    """Deterministic, friendly error messages for user. Never leaks raw API exceptions."""
    return "I couldn't complete that request right now."


class GeminiLLMProvider(LLMProvider):
    """Production provider using Google Gemini API."""

    def __init__(self, api_key: Optional[str] = None, model_name: str = "gemini-3.8-flash"):
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._model_name = model_name
        self._client = None

        if not self._api_key:
            log.warning("GEMINI_API_KEY not set. GeminiLLMProvider will fail on requests.")

    def _get_client(self):
        if self._client is None:
            from google import genai
            from .config import DEADLINE_STRUCTURED_TOTAL
            # Bound the underlying HTTP request as well as the coroutine. SDK
            # retries otherwise keep running in the executor after wait_for times out.
            self._client = genai.Client(api_key=self._api_key, http_options={
                "timeout": int(max(10.0, DEADLINE_STRUCTURED_TOTAL) * 1000),
                "retry_options": {"attempts": 1},
            })
        return self._client

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model_name

    @observe_model_attempt
    async def generate_result(
        self, user_text: str, context: Dict[str, Any], on_chunk: Optional[callable] = None
    ) -> ModelResult:
        import asyncio
        from .config import get_lane_deadlines

        client = self._get_client()

        # Build a safe context summary (no secrets)
        safe_context = {
            "source_device_id": context.get("device", {}).get("id", "unknown"),
            "source_device_name": context.get("device", {}).get("name", "unknown"),
            "source_device_capabilities": context.get("device", {}).get("capabilities", []),
        }
        
        session = context.get("session", {})
        if session:
            from .session_context import SessionContextManager
            safe_context["conversation_history"] = SessionContextManager.format_context_for_prompt(session)

        if "memory" in context:
            safe_context["memories"] = context["memory"]

        if "people" in context:
            safe_context["people"] = context["people"]

        if "speaker" in context:
            safe_context["speaker"] = context["speaker"]

        if "current_speaker" in context:
            safe_context["current_speaker"] = context["current_speaker"]

        if "social_context" in context:
            safe_context["social_context"] = context["social_context"]

        if "world_state" in context:
            safe_context["world_state"] = context["world_state"]

        force_task_orchestration = context.get("force_task_orchestration", False)
        force_response = context.get("force_response_only", False)
        mode_planning = context.get("mode") == "planning"
        mode_agent_decision = context.get("mode") == "agent_decision"
        mode_result_interpretation = context.get("mode") == "result_interpretation"

        if mode_agent_decision:
            # AgentTurn: model produces structured JSON decision.
            # system_prompt is pre-built with capability catalog + world state.
            system_inst = context.get("system_prompt", "You are the Cortex AI reasoning engine. Return a structured JSON AgentDecision.")
            conv_history = context.get("conversation_history", [])
            history_text = ""
            if conv_history:
                history_text = "\n".join(f"{t.get('role','user').upper()}: {t.get('text','')}" for t in conv_history)
                history_text = f"\nCONVERSATION HISTORY:\n{history_text}\n"
            user_message = (
                f"{history_text}"
                f"USER REQUEST: \"{user_text}\"\n\n"
                f"Respond with ONLY the JSON decision object."
            )
            force_response = True  # Return raw text as response_text

        elif mode_result_interpretation:
            # AgentTurn result interpretation: user_text IS the full prompt.
            system_inst = context["system_prompt"]
            user_message = json.dumps({"request": user_text, "capability": context.get("capability"),
                                       "goal": context.get("goal"), "tool_result": context.get("tool_result")})
            force_response = True

        elif mode_planning:
            caps_json = json.dumps(context.get("available_capabilities", []), indent=2)
            user_message = (
                f"User Goal: \"{user_text}\"\n\n"
                f"Available Capabilities:\n{caps_json}\n\n"
                f"Respond with ONLY a valid JSON ModelResult object with type='PLAN' and 'plan' containing 'steps' or 'unachievable_reason'."
            )
            system_inst = (
                "You are the Cortex Goal Planner. Convert the user goal into a bounded execution plan. "
                "Output ONLY a JSON ModelResult object: "
                "{\"type\": \"PLAN\", \"plan\": {\"steps\": [{\"description\": \"...\", \"capability\": \"...\", \"arguments\": {...}}]}}. "
                "Every capability MUST be an exact match from the Available Capabilities list. "
                "If the goal cannot be accomplished using the available capabilities, output: "
                "{\"type\": \"PLAN\", \"plan\": {\"steps\": [], \"unachievable_reason\": \"<clear reason>\"}}."
            )
        elif force_task_orchestration:
            user_message = f"{user_text}\n\nContext:\n{json.dumps(safe_context, indent=2)}"
            system_inst = "You are the Cortex Phase 6 Task Orchestrator. Output ONLY a valid JSON ModelResult object with type='TASK_STEP'."
        elif force_response:
            user_message = f"User request: \"{user_text}\"\n\nContext:\n{json.dumps(safe_context, indent=2)}\n\nRespond conversationally directly. No JSON."
            system_inst = "You are Cortex, a helpful AI assistant. Answer directly, conversationally, concisely. No JSON formatting."
        else:
            user_message = (
                f"User request: \"{user_text}\"\n\n"
                f"Context:\n{json.dumps(safe_context, indent=2)}\n\n"
                f"Respond with ONLY the JSON ModelResult object."
            )
            system_inst = _GEMINI_SYSTEM_PROMPT

        lane = context.get("routing_lane") or ("STRUCTURED_MODEL" if mode_agent_decision or mode_result_interpretation
                                               else "FAST_RESPONSE" if force_response else "STRUCTURED_MODEL")
        deadline_ttft, deadline_total = get_lane_deadlines(lane)
        from .routing_profiles import request_timeout
        deadline_total = request_timeout(context,deadline_total)

        t0 = time.monotonic()
        trace = context.get("trace")
        latency_metrics = context.get("latency_metrics")
        if trace and hasattr(trace, "record_stage"):
            trace.record_stage("model_request_started", t0)
        if latency_metrics is not None:
            latency_metrics["model_request_started"] = t0

        log.info(f"[Latency] Model provider selected: Gemini (lane={lane}, ttft_deadline={deadline_ttft:.1f}s, total_deadline={deadline_total:.1f}s)")
        log.info(f"[Latency] Gemini request start: 0.000s")
        loop = asyncio.get_running_loop()

        if os.environ.get("CORTEX_FORCE_GEMINI_FAILURE") == "true":
            raise Exception("503 Service Unavailable: Synthetic test failure")

        if force_response and on_chunk:
            def _stream_gemini():
                return client.models.generate_content_stream(
                    model=self._model_name,
                    contents=user_message,
                    config={
                        "system_instruction": system_inst,
                        "temperature": 0.1,
                        "max_output_tokens": 512,
                    },
                )
            
            def _get_next(iterator):
                try:
                    return next(iterator)
                except StopIteration:
                    return None
                    
            try:
                response_iter = await asyncio.wait_for(
                    loop.run_in_executor(None, _stream_gemini),
                    timeout=deadline_ttft
                )
                
                first_chunk = await asyncio.wait_for(
                    loop.run_in_executor(None, _get_next, response_iter),
                    timeout=deadline_ttft
                )
                
                t_first = time.monotonic()
                has_first_token = bool(first_chunk is not None and first_chunk.text)
                if has_first_token and trace and hasattr(trace, "record_stage"):
                    trace.record_stage("model_first_token", t_first)
                if has_first_token and latency_metrics is not None:
                    latency_metrics["model_first_token"] = t_first

                full_text = ""
                if first_chunk is not None and first_chunk.text:
                    full_text += first_chunk.text
                    await on_chunk(first_chunk.text)
                
                while True:
                    elapsed = time.monotonic() - t0
                    remaining = deadline_total - elapsed
                    if remaining <= 0:
                        raise ModelDeadlineExceeded(f"Gemini generation exceeded total deadline ({deadline_total:.1f}s)")
                    
                    chunk = await asyncio.wait_for(
                        loop.run_in_executor(None, _get_next, response_iter),
                        timeout=remaining
                    )
                    if chunk is None:
                        break
                    chunk_text = chunk.text
                    if chunk_text:
                        if not has_first_token:
                            has_first_token = True
                            t_first = time.monotonic()
                            if trace and hasattr(trace, "record_stage"):
                                trace.record_stage("model_first_token", t_first)
                            if latency_metrics is not None:
                                latency_metrics["model_first_token"] = t_first
                        full_text += chunk_text
                        await on_chunk(chunk_text)
                
                t_complete = time.monotonic()
                if trace and hasattr(trace, "record_stage"):
                    trace.record_stage("model_complete", t_complete)
                if latency_metrics is not None:
                    latency_metrics["model_complete"] = t_complete

                elapsed = t_complete - t0
                log.info(f"[Gemini] Stream finished in {elapsed:.2f}s")
                log.info(f"[Latency] Gemini request end: {elapsed:.3f}s")
                return ModelResult(type="RESPONSE", response_text=full_text)
            except asyncio.TimeoutError:
                t_err = time.monotonic()
                log.info(f"[Latency] Gemini request end (timeout): {t_err - t0:.3f}s")
                raise ModelDeadlineExceeded(f"Gemini streaming request timed out ({deadline_total:.1f}s)")
            except Exception as e:
                log.error(f"[Gemini] Streaming failed: {e}")
                log.info(f"[Latency] Gemini request end (error): {time.monotonic() - t0:.3f}s")
                raise

        async def _call_gemini():
            response = await client.aio.models.generate_content(
                model=self._model_name,
                contents=user_message,
                config={
                    "system_instruction": system_inst,
                    "temperature": 0.1,
                    "max_output_tokens": 2048 if mode_agent_decision else 512,
                    **({"response_mime_type": "application/json"} if mode_agent_decision else {}),
                    **({"http_options":{"timeout":int(deadline_total*1000),"retry_options":{"attempts":1}}}
                       if context.get("router_managed_retries") and context.get("_router_timeout_seconds") else {}),
                },
            )
            return response.text

        try:
            # Retry only an explicitly failed inference request, never a capability
            # or an ambiguous timeout. Both attempts share one total time budget.
            async def _request_with_recovery():
                for attempt in range(2):
                    try:
                        return await _call_gemini()
                    except Exception as exc:
                        remaining = deadline_total - (time.monotonic() - t0)
                        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                        if attempt or status != 503 or remaining <= 1.0 or context.get("router_managed_retries"):
                            raise
                        log.info("gemini_retry reason=temporary_503 attempt=2")
                        await asyncio.sleep(.5)
            raw_text = await asyncio.wait_for(_request_with_recovery(), timeout=deadline_total)
        except asyncio.TimeoutError:
            t_err = time.monotonic()
            log.info(f"[Latency] Gemini request end (timeout): {t_err - t0:.3f}s")
            raise ModelDeadlineExceeded(f"Gemini API request timed out after {deadline_total:.1f}s")

        t_complete = time.monotonic()
        if trace and hasattr(trace, "record_stage"):
            trace.record_stage("model_complete", t_complete)
        if latency_metrics is not None:
            latency_metrics["model_complete"] = t_complete

        elapsed = t_complete - t0
        log.info(f"[Gemini] Response received in {elapsed:.2f}s")
        log.info(f"[Latency] Gemini request end: {elapsed:.3f}s")

        if force_response:
            return ModelResult(type="RESPONSE", response_text=raw_text.strip())

        # Parse and validate
        try:
            cleaned = raw_text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines)

            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            err_snip = raw_text[:300] if raw_text else "None"
            log.error(f"[Gemini] Malformed JSON response: {err_snip} | Error: {e}")
            raise ValueError(f"LLM returned invalid JSON: {e}. Raw: {err_snip}")

        try:
            res = ModelResult(**parsed)
        except Exception as e:
            err_snip = raw_text[:300] if raw_text else "None"
            log.error(f"[Gemini] ModelResult validation failed: {e} | Raw JSON: {err_snip}")
            raise ValueError(f"LLM output does not match ModelResult schema: {e}")

        if res.type == "ACTION" and res.action_plan:
            log.info(
                f"[Gemini] intent={res.action_plan.intent} action={res.action_plan.action} "
                f"confidence={res.action_plan.confidence:.2f} target_ref={res.action_plan.target_device_reference}"
            )
        else:
            log.info(f"[Gemini] type={res.type} response_text={bool(res.response_text)}")
            
        return res


# ---------------------------------------------------------------------------
# Circuit breaker and fallback provider
# ---------------------------------------------------------------------------

class UnavailableLLMProvider(LLMProvider):
    """A configuration error must not silently enable deterministic demo behavior."""
    name = "unavailable"
    model = "not-configured"
    configured = False

    async def generate_result(self, user_text, context, on_chunk=None):
        raise ReasoningProviderUnavailable("PROVIDER_NOT_CONFIGURED")


class CircuitBreakerProvider(LLMProvider):
    def __init__(self, primary: LLMProvider, fallback: Optional[LLMProvider] = None):
        self.primary = primary
        self.fallback = fallback
        self.failures = 0
        self.circuit_open_until = 0.0
        self.last_primary_failure_category = None
        # Authoritative interaction result tracking (prevents duplicate execution/responses)
        self._interaction_winners: Dict[str, str] = {}

    @property
    def name(self) -> str:
        return f"cb({self.primary.name})"

    @property
    def model(self) -> str:
        return self.primary.model

    async def generate_result(self, user_text: str, context: Dict[str, Any], on_chunk: Optional[callable] = None) -> ModelResult:
        now = time.time()
        interaction_id = context.get("interaction_id") or context.get("request_id") or context.get("trace_id") or uuid.uuid4().hex[:8]
        trace = context.get("trace")

        if self.circuit_open_until and now < self.circuit_open_until:
            log.warning(f"[{self.name}] Circuit open ({self.circuit_open_until - now:.1f}s remaining). Routing directly to fallback.")
            if trace:
                trace.fallback_occurred = True
                trace.failure_category = "CIRCUIT_OPEN"
            return await self._try_fallback(user_text, context, on_chunk, interaction_id,
                                            self.last_primary_failure_category or "CIRCUIT_OPEN")

        try:
            plan = await self.primary.generate_result(user_text, context, on_chunk)
            
            # Authoritative check: If fallback already won for this interaction, discard primary
            if interaction_id in self._interaction_winners and self._interaction_winners[interaction_id] != self.primary.name:
                log.warning(f"[{self.name}] Late response from primary for interaction {interaction_id} discarded (fallback already won).")
                return self._interaction_winners[interaction_id + "_result"]
            
            self._interaction_winners[interaction_id] = self.primary.name
            if self.failures > 0:
                log.info(f"[{self.name}] Provider recovered. Resetting circuit breaker.")
                self.failures = 0
                self.circuit_open_until = 0.0
            if trace:
                trace.model_provider = self.primary.name
            return plan

        except Exception as e:
            cat = classify_failure(e)
            self.last_primary_failure_category = cat
            log.warning(f"[{self.name}] Primary ({self.primary.name}) failed with category={cat}: {e}")
            
            if trace:
                trace.failure_category = cat
                trace.internal_error = str(e)

            # Update circuit breaker health
            if cat == "RATE_LIMIT_429":
                self.failures = 3
                self.circuit_open_until = now + 300.0
                log.warning(f"[{self.name}] Quota exhausted on primary. Tripping circuit breaker for 300s.")
            else:
                self.failures += 1
                if self.failures >= 2:
                    self.circuit_open_until = now + 60.0
                    log.error(f"[{self.name}] Circuit breaker tripped! Open for 60s.")

            # Do NOT blindly retry the same failing provider! Fail over immediately to fallback.
            if self.fallback and context.get("allow_provider_fallback", True):
                if trace:
                    trace.fallback_occurred = True
                log.info(f"[{self.name}] Failing over immediately to fallback provider: {self.fallback.name}")
                return await self._try_fallback(user_text, context, on_chunk, interaction_id, cat)

            if context.get("mode") in ("agent_decision", "result_interpretation"):
                raise ReasoningProviderUnavailable(cat) from e
            # If no fallback is available:
            log.error(f"[{self.name}] No fallback provider available. Returning deterministic failure message.")
            if trace:
                trace.success = False
            return ModelResult(
                type="RESPONSE",
                response_text=get_user_friendly_error(cat)
            )

    async def _try_fallback(
        self,
        user_text: str,
        context: Dict[str, Any],
        on_chunk: Optional[callable] = None,
        interaction_id: Optional[str] = None,
        primary_failure_cat: Optional[str] = None
    ) -> ModelResult:
        trace = context.get("trace")
        fb_cat, fallback_status = None, None
        if self.fallback and context.get("allow_provider_fallback", True):
            try:
                log.info(f"[{self.name}] Invoking fallback provider: {self.fallback.name}")
                if trace:
                    trace.fallback_occurred = True
                    trace.model_provider = self.fallback.name
                res = await self.fallback.generate_result(user_text, context, on_chunk)
                
                # Mark fallback as authoritative winner
                if interaction_id:
                    self._interaction_winners[interaction_id] = self.fallback.name
                    self._interaction_winners[interaction_id + "_result"] = res
                return res
            except Exception as e:
                fb_cat = classify_failure(e)
                fallback_status = getattr(e, "status_code", None)
                log.error(f"[{self.name}] Fallback provider ({self.fallback.name}) also failed ({fb_cat}): {e}")
                if trace:
                    trace.success = False
                    trace.failure_category = f"FALLBACK_FAILED_{fb_cat}"
                    trace.internal_error = f"Primary: {primary_failure_cat} | Fallback: {e}"

        if context.get("mode") in ("agent_decision", "result_interpretation"):
            raise ReasoningProviderUnavailable(primary_failure_cat, fb_cat, fallback_status)
        if trace:
            trace.success = False
        return ModelResult(
            type="RESPONSE",
            response_text=get_user_friendly_error(primary_failure_cat)
        )

_active_provider: Optional[LLMProvider] = None

def get_llm_provider() -> LLMProvider:
    """Return the currently configured LLM provider."""
    global _active_provider
    if _active_provider is None:
        _active_provider = _create_default_provider()
        if os.environ.get("CORTEX_LOCAL_MODEL") and os.environ.get("CORTEX_LLM_PROVIDER", "direct").lower() not in {"mock", "ollama"}:
            from .local_model import OllamaLLMProvider, PrivacyAwareRouter
            _active_provider = PrivacyAwareRouter(_active_provider, OllamaLLMProvider())
    return _active_provider

def set_llm_provider(provider: LLMProvider) -> None:
    """Override the active provider (useful for tests)."""
    global _active_provider
    _active_provider = provider
    log.info(f"LLM provider set to: {provider.name} ({provider.model})")

def _create_default_provider() -> LLMProvider:
    """Create provider based on environment configuration."""
    from dotenv import load_dotenv
    load_dotenv()

    provider_name = os.environ.get("CORTEX_LLM_PROVIDER", "direct").lower()
    
    if provider_name in {"direct", "groq", "nvidia"}:
        from .direct_provider import DirectLLMProvider
        from .direct_router import DirectModelRouter
        from .provider_health import ProviderHealthStore
        try:
            if provider_name == "direct":
                router = DirectModelRouter()
            else:
                if provider_name == "nvidia" and os.getenv("CORTEX_ENABLE_NVIDIA_TRIAL", "0") != "1":
                    return UnavailableLLMProvider()
                model = os.getenv("CORTEX_GROQ_MODEL", "qwen/qwen3.8-27b") if provider_name == "groq" else os.getenv("CORTEX_NVIDIA_MODEL", "deepseek-ai/deepseek-v4.1-flash")
                provider = DirectLLMProvider(provider_name, model)
                router = DirectModelRouter([provider] if provider.configured else [],health=ProviderHealthStore.default())
        except ValueError:
            log.warning("Invalid direct provider model configuration; reasoning is unavailable")
            return UnavailableLLMProvider()
        return router if router.configured else UnavailableLLMProvider()
    elif provider_name == "ollama":
        from .local_model import OllamaLLMProvider
        return OllamaLLMProvider()
    elif provider_name == "mock":
        log.info("Using MockLLMProvider (CORTEX_LLM_PROVIDER=mock)")
        return MockLLMProvider()
    elif provider_name == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            log.warning("GEMINI_API_KEY not set; reasoning is unavailable")
            return UnavailableLLMProvider()
        else:
            model = os.environ.get("CORTEX_GEMINI_MODEL", "gemini-3.8-flash")
            log.info(f"Using GeminiLLMProvider (model={model})")
            primary = GeminiLLMProvider(api_key=api_key, model_name=model)
            
            from .direct_router import DirectModelRouter
            from .gemini_route import GeminiRoute
            from .provider_health import ProviderHealthStore
            return DirectModelRouter([GeminiRoute(api_key=api_key,model_name=model)],health=ProviderHealthStore.default())
    else:
        log.warning("Unsupported provider configured; reasoning is unavailable")
        return UnavailableLLMProvider()
