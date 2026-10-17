"""
hub/tracing.py
--------------
Runtime Latency Observability & End-to-End Interaction Tracing for Cortex (Phase 8R-A/B).

Captures the full 14-stage lifecycle of every voice / natural language interaction:
1.  wake_detected
2.  microphone_open
3.  speech_first_detected
4.  speech_end
5.  stt_complete
6.  hub_request_received
7.  routing_complete
8.  model_request_started
9.  model_first_token
10. model_complete
11. tts_request_started
12. tts_first_audio
13. playback_started
14. playback_complete

Never logs secrets, OAuth tokens, device secrets, or API keys.
Uses monotonic clocks for all latency calculations; wall-clock timestamps for audit.
"""

import time
import uuid
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Optional

log = logging.getLogger("cortex.trace")

# Defense-in-depth: Regex for scrubbing potential tokens/secrets from logs/traces
_SECRET_PATTERNS = [
    re.compile(r'sk-or-v1-[0-9a-zA-Z\-_]{20,}'),    # OpenRouter key
    re.compile(r'AIza[0-9A-Za-z\-_]{20,}'),          # Google API key
    re.compile(r'AQ\.[0-9A-Za-z\-_]{20,}'),          # Gemini token
    re.compile(r'gsk_[0-9A-Za-z]{20,}'),            # Groq key
    re.compile(r'AKIA[0-9A-Z]{16}'),                # AWS Access Key
    re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*'),   # Bearer tokens
    re.compile(r'[0-9a-fA-F]{64}'),                 # 32-byte hex (device secret)
]


def scrub_secrets(text: Any) -> Any:
    """Scrub secrets, OAuth tokens, device secrets, or API keys from any string or structure."""
    if isinstance(text, str):
        scrubbed = text
        for pat in _SECRET_PATTERNS:
            scrubbed = pat.sub("<REDACTED>", scrubbed)
        return scrubbed
    elif isinstance(text, dict):
        return {k: scrub_secrets(v) for k, v in text.items()}
    elif isinstance(text, list):
        return [scrub_secrets(v) for v in text]
    return text


TRACE_STAGES = [
    "wake_detected",
    "microphone_open",
    "speech_first_detected",
    "speech_end",
    "stt_complete",
    "hub_request_received",
    "routing_complete",
    "model_request_started",
    "model_first_token",
    "model_complete",
    "tts_request_started",
    "tts_first_audio",
    "playback_started",
    "playback_complete"
]


@dataclass
class InteractionTrace:
    trace_id: str = field(default_factory=lambda: f"tr_{uuid.uuid4().hex[:12]}")
    session_id: str = field(default_factory=lambda: f"ses_{uuid.uuid4().hex[:8]}")
    interaction_id: str = field(default_factory=lambda: f"ix_{uuid.uuid4().hex[:6]}")
    
    # Model & Routing metadata
    model_provider: Optional[str] = None
    routing_lane: Optional[str] = None
    fallback_occurred: bool = False
    failure_category: Optional[str] = None
    success: bool = True
    
    # Timestamps (monotonic floats for duration calculations)
    timestamps: Dict[str, float] = field(default_factory=dict)
    
    # Wall-clock ISO timestamps for auditing/logs
    wall_clock_timestamps: Dict[str, str] = field(default_factory=dict)
    
    # Derived stage durations (in seconds)
    stage_durations: Dict[str, float] = field(default_factory=dict)
    
    # Key totals
    total_wake_to_first_audio: Optional[float] = None
    total_speech_end_to_first_audio: Optional[float] = None
    
    # Internal error detail (never shown to user)
    internal_error: Optional[str] = None
    user_facing_message: Optional[str] = None

    def record_stage(self, stage: str, t_mono: Optional[float] = None, t_wall: Optional[str] = None):
        """Record a stage timestamp using monotonic clock by default."""
        mono_val = time.monotonic() if t_mono is None else t_mono
        self.timestamps[stage] = mono_val
        self.wall_clock_timestamps[stage] = (
            datetime.now(timezone.utc).isoformat() if t_wall is None else t_wall
        )

    def compute_durations(self):
        """Compute derived stage durations and critical end-to-end metrics."""
        ts = self.timestamps

        # Stage: STT latency
        if "stt_complete" in ts and "microphone_open" in ts:
            self.stage_durations["stt_latency"] = round(ts["stt_complete"] - ts["microphone_open"], 4)
        elif "stt_complete" in ts and "speech_end" in ts:
            self.stage_durations["stt_latency"] = round(ts["stt_complete"] - ts["speech_end"], 4)

        # Stage: Routing latency
        if "routing_complete" in ts and "hub_request_received" in ts:
            self.stage_durations["routing_latency"] = round(ts["routing_complete"] - ts["hub_request_received"], 4)

        # Stage: LLM TTFT (Time To First Token)
        if "model_first_token" in ts and "model_request_started" in ts:
            self.stage_durations["llm_ttft"] = round(ts["model_first_token"] - ts["model_request_started"], 4)
        elif "model_complete" in ts and "model_request_started" in ts and "model_first_token" not in ts:
            # For non-streaming requests, TTFT equals total generation latency
            self.stage_durations["llm_ttft"] = round(ts["model_complete"] - ts["model_request_started"], 4)

        # Stage: LLM Total latency
        if "model_complete" in ts and "model_request_started" in ts:
            self.stage_durations["llm_total_latency"] = round(ts["model_complete"] - ts["model_request_started"], 4)

        # Stage: TTS first-audio latency
        if "tts_first_audio" in ts and "tts_request_started" in ts:
            self.stage_durations["tts_first_audio_latency"] = round(ts["tts_first_audio"] - ts["tts_request_started"], 4)

        # Stage: Audio playback duration
        if "playback_complete" in ts and "playback_started" in ts:
            self.stage_durations["playback_duration"] = round(ts["playback_complete"] - ts["playback_started"], 4)

        # End-to-end: wake -> first audio
        if "tts_first_audio" in ts and "wake_detected" in ts:
            self.total_wake_to_first_audio = round(ts["tts_first_audio"] - ts["wake_detected"], 4)
            self.stage_durations["wake_to_first_audio"] = self.total_wake_to_first_audio

        # End-to-end: speech_end -> first audio
        if "tts_first_audio" in ts and "speech_end" in ts:
            self.total_speech_end_to_first_audio = round(ts["tts_first_audio"] - ts["speech_end"], 4)
            self.stage_durations["speech_end_to_first_audio"] = self.total_speech_end_to_first_audio

    def to_dict(self) -> Dict[str, Any]:
        """Convert trace to a safe serializable dictionary."""
        self.compute_durations()
        raw = {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "interaction_id": self.interaction_id,
            "model_provider": self.model_provider,
            "routing_lane": self.routing_lane,
            "fallback_occurred": self.fallback_occurred,
            "failure_category": self.failure_category,
            "success": self.success,
            "timestamps": self.timestamps,
            "wall_clock_timestamps": self.wall_clock_timestamps,
            "stage_durations": self.stage_durations,
            "total_wake_to_first_audio": self.total_wake_to_first_audio,
            "total_speech_end_to_first_audio": self.total_speech_end_to_first_audio,
            "internal_error": self.internal_error,
            "user_facing_message": self.user_facing_message
        }
        return scrub_secrets(raw)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InteractionTrace":
        """Instantiate trace from dictionary."""
        t = cls(
            trace_id=d.get("trace_id") or f"tr_{uuid.uuid4().hex[:12]}",
            session_id=d.get("session_id") or f"ses_{uuid.uuid4().hex[:8]}",
            interaction_id=d.get("interaction_id") or f"ix_{uuid.uuid4().hex[:6]}",
            model_provider=d.get("model_provider"),
            routing_lane=d.get("routing_lane"),
            fallback_occurred=bool(d.get("fallback_occurred", False)),
            failure_category=d.get("failure_category"),
            success=bool(d.get("success", True)),
            timestamps=d.get("timestamps", {}),
            wall_clock_timestamps=d.get("wall_clock_timestamps", {}),
            stage_durations=d.get("stage_durations", {}),
            total_wake_to_first_audio=d.get("total_wake_to_first_audio"),
            total_speech_end_to_first_audio=d.get("total_speech_end_to_first_audio"),
            internal_error=d.get("internal_error"),
            user_facing_message=d.get("user_facing_message")
        )
        return t

    def log_summary(self):
        """Emit a formatted latency trace log summary."""
        self.compute_durations()
        dur = self.stage_durations
        parts = [
            f"[Trace] id={self.trace_id}",
            f"lane={self.routing_lane or 'NONE'}",
            f"provider={self.model_provider or 'NONE'}",
            f"fallback={'YES' if self.fallback_occurred else 'NO'}",
            f"status={'OK' if self.success else 'FAILED'}"
        ]
        if self.failure_category:
            parts.append(f"fail_cat={self.failure_category}")
        if "stt_latency" in dur:
            parts.append(f"stt={dur['stt_latency']:.3f}s")
        if "routing_latency" in dur:
            parts.append(f"routing={dur['routing_latency']:.3f}s")
        if "llm_ttft" in dur:
            parts.append(f"ttft={dur['llm_ttft']:.3f}s")
        if "llm_total_latency" in dur:
            parts.append(f"llm_tot={dur['llm_total_latency']:.3f}s")
        if "tts_first_audio_latency" in dur:
            parts.append(f"tts_fa={dur['tts_first_audio_latency']:.3f}s")
        if self.total_speech_end_to_first_audio is not None:
            parts.append(f"speech_end->first_audio={self.total_speech_end_to_first_audio:.3f}s")
        if self.total_wake_to_first_audio is not None:
            parts.append(f"wake->first_audio={self.total_wake_to_first_audio:.3f}s")

        log.info(" | ".join(parts))
