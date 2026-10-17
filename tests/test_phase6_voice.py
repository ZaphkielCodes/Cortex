import pytest
import asyncio
from agent.voice.controller import VoiceController, VoiceState
from agent.voice.mock_providers import MockWakeWordProvider, MockSTTProvider, MockTTSProvider

@pytest.mark.asyncio
async def test_voice_controller_states():
    wakeword = MockWakeWordProvider()
    stt = MockSTTProvider()
    tts = MockTTSProvider()
    
    async def on_text(text):
        assert text == "open notepad"
        return "Notepad opened."
        
    vc = VoiceController(wakeword, stt, tts, on_text)
    
    # Initially idle
    assert vc.state == VoiceState.IDLE
    
    vc.start()
    assert wakeword._running
    
    # Simulate wake word
    wakeword.trigger()
    
    # Give the task loop time to transition
    await asyncio.sleep(0.1)
    
    # Should be listening
    assert vc.state == VoiceState.LISTENING
    assert stt._listening
    assert not wakeword._running # Paused
    
    # Wait for STT to finish (it sleeps for 2 seconds)
    await asyncio.sleep(2.1)
    
    # Should be speaking now because the think step is fast
    assert vc.state == VoiceState.SPEAKING
    assert tts._speaking
    
    # Wait for TTS to finish
    await asyncio.sleep(1.0)
    
    # Should be back to IDLE
    assert vc.state == VoiceState.IDLE
    assert wakeword._running

@pytest.mark.asyncio
async def test_voice_controller_mute():
    wakeword = MockWakeWordProvider()
    vc = VoiceController(wakeword, MockSTTProvider(), MockTTSProvider(), None)
    
    vc.start()
    assert wakeword._running
    
    vc.mute()
    assert vc.state == VoiceState.MUTED
    assert not wakeword._running
    
    # Should not start if muted
    vc.start()
    assert vc.state == VoiceState.MUTED
    assert not wakeword._running
    
    vc.unmute()
    assert vc.state == VoiceState.IDLE
    assert wakeword._running
