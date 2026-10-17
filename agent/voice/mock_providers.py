"""
agent/voice/mock_providers.py
-----------------------------
Mock implementations of Voice interfaces for testing.
"""

import asyncio
import logging
from typing import Callable, Optional
from .interfaces import WakeWordProvider, STTProvider, TTSProvider

log = logging.getLogger("cortex.voice.mock")

class MockWakeWordProvider(WakeWordProvider):
    def __init__(self, wake_word: str = "Yo Cortex"):
        self.wake_word = wake_word
        self._running = False
        self._task = None
        self._on_wake = None
        
    def start(self, on_wake: Callable[[], None]) -> None:
        self._running = True
        self._on_wake = on_wake
        log.info(f"[MockWakeWord] Started listening for '{self.wake_word}' locally...")
        
    def stop(self) -> None:
        self._running = False
        log.info("[MockWakeWord] Stopped listening.")
        
    def trigger(self):
        """Mock method to simulate a wake word detection."""
        if self._running and self._on_wake:
            log.info(f"[MockWakeWord] '{self.wake_word}' detected!")
            self._on_wake()

class MockSTTProvider(STTProvider):
    def __init__(self):
        self._listening = False
        
    async def listen(self) -> str:
        self._listening = True
        log.info("[MockSTT] Listening to microphone...")
        # Simulate taking some time to listen
        await asyncio.sleep(2)
        text = "open notepad" # Mock transcribed text
        log.info(f"[MockSTT] Heard: '{text}'")
        self._listening = False
        return text
        
    def stop_listening(self) -> None:
        self._listening = False
        log.info("[MockSTT] Stopped listening.")

class MockTTSProvider(TTSProvider):
    def __init__(self):
        self._speaking = False
        
    async def speak(self, text: str) -> None:
        self._speaking = True
        log.info(f"[MockTTS] Speaking: '{text}'")
        # Simulate time taken to speak
        await asyncio.sleep(len(text) * 0.05)
        log.info("[MockTTS] Finished speaking.")
        self._speaking = False
        
    def stop_speaking(self) -> None:
        self._speaking = False
        log.info("[MockTTS] Interrupted speech.")


class MockSpeakerResolver:
    """Mock speaker resolver that yields configurable speaker segments."""
    def __init__(self, default_label: str = "Primary User", default_speaker_id: str = "spk_primary"):
        self.current_label = default_label
        self.current_speaker_id = default_speaker_id
        self.current_confidence = 1.0

    def set_speaker(self, label: str, speaker_id: Optional[str] = None, confidence: float = 1.0):
        self.current_label = label
        self.current_speaker_id = speaker_id or f"spk_{label.lower().replace(' ', '_')}"
        self.current_confidence = confidence

    async def resolve_segment(self, audio_data: bytes = None, transcript: str = None):
        from .interfaces import SpeakerSegment
        return SpeakerSegment(
            speaker_id=self.current_speaker_id,
            label=self.current_label,
            confidence=self.current_confidence,
            metadata={"source": "mock_speaker_resolver"}
        )
