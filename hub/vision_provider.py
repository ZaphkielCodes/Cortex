"""
hub/vision_provider.py
----------------------
Bounded Vision Provider abstraction for Cortex (Phase A2).

Provides multimodal understanding of captured OS screens.
Does NOT rely on OCR-first pipelines; uses true multimodal vision providers.

Supported providers:
- GeminiVisionProvider: Google Gemini API (gemini-3.8-flash with fallback models)
- MockVisionProvider: Deterministic provider for unit and integration testing
"""

import base64
import io
import json
import logging
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .screen_perception import ScreenObservation

log = logging.getLogger("cortex.vision")


class VisionProvider(ABC):
    """Abstract base class for Cortex multimodal vision providers."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Identifier name of the vision provider."""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Underlying model name used by the provider."""
        pass

    def is_available(self) -> bool:
        """Whether this vision provider is currently available and configured."""
        return True

    @abstractmethod
    async def analyze_image(
        self,
        image_ref: str,
        question: Optional[str] = None,
        mode: str = "describe",  # "describe" | "locate" | "inspect"
        device_id: Optional[str] = None,
    ) -> ScreenObservation:
        """
        Analyze an OS screenshot and produce a grounded ScreenObservation.
        """
        pass


class GeminiVisionProvider(VisionProvider):
    """Production provider using Google Gemini multimodal vision capabilities."""

    DEFAULT_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite"]
    FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash-lite"]

    def is_available(self) -> bool:
        return bool(self._api_key)

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._primary_model = model_name or os.environ.get("CORTEX_GEMINI_VISION_MODEL", "gemini-3.8-flash")
        self._client = None

        if not self._api_key:
            log.warning("GEMINI_API_KEY not configured. GeminiVisionProvider will not be available.")

    @property
    def provider_name(self) -> str:
        return "gemini_vision"

    @property
    def model_name(self) -> str:
        return self._primary_model

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _load_and_preprocess_image(self, image_ref: str):
        """
        Load an image from a file path or base64 data string.
        Optionally resizes if exceeding 1920x1080 for latency and efficiency.
        """
        from PIL import Image

        img = None
        if os.path.exists(image_ref):
            img = Image.open(image_ref)
        elif image_ref.startswith("data:image") or (len(image_ref) > 100 and "/" not in image_ref[:20]):
            raw_b64 = image_ref.split(",", 1)[-1]
            img_bytes = base64.b64decode(raw_b64)
            img = Image.open(io.BytesIO(img_bytes))
        else:
            p = Path(image_ref)
            if p.exists():
                img = Image.open(str(p))
            else:
                raise FileNotFoundError(f"Screenshot image reference not found: {image_ref}")

        # Ensure RGB mode
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Downscale gracefully if larger than 1920x1080 (preserves aspect ratio)
        max_dim = 1920
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        return img

    async def analyze_image(
        self,
        image_ref: str,
        question: Optional[str] = None,
        mode: str = "describe",
        device_id: Optional[str] = None,
    ) -> ScreenObservation:
        import asyncio

        if not self._api_key:
            raise RuntimeError("Gemini API key is not configured for vision analysis.")

        img = self._load_and_preprocess_image(image_ref)
        client = self._get_client()

        # Build prompt instructions based on mode and question
        system_prompt = (
            "You are Cortex's multimodal visual perception system. "
            "Inspect the provided screen capture and answer authoritatively based ONLY on what is visibly present.\n"
            "Do NOT speculate about processes or windows that are not visible in the image.\n"
            "Respond strictly in JSON matching this exact structure without markdown backticks:\n"
            "{\n"
            '  "summary": "<concise clear 1-2 sentence description directly answering the question or describing the screen>",\n'
            '  "visible_apps": ["<list of application names visibly open, e.g. Visual Studio Code, Chrome, Terminal, Notepad>"],\n'
            '  "visible_text_fragments": ["<key visible titles, headline text, error messages, or buttons>"],\n'
            '  "regions": [{"label": "<element name>", "confidence": 0.95}],\n'
            '  "confidence": <float between 0.0 and 1.0>\n'
            "}"
        )

        user_prompt = f"Mode: {mode}\n"
        if question:
            user_prompt += f"User Question: {question}\n"
        else:
            user_prompt += "User Question: What is visible on this screen?\n"

        if mode == "inspect":
            user_prompt += (
                "Focus inspection on: any visible error dialogs, exceptions, modal alerts, failure notices, "
                "or red warning text. Quote the exact error message if present."
            )
        elif mode == "locate":
            user_prompt += "Focus inspection on: identifying prominent interactive UI regions, buttons, and navigation bars."
        else:
            user_prompt += "Focus inspection on: what applications, documents, websites, or windows are active and visible."

        # Model trial list starting with primary model
        models_to_try = [self._primary_model]
        for m in self.DEFAULT_MODELS:
            if m not in models_to_try:
                models_to_try.append(m)

        raw_response_text = None
        used_model = self._primary_model
        t0 = time.monotonic()

        last_error = None
        for candidate_model in models_to_try:
            try:
                def _call():
                    resp = client.models.generate_content(
                        model=candidate_model,
                        contents=[img, user_prompt],
                        config={
                            "system_instruction": system_prompt,
                            "temperature": 0.1,
                            "max_output_tokens": 1024,
                        },
                    )
                    return resp.text

                loop = asyncio.get_running_loop()
                raw_response_text = await asyncio.wait_for(
                    loop.run_in_executor(None, _call),
                    timeout=20.0
                )
                used_model = candidate_model
                break
            except Exception as exc:
                last_error = exc
                log.warning(f"[GeminiVision] Model {candidate_model} failed ({exc}). Retrying with fallback...")

        elapsed = time.monotonic() - t0

        if raw_response_text is None:
            raise RuntimeError(f"Vision provider failed across all candidate models: {last_error}")

        # Parse JSON output
        parsed: Dict[str, Any] = {}
        try:
            cleaned = raw_response_text.strip()
            if cleaned.startswith("```"):
                lines = [l for l in cleaned.split("\n") if not l.strip().startswith("```")]
                cleaned = "\n".join(lines).strip()
            parsed = json.loads(cleaned)
        except Exception as parse_err:
            log.warning(f"[GeminiVision] JSON parse fallback: {parse_err}")
            parsed = {
                "summary": raw_response_text.strip()[:300],
                "visible_apps": [],
                "visible_text_fragments": [],
                "regions": [],
                "confidence": 0.8,
            }

        obs = ScreenObservation(
            observation_id=str(uuid.uuid4()),
            device_id=device_id or "unknown",
            captured_at=datetime.now(timezone.utc).isoformat(),
            summary=parsed.get("summary") or "Screen visual inspection completed.",
            visible_apps=list(parsed.get("visible_apps") or []),
            visible_text_fragments=list(parsed.get("visible_text_fragments") or []),
            regions=list(parsed.get("regions") or []),
            confidence=float(parsed.get("confidence", 0.95)),
            source_image_reference=str(image_ref),
            provenance={
                "provider": self.provider_name,
                "model": used_model,
                "latency_sec": round(elapsed, 3),
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
                "mode": mode,
            },
        )
        return obs


class MockVisionProvider(VisionProvider):
    """
    Deterministic Mock Vision Provider for testing, offline evaluation,
    and verifying failure/unavailable behavior without network requests.
    """

    def __init__(
        self,
        canned_observation: Optional[ScreenObservation] = None,
        should_fail: bool = False,
        failure_message: str = "Simulated vision provider failure",
        summary: Optional[str] = None,
        visible_apps: Optional[List[str]] = None,
        visible_text_fragments: Optional[List[str]] = None,
        regions: Optional[List[Dict[str, Any]]] = None,
        confidence: float = 0.95,
        is_avail: bool = True,
        is_available: bool = True,
    ):
        self.canned_observation = canned_observation
        self.should_fail = should_fail
        self.failure_message = failure_message
        self.calls: List[Dict[str, Any]] = []
        self._summary = summary
        self._visible_apps = visible_apps
        self._visible_text_fragments = visible_text_fragments
        self._regions = regions
        self._confidence = confidence
        self._is_available = is_avail and is_available

    def is_available(self) -> bool:
        return self._is_available

    @property
    def provider_name(self) -> str:
        return "mock_vision"

    @property
    def model_name(self) -> str:
        return "mock-screen-model"

    async def analyze_image(
        self,
        image_ref: str,
        question: Optional[str] = None,
        mode: str = "describe",
        device_id: Optional[str] = None,
    ) -> ScreenObservation:
        self.calls.append({
            "image_ref": image_ref,
            "question": question,
            "mode": mode,
            "device_id": device_id,
        })

        if self.should_fail or not self._is_available:
            raise RuntimeError(self.failure_message if self.should_fail else "screen analysis provider unavailable")

        if self.canned_observation:
            return self.canned_observation

        q_lower = (question or "").lower()

        # Dynamic deterministic mock response based on prompt questions
        if self._summary is not None:
            summary = self._summary
            apps = self._visible_apps if self._visible_apps is not None else ["MockApp"]
            text_frags = self._visible_text_fragments if self._visible_text_fragments is not None else ["MockText"]
            regions = self._regions if self._regions is not None else [{"label": apps[0], "confidence": self._confidence}]
            conf = self._confidence
        elif "error" in q_lower or mode == "inspect":
            summary = "An error dialog is visible: 'Connection Refused: Failed to connect to server on port 8080'."
            apps = ["ErrorDialog", "Visual Studio Code"]
            text_frags = ["Connection Refused", "Failed to connect to server on port 8080", "OK", "Cancel"]
            regions = [{"label": apps[0], "confidence": 0.98}]
            conf = 0.98
        elif "youtube" in q_lower or "browser" in q_lower:
            summary = "YouTube is visibly open in Google Chrome with the home feed playing video recommendations."
            apps = ["Google Chrome"]
            text_frags = ["YouTube", "Home", "Subscriptions", "Search"]
            regions = [{"label": apps[0], "confidence": 0.98}]
            conf = 0.98
        elif "app" in q_lower and ("which" in q_lower or "open" in q_lower):
            summary = "Visual Studio Code is open and active on screen displaying project files."
            apps = ["Visual Studio Code"]
            text_frags = ["Visual Studio Code", "agent_harness.py", "Explorer"]
            regions = [{"label": apps[0], "confidence": 0.98}]
            conf = 0.98
        elif "read" in q_lower:
            summary = "The screen shows a terminal running Python tests with '28 passed'."
            apps = ["Windows Terminal"]
            text_frags = ["test_phase_a1.py", "28 passed", "pytest"]
            regions = [{"label": apps[0], "confidence": 0.98}]
            conf = 0.98
        else:
            summary = "The screen displays a desktop workspace with Visual Studio Code open in the foreground."
            apps = ["Visual Studio Code"]
            text_frags = ["Cortex", "Visual Studio Code", "Terminal"]
            regions = [{"label": apps[0], "confidence": 0.98}]
            conf = 0.98

        return ScreenObservation(
            observation_id=str(uuid.uuid4()),
            device_id=device_id or "test_dev",
            captured_at=datetime.now(timezone.utc).isoformat(),
            summary=summary,
            visible_apps=apps,
            visible_text_fragments=text_frags,
            regions=regions,
            confidence=conf,
            source_image_reference=image_ref,
            provenance={
                "provider": self.provider_name,
                "model": self.model_name,
                "mode": mode,
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            },
        )


# ---------------------------------------------------------------------------
# Global Vision Provider Registry / Factory
# ---------------------------------------------------------------------------

_active_vision_provider: Optional[VisionProvider] = None
_vision_override_set: bool = False


def set_vision_provider_override(provider: Optional[VisionProvider]) -> None:
    """Explicitly set or clear the active vision provider (for testing)."""
    global _active_vision_provider, _vision_override_set
    _active_vision_provider = provider
    _vision_override_set = (provider is not None)


def get_vision_provider() -> Optional[VisionProvider]:
    """
    Retrieve the configured vision provider.
    Returns None if vision perception is disabled or unconfigured.
    """
    global _active_vision_provider, _vision_override_set
    if _vision_override_set:
        return _active_vision_provider

    # Check environment configuration
    enabled_env = os.environ.get("CORTEX_VISION_ENABLED", "true").lower()
    if enabled_env in ("false", "0", "no", "disabled"):
        return None

    default = "groq" if os.environ.get("CORTEX_LLM_PROVIDER", "direct").lower() in {"direct", "groq", "nvidia"} else "gemini"
    prov_type = os.environ.get("CORTEX_VISION_PROVIDER", default).lower()
    if prov_type in ("disabled", "none", "off"):
        return None

    if prov_type == "mock":
        return MockVisionProvider()

    if prov_type in {"groq", "direct"}:
        from .direct_router import DirectModelRouter
        from .direct_vision import DirectVisionProvider
        from .llm_provider import get_llm_provider
        router = get_llm_provider()
        while hasattr(router, "provider"):
            router = router.provider
        if not isinstance(router, DirectModelRouter):
            # A separate explicit Gemini/Ollama reasoning choice must not silently
            # enable vision without Groq credentials.
            return None
        provider = DirectVisionProvider(router)
        return provider if provider.is_available() else None

    if prov_type != "gemini":
        return None

    # Default to Gemini if API key is present
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        return GeminiVisionProvider(api_key=api_key)

    # No valid API key and no mock requested
    return None
