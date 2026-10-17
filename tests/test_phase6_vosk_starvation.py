import pytest
import asyncio
import time
from unittest.mock import patch, MagicMock
from agent.voice.real_providers import VoskSTTProvider
import json

@pytest.mark.asyncio
async def test_stt_does_not_starve_event_loop():
    """
    Tests that a slow STT recognition block (KaldiRecognizer.AcceptWaveform)
    does not starve the asyncio event loop. If run_in_executor is used,
    parallel tasks should continue to execute smoothly.
    """
    
    # Track when the heartbeat task actually ran
    heartbeat_ticks = []
    
    async def heartbeat():
        for _ in range(5):
            await asyncio.sleep(0.1)
            heartbeat_ticks.append(time.monotonic())
            
    # Mock KaldiRecognizer
    mock_rec = MagicMock()
    
    # Make AcceptWaveform deliberately slow and blocking
    def slow_accept(*args, **kwargs):
        time.sleep(0.3) # 300ms blocking sleep
        return True
        
    mock_rec.AcceptWaveform = slow_accept
    mock_rec.Result.return_value = json.dumps({"text": "test"})
    
    with patch('agent.voice.real_providers.KaldiRecognizer', return_value=mock_rec):
        with patch('agent.voice.real_providers.model', MagicMock()): # simulate model existing
            with patch('sounddevice.RawInputStream') as mock_sd:
                # We need to simulate the callback feeding data
                stt = VoskSTTProvider()
                
                # Start STT listening (which sets up self._q)
                async def run_stt():
                    try:
                        async for is_final, text in stt.listen_stream():
                            if is_final:
                                break
                    except Exception as e:
                        pass
                
                stt_task = asyncio.create_task(run_stt())
                
                # Wait for queue to be initialized
                await asyncio.sleep(0.05)
                
                # Feed some fake audio chunks
                for _ in range(3):
                    # Call the callback directly as sounddevice would from its thread
                    stt._callback(b'\x00' * 4000, 4000, None, None)
                    await asyncio.sleep(0.01)
                
                hb_task = asyncio.create_task(heartbeat())
                
                # Let them run
                await asyncio.gather(hb_task)
                stt.stop_listening()
                await stt_task
                
                # If starvation occurred, the 5 heartbeats (0.1s interval) would take 
                # much longer than 0.5s total because slow_accept takes 0.3s * 3 = 0.9s
                
                assert len(heartbeat_ticks) == 5
                
                intervals = []
                for i in range(1, len(heartbeat_ticks)):
                    intervals.append(heartbeat_ticks[i] - heartbeat_ticks[i-1])
                    
                # All intervals should be roughly 0.1s (allow some margin, e.g. < 0.2s)
                # If they were starved by slow_accept, they would be >= 0.3s
                for interval in intervals:
                    assert interval < 0.2, f"Event loop starvation detected! Interval was {interval:.3f}s"
