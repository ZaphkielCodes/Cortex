"""
hub/metrics.py
--------------
Bounded Metrics Collector for Cortex Runtime Latency (Phase 8R-A/B).

Collects bounded rolling samples of monotonic durations across:
- STT latency
- routing latency
- LLM TTFT
- LLM total latency
- TTS first-audio latency
- wake -> first audio
- speech end -> first audio

Computes:
- count
- success rate
- fallback rate
- p50
- p95
"""

import math
from collections import deque
from typing import Dict, Any, List, Optional
from .tracing import InteractionTrace


def compute_percentile(data: List[float], p: float) -> float:
    """Compute percentile p (0 to 100) using linear interpolation."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    k = (n - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    d0 = sorted_data[int(f)] * (c - k)
    d1 = sorted_data[int(c)] * (k - f)
    return round(d0 + d1, 4)


class MetricsCollector:
    """Bounded, in-memory latency and reliability metrics aggregator."""

    METRIC_KEYS = [
        "stt_latency",
        "routing_latency",
        "llm_ttft",
        "llm_total_latency",
        "tts_first_audio_latency",
        "wake_to_first_audio",
        "speech_end_to_first_audio"
    ]

    _instance: Optional["MetricsCollector"] = None

    @classmethod
    def get_instance(cls, max_samples: int = 1000) -> "MetricsCollector":
        if cls._instance is None:
            cls._instance = cls(max_samples=max_samples)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset the singleton (useful for isolated tests)."""
        cls._instance = None

    def __init__(self, max_samples: int = 1000):
        self.max_samples = max_samples
        self.samples: Dict[str, deque] = {
            k: deque(maxlen=max_samples) for k in self.METRIC_KEYS
        }
        self.recent_traces: deque = deque(maxlen=max_samples)
        self.total_interactions: int = 0
        self.successful_interactions: int = 0
        self.fallback_interactions: int = 0

    def record_trace(self, trace: InteractionTrace):
        """Record completed interaction trace into metrics buffers."""
        trace.compute_durations()
        self.total_interactions += 1
        if trace.success:
            self.successful_interactions += 1
        if trace.fallback_occurred:
            self.fallback_interactions += 1

        self.recent_traces.append(trace)

        # Ingest durations into bounded deques
        dur = trace.stage_durations
        for k in self.METRIC_KEYS:
            if k in dur and dur[k] is not None and dur[k] >= 0:
                self.samples[k].append(dur[k])

    def record_direct(
        self,
        category: str,
        duration: float,
        success: bool = True,
        fallback: bool = False
    ):
        """Record an individual metric sample directly."""
        if category in self.samples and duration >= 0:
            self.samples[category].append(round(duration, 4))
        self.total_interactions += 1
        if success:
            self.successful_interactions += 1
        if fallback:
            self.fallback_interactions += 1

    def get_report(self) -> Dict[str, Any]:
        """Generate structured latency and reliability metrics summary."""
        count = self.total_interactions
        success_rate = round(self.successful_interactions / count, 4) if count > 0 else 1.0
        fallback_rate = round(self.fallback_interactions / count, 4) if count > 0 else 0.0

        stage_metrics = {}
        for k in self.METRIC_KEYS:
            vals = list(self.samples[k])
            n_vals = len(vals)
            if n_vals > 0:
                stage_metrics[k] = {
                    "count": n_vals,
                    "p50": compute_percentile(vals, 50.0),
                    "p95": compute_percentile(vals, 95.0),
                    "min": round(min(vals), 4),
                    "max": round(max(vals), 4),
                    "mean": round(sum(vals) / n_vals, 4)
                }
            else:
                stage_metrics[k] = {
                    "count": 0,
                    "p50": 0.0,
                    "p95": 0.0,
                    "min": 0.0,
                    "max": 0.0,
                    "mean": 0.0
                }

        return {
            "count": count,
            "success_rate": success_rate,
            "fallback_rate": fallback_rate,
            "metrics": stage_metrics
        }
