import pytest
import asyncio
import json
from unittest.mock import MagicMock, patch
from agent.ui.settings import UISettings
from agent.ui_ipc import UIIPCServer
from agent.voice.controller import VoiceController, VoiceState
from agent.voice.mock_providers import MockWakeWordProvider, MockSTTProvider, MockTTSProvider

@pytest.fixture
def temp_settings_file(tmp_path):
    settings_file = tmp_path / "ui_settings.json"
    with patch("agent.ui.settings.SETTINGS_FILE", settings_file):
        yield settings_file

def test_settings_persistence(temp_settings_file):
    settings = UISettings()
    assert settings.get("orb_enabled") == True
    
    settings.set("orb_enabled", False)
    assert settings.get("orb_enabled") == False
    
    # Reload from file
    new_settings = UISettings()
    assert new_settings.get("orb_enabled") == False

@pytest.mark.asyncio
async def test_ipc_state_mapping():
    wakeword = MockWakeWordProvider()
    
    async def on_text(text):
        return "Notepad opened."
        
    state_updates = []
    def on_state(state):
        state_updates.append(state)
        
    vc = VoiceController(wakeword, MockSTTProvider(), MockTTSProvider(), on_text, on_state_change=on_state)
    
    vc.start()
    assert state_updates[-1] == VoiceState.IDLE
    
    vc.mute()
    assert state_updates[-1] == VoiceState.MUTED
    
    vc.unmute()
    assert state_updates[-1] == VoiceState.IDLE
    
    # Trigger listening
    wakeword.trigger()
    await asyncio.sleep(0.1)
    assert state_updates[-1] == VoiceState.LISTENING
    
    # It will transition to THINKING then SPEAKING
    await asyncio.sleep(2.1)
    assert state_updates[-2] == VoiceState.THINKING
    assert state_updates[-1] == VoiceState.SPEAKING
    
    # Wait for TTS
    await asyncio.sleep(1.0)
    assert state_updates[-1] == VoiceState.IDLE

@pytest.mark.asyncio
async def test_ui_ipc_server():
    server = UIIPCServer(host="127.0.0.1", port=0)
    await server.start()
    assert server.port > 0
    
    # Test sending a message
    # Mock a client
    class MockClient:
        def __init__(self):
            self.data = []
        def write(self, data):
            self.data.append(data)
            
    client = MockClient()
    server.clients.append(client)
    server.send({"type": "state", "state": "listening"})
    
    assert len(client.data) == 1
    assert b"listening" in client.data[0]
    
    server.server.close()
    await server.server.wait_closed()
