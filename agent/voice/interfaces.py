"""
agent/voice/interfaces.py
-------------------------
Abstract Base Classes for Voice components.
"""

from abc import ABC, abstractmethod
from typing import Callable, Optional

class WakeWordProvider(ABC):
    @abstractmethod
    def start(self, on_wake: Callable[[], None]) -> None:
        """Start listening for the wake word in the background."""
        pass
        
    @abstractmethod
    def stop(self) -> None:
        """Stop listening for the wake word."""
        pass

class STTProvider(ABC):
    @abstractmethod
    async def listen(self) -> str:
        """Listen to the microphone and return transcribed text."""
        pass
        
    async def listen_stream(self):
        """Yield (is_final, text) tuples for streaming STT."""
        # Default fallback: just yield the final result
        result = await self.listen()
        yield True, result
        
    @abstractmethod
    def stop_listening(self) -> None:
        """Interrupt an ongoing listen operation."""
        pass

class TTSProvider(ABC):
    @abstractmethod
    async def speak(self, text: str) -> None:
        """Synthesize and play speech."""
        pass
        
    def speak_stream(self, chunk: str) -> None:
        """Stream speech synthesis for a text chunk."""
        # By default, do nothing or buffer. Providers can override this.
        pass
        
    @abstractmethod
    def stop_speaking(self) -> None:
        """Interrupt ongoing speech."""
        pass


class SpeakerSegment:
    """Represents a classified or labeled segment of speech."""
    def __init__(
        self,
        speaker_id: Optional[str] = None,
        label: str = "Unknown",
        confidence: float = 1.0,
        start_time: float = 0.0,
        end_time: float = 0.0,
        metadata: Optional[dict] = None
    ):
        self.speaker_id = speaker_id
        self.label = label
        self.confidence = confidence
        self.start_time = start_time
        self.end_time = end_time
        self.metadata = metadata or {}


class SpeakerResolver(ABC):
    """Abstract interface for resolving speaker identities from audio/transcripts."""
    @abstractmethod
    async def resolve_segment(
        self,
        audio_data: Optional[bytes] = None,
        transcript: Optional[str] = None
    ) -> SpeakerSegment:
        """Resolve speaker label, confidence, and metadata from audio segment or transcript."""
        pass

