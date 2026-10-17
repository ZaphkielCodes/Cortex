import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from agent.agent import _session

@pytest.mark.asyncio
async def test_agent_session_no_diagnostic_nameerror():
    """
    Ensures that the Agent's _session loop can run without throwing NameError
    from removed diagnostic code.
    """
    
    stop_event = asyncio.Event()
    
    # Mock WebSocket that just returns a command and then blocks
    class MockWS:
        def __init__(self):
            self.recv_called = False
            
        async def recvself(self):
            if not self.recv_called:
                self.recv_called = True
                return '{"type": "command", "command": "dummy", "args": [], "command_id": "123"}'
            else:
                while not stop_event.is_set():
                    await asyncio.sleep(0.1)
                raise asyncio.TimeoutError()
                
        async def recv(self):
            return await self.recvself()
            
        async def send(self, data):
            pass
            
    mock_ws = MockWS()
    
    # Mock UIIPCServer
    mock_ui = MagicMock()
    
    # Mock VoiceController
    mock_voice = MagicMock()
    mock_voice.start = MagicMock()
    mock_voice.stop = MagicMock()
    mock_voice.state = "idle"
    
    active_state = {'ws': mock_ws, 'voice_futures': {}, 'active_cmd_id': None}
    
    # Patch authenticate so it succeeds
    with patch('agent.agent._authenticate', new_callable=AsyncMock) as mock_auth:
        mock_auth.return_value = True
        
        with patch('websockets.connect') as mock_connect:
            mock_connect.return_value.__aenter__.return_value = mock_ws
            
            # Start session as a background task
            session_task = asyncio.create_task(
                _session("ws://test", "http://test", "dev1", "secret", stop_event, mock_ui, mock_voice, active_state)
            )
            
            # Let it run for a bit
            await asyncio.sleep(0.5)
            
            # It should not have crashed (if it threw NameError, session_task would be done and have an exception)
            assert not session_task.done(), "Session task terminated unexpectedly (possibly NameError)"
            
            # Stop the session cleanly
            stop_event.set()
            
            # Let it clean up
            await asyncio.sleep(0.5)
            
            # Now it should be done without errors
            assert session_task.done()
            
            # Check for exception (will raise if there was one)
            exc = session_task.exception()
            assert exc is None, f"Session task raised exception: {exc}"
