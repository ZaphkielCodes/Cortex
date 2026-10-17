import asyncio
import logging
import os
import io
import math
import struct
import time

from .interfaces import TTSProvider

log = logging.getLogger("cortex.voice.polly")

class AmazonPollyTTSProvider(TTSProvider):
    def __init__(self, fallback_provider: TTSProvider = None, on_audio_level=None):
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        self.fallback = fallback_provider
        self._on_audio_level = on_audio_level
        self._speaking = False
        
        self.region = os.environ.get('CORTEX_POLLY_REGION', 'us-east-1')
        self.voice = os.environ.get('CORTEX_POLLY_VOICE', 'Matthew')
        self.engine = os.environ.get('CORTEX_POLLY_ENGINE', 'neural')

        # Automated validation for neural engine
        SUPPORTED_NEURAL_REGIONS = {
            "us-east-1", "us-west-2", "ap-southeast-2", "ca-central-1", 
            "eu-west-1", "eu-central-1", "eu-west-2", "ap-northeast-1", 
            "ap-south-1", "ap-southeast-1"
        }
        
        if self.engine == "neural" and self.region not in SUPPORTED_NEURAL_REGIONS:
            log.warning(f"[Polly Config] ValidationException: The selected engine '{self.engine}' is not supported in region '{self.region}'. Polly will be disabled.")
            self.client = None
            return

        try:
            self.client = boto3.client('polly', region_name=self.region)
            log.info(f"[Polly Config] Resolved region: {self.region}")
            log.info(f"[Polly Config] Resolved voice: {self.voice}")
            log.info(f"[Polly Config] Resolved engine: {self.engine}")
        except Exception as e:
            log.error(f"Failed to initialize Amazon Polly client: {e}")
            self.client = None

    async def speak(self, text: str) -> None:
        self._speaking = True
        loop = asyncio.get_running_loop()
        log.info(f"[TTS] Speaking (Polly): '{text}'")

        def run_tts():
            if not self.client:
                raise RuntimeError("Amazon Polly client not initialized.")
                
            from botocore.exceptions import BotoCoreError, ClientError
            
            t0 = time.monotonic()
            log.info(f"[Latency] TTS start (Polly): 0.000s")
            try:
                response = self.client.synthesize_speech(
                    Text=text,
                    OutputFormat='pcm',
                    SampleRate='16000',
                    VoiceId=self.voice,
                    Engine=self.engine
                )
            except (BotoCoreError, ClientError) as e:
                raise RuntimeError(f"Amazon Polly API error: {e}")

            log.info(f"[Latency] TTS audio received: {time.monotonic() - t0:.3f}s")
            
            if "AudioStream" in response:
                import sounddevice as sd
                # Read all audio data
                audio_data = response['AudioStream'].read()
                response['AudioStream'].close()
                
                # We requested PCM 16000Hz. Polly returns 16-bit signed PCM (mono).
                # Playback with sounddevice
                
                # To support pausing/stopping, we can use an InputStream/OutputStream or play and wait
                if self._on_audio_level:
                    # We need to chunk it and calculate audio levels
                    def callback(outdata, frames, time_info, status):
                        nonlocal audio_data
                        chunk_size = frames * 2 # 16-bit mono = 2 bytes per frame
                        if len(audio_data) == 0:
                            if self._on_audio_level:
                                self._on_audio_level(0.0)
                            raise sd.CallbackStop()
                            
                        chunk = audio_data[:chunk_size]
                        audio_data = audio_data[chunk_size:]
                        
                        try:
                            fmt = f"<{len(chunk)//2}h"
                            shorts = struct.unpack(fmt, chunk)
                            rms = math.sqrt(sum(s*s for s in shorts) / len(shorts)) if shorts else 0
                            amp = min(rms / 10000.0, 1.0)
                            if self._on_audio_level:
                                self._on_audio_level(amp)
                        except Exception:
                            pass
                            
                        # pad if needed
                        if len(chunk) < chunk_size:
                            chunk = chunk.ljust(chunk_size, b'\x00')
                        outdata[:] = chunk
                        
                    with sd.RawOutputStream(samplerate=16000, channels=1, dtype='int16', callback=callback) as stream:
                        while stream.active and self._speaking:
                            sd.sleep(50)
                else:
                    import numpy as np
                    audio_array = np.frombuffer(audio_data, dtype=np.int16)
                    sd.play(audio_array, samplerate=16000, blocking=True)
                    sd.wait()
            else:
                raise RuntimeError("No AudioStream found in Amazon Polly response.")

        try:
            await loop.run_in_executor(None, run_tts)
        except Exception as e:
            log.error(f"[TTS] Amazon Polly failed: {e}")
            if self.fallback:
                log.info("Falling back to backup TTS provider...")
                await self.fallback.speak(text)
            else:
                log.error("No fallback TTS provider available.")
        finally:
            self._speaking = False
            if self._on_audio_level:
                self._on_audio_level(0.0)

    def stop_speaking(self) -> None:
        self._speaking = False
        if self.fallback:
            self.fallback.stop_speaking()
