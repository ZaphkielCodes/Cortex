"""
hub/screen_perception.py
-------------------------
Structured visual perception and observation models for Cortex (Phase A2).

Provides:
- ScreenObservation: Structured container for visual screen analysis
- ScreenPerceptionManager: Ephemeral observation cache, signal publishing, and retention management
"""

import os
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("cortex.screen_perception")

# Ephemeral observation cache bound
MAX_OBSERVATIONS_HISTORY = 20

# Screenshot retention bounds
MAX_SCREENSHOTS_RETAINED = 20
SCREENSHOT_MAX_AGE_SECONDS = 3600  # 1 hour


@dataclass
class ScreenObservation:
    """Structured visual perception result grounded in an OS screenshot."""
    observation_id: str
    device_id: str
    captured_at: str
    summary: str
    visible_apps: List[str] = field(default_factory=list)
    visible_text_fragments: List[str] = field(default_factory=list)
    regions: List[Dict[str, Any]] = field(default_factory=list)
    confidence: float = 1.0
    source_image_reference: Optional[str] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScreenObservation":
        return cls(
            observation_id=data.get("observation_id") or str(uuid.uuid4()),
            device_id=data.get("device_id") or "unknown",
            captured_at=data.get("captured_at") or datetime.now(timezone.utc).isoformat(),
            summary=data.get("summary") or "Screen captured.",
            visible_apps=list(data.get("visible_apps") or []),
            visible_text_fragments=list(data.get("visible_text_fragments") or []),
            regions=list(data.get("regions") or []),
            confidence=float(data.get("confidence", 1.0)),
            source_image_reference=data.get("source_image_reference"),
            provenance=dict(data.get("provenance") or {}),
        )


class ScreenPerceptionManager:
    """
    Singleton managing visual observations, ephemeral memory,
    bounded screenshot file retention, and World State publishing.
    """

    _instance: Optional["ScreenPerceptionManager"] = None

    def __init__(self):
        self._observations: List[ScreenObservation] = []
        self._latest_by_device: Dict[str, ScreenObservation] = {}

    @classmethod
    def get_instance(cls) -> "ScreenPerceptionManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (used in test isolation)."""
        cls._instance = None

    def store_observation(self, observation: ScreenObservation) -> None:
        """Store a fresh visual observation in the bounded ephemeral cache."""
        self._observations.append(observation)
        if len(self._observations) > MAX_OBSERVATIONS_HISTORY:
            self._observations.pop(0)

        self._latest_by_device[observation.device_id] = observation

        # Safe logging: log metadata only, NEVER raw image bytes
        log.info(
            f"[ScreenPerception] Stored observation {observation.observation_id} | "
            f"device={observation.device_id} | apps={observation.visible_apps} | "
            f"confidence={observation.confidence:.2f}"
        )

    @classmethod
    def get_latest_observation(cls, device_id: Optional[str] = None) -> Optional[ScreenObservation]:
        """Get the most recent observation, optionally filtered by device."""
        instance = cls.get_instance()
        if device_id and device_id in instance._latest_by_device:
            return instance._latest_by_device[device_id]
        if instance._observations:
            return instance._observations[-1]
        return None

    async def publish_world_state_signals(self, observation: ScreenObservation) -> None:
        """
        Publish bounded, non-raw visual signals to the World State:
        - screen.foreground_app
        - screen.summary
        - screen.last_observed_at

        Raw image bytes/base64 are NEVER published to World State.
        """
        try:
            from .world_state import WorldStateManager, Signal

            wsm = WorldStateManager.get_instance()
            now_iso = datetime.now(timezone.utc).isoformat()
            dev_id = observation.device_id or "unknown"

            # Primary visible app
            primary_app = observation.visible_apps[0] if observation.visible_apps else "unknown"

            # 1. Foreground app signal
            await wsm.ingest_signal(
                Signal(
                    key=f"screen.foreground_app:{dev_id}",
                    source="screen_perception",
                    timestamp=now_iso,
                    confidence=observation.confidence,
                    ttl=120.0,
                    payload={"app": primary_app, "device_id": dev_id},
                )
            )

            # 2. Visual summary signal
            await wsm.ingest_signal(
                Signal(
                    key=f"screen.summary:{dev_id}",
                    source="screen_perception",
                    timestamp=now_iso,
                    confidence=observation.confidence,
                    ttl=120.0,
                    payload={"summary": observation.summary, "device_id": dev_id},
                )
            )

            # 3. Last observed timestamp signal
            await wsm.ingest_signal(
                Signal(
                    key=f"screen.last_observed_at:{dev_id}",
                    source="screen_perception",
                    timestamp=now_iso,
                    confidence=1.0,
                    ttl=300.0,
                    payload={"captured_at": observation.captured_at, "device_id": dev_id},
                )
            )

            log.info(
                f"[ScreenPerception] Published visual signals for device '{dev_id}': "
                f"foreground_app='{primary_app}'"
            )
        except Exception as exc:
            log.warning(f"[ScreenPerception] Could not publish world state signals: {exc}")

    @staticmethod
    def cleanup_ephemeral_screenshots(
        directory: Path,
        max_keep: Optional[int] = None,
        max_age_hours: Optional[float] = None,
    ) -> int:
        """
        Prune screenshots older than max_age_hours or exceeding max_keep
        to preserve privacy and storage bounds.
        Returns the number of files cleaned up.
        """
        if not directory.exists():
            return 0

        max_retained = max_keep if max_keep is not None else MAX_SCREENSHOTS_RETAINED
        max_age_secs = (max_age_hours * 3600.0) if max_age_hours is not None else SCREENSHOT_MAX_AGE_SECONDS

        files = sorted(
            [f for f in directory.iterdir() if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg")],
            key=lambda x: x.stat().st_mtime
        )

        now_ts = datetime.now().timestamp()
        removed_count = 0

        # Remove files older than TTL
        remaining = []
        for f in files:
            try:
                age = now_ts - f.stat().st_mtime
                if age > max_age_secs:
                    f.unlink(missing_ok=True)
                    removed_count += 1
                else:
                    remaining.append(f)
            except Exception:
                pass

        # If still exceeding capacity bound, prune oldest
        while len(remaining) > max_retained:
            oldest = remaining.pop(0)
            try:
                oldest.unlink(missing_ok=True)
                removed_count += 1
            except Exception:
                pass

        return removed_count
