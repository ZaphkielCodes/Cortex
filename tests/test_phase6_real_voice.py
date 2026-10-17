import pytest
import asyncio
import os
from unittest.mock import patch

try:
    from agent.voice.real_providers import LocalWakeWordProvider, VoskSTTProvider, PyTTSx3Provider
    REAL_VOICE_AVAILABLE = True
except ImportError:
    REAL_VOICE_AVAILABLE = False

@pytest.mark.asyncio
async def test_real_wake_word_lifecycle():
    if not REAL_VOICE_AVAILABLE:
        pytest.skip("Real voice libraries not installed.")
        
    provider = LocalWakeWordProvider()
    
    wake_triggered = False
    def on_wake():
        nonlocal wake_triggered
        wake_triggered = True
        
    # Test start and stop
    provider.start(on_wake)
    assert provider._running is True
    assert provider._thread is not None
    assert provider._thread.is_alive()
    
    # Let it run for a bit
    await asyncio.sleep(0.5)
    
    provider.stop()
    assert provider._running is False
    
    # Wait for thread to exit
    provider._thread.join(timeout=2.0)
    assert not provider._thread.is_alive()

@pytest.mark.asyncio
async def test_real_tts_lifecycle():
    if not REAL_VOICE_AVAILABLE:
        pytest.skip("Real voice libraries not installed.")
        
    provider = PyTTSx3Provider()
    
    # Test speak
    # We mock pyttsx3.init so it doesn't actually play audio in CI,
    # but we test the asyncio lifecycle
    with patch("pyttsx3.init") as mock_init:
        await provider.speak("Test")
        mock_init.assert_called_once()
        mock_engine = mock_init.return_value
        mock_engine.say.assert_called_once_with("Test")
        mock_engine.runAndWait.assert_called_once()

@pytest.mark.asyncio
async def test_real_stt_timeout():
    if not REAL_VOICE_AVAILABLE:
        pytest.skip("Real voice libraries not installed.")
        
    provider = VoskSTTProvider()
    
    # We will start listen and then immediately stop it to test cancellation
    task = asyncio.create_task(provider.listen())
    
    await asyncio.sleep(0.5)
    assert provider._listening is True
    
    # Simulate stopping listening
    provider.stop_listening()
    
    result = await task
    # Without audio, it should return empty string
    assert result == ""
    assert provider._listening is False
