import pytest
import asyncio
import struct
import math
from unittest.mock import patch, MagicMock
from agent.voice.groq_providers import GroqSTTProvider

def create_audio_chunk(rms_value, length=4000):
    """Creates a dummy 16-bit PCM audio chunk with approx the given RMS."""
    # RMS = sqrt(sum(x^2)/N)
    # x = RMS
    val = int(rms_value)
    fmt = f"<{length}h"
    return struct.pack(fmt, *([val] * length))

@pytest.mark.asyncio
async def test_vad_energy_thresholds():
    """Test that energy RMS logic triggers speech start and preserves pre-roll."""
    import os
    with patch.dict(os.environ, {'GROQ_API_KEY': 'fake_key'}):
        # Create the provider
        with patch.dict('os.environ', {'GROQ_API_KEY': 'fake_key'}):
            GroqSTTProvider._circuit_broken_until = 0
            stt = GroqSTTProvider()
            stt._client = MagicMock()
    
    # Mock KaldiRecognizer
    mock_rec = MagicMock()
    mock_rec.AcceptWaveform.return_value = False
    mock_rec.PartialResult.return_value = '{"partial": ""}' # No text yet
    
    # We will feed audio chunks and check behavior
    with patch('vosk.KaldiRecognizer', return_value=mock_rec):
        with patch('agent.voice.groq_providers.vosk_model', MagicMock()):
            with patch('sounddevice.RawInputStream') as mock_sd:
                
                # Setup
                stream = stt.listen_stream()
                
                async def run_stream():
                    results = []
                    async for item in stream:
                        results.append(item)
                    return results
                    
                # Start the generator in background
                t = asyncio.create_task(run_stream())
                
                # Give it a moment to initialize self._q and self._listening
                await asyncio.sleep(0.05)
                
                # 1. Background noise (RMS ~100)
                noise = create_audio_chunk(100)
                await stt._q.put(noise)
                await stt._q.put(noise)
                
                # 2. Quiet speaker (RMS ~350)
                quiet = create_audio_chunk(350)
                await stt._q.put(quiet)
                await stt._q.put(quiet)
                
                # 3. Speech starts (RMS > 400 for 2 frames)
                speech = create_audio_chunk(500)
                await stt._q.put(speech)
                await stt._q.put(speech)
                
                # After these, Vosk returns text
                mock_rec.PartialResult.return_value = '{"partial": "hello"}'
                await stt._q.put(speech)
                
                # Terminate the loop
                await asyncio.sleep(0.05) # wait for the queue to drain
                stt._listening = False
                await stt._q.put(b"")
                
                results = await t
                    
                # The first item yielded should be (False, 'hello')
                assert len(results) > 0
                assert results[0] == (False, "hello")

