import asyncio
import logging
import os
import time
import json
import io
import wave
import aiohttp

from .interfaces import STTProvider
from .real_providers import VoskSTTProvider, model as vosk_model

log = logging.getLogger("cortex.voice.groq")

def transcription_uncertain(result):
    """Use provider quality measurements rather than guessing the intended wording."""
    import math
    for segment in result.get("segments") or []:
        if not isinstance(segment, dict):
            return True
        for key, threshold, lower in (("avg_logprob", -1.0, True),
                                       ("no_speech_prob", .6, False),
                                       ("compression_ratio", 2.4, False)):
            value = segment.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return True
            if (value < threshold) if lower else (value > threshold):
                return True
    return False


class GroqSTTProvider(STTProvider):
    _circuit_broken_until = 0

    def __init__(self, fallback_provider: STTProvider = None):
        self.api_key = os.environ.get("GROQ_API_KEY")
        if not self.api_key:
            log.info("No GROQ_API_KEY found. Groq STT will skip/fail.")
            
        # Keep Vosk as the fallback
        self.fallback = fallback_provider or VoskSTTProvider()
        self._listening = False
        self._q = None
        self._loop = None
        self._initial_audio = b""
        self.last_transcription_uncertain = False

    def set_initial_audio(self, audio):
        self._initial_audio = bytes(audio)[-96000:]

    def _callback(self, indata, frames, time_info, status):
        if self._q and self._loop:
            self._loop.call_soon_threadsafe(self._q.put_nowait, bytes(indata))

    async def listen(self) -> str:
        final_text = ""
        async for is_final, text in self.listen_stream():
            if is_final:
                final_text = text
        return final_text

    async def listen_stream(self):
        self.last_transcription_uncertain = False
        initial_audio, self._initial_audio = self._initial_audio, b""
        # Check circuit breaker
        if time.time() < type(self)._circuit_broken_until or not self.api_key:
            if not self.api_key:
                log.warning("Groq STT skipped: No API key.")
            else:
                log.warning("Groq STT skipped: Circuit breaker active.")
            
            # Use fallback directly for the entire stream, preserving the wake handoff.
            seed = getattr(self.fallback, "set_initial_audio", None)
            if callable(seed):
                seed(initial_audio)
            async for is_final, text in self.fallback.listen_stream():
                yield is_final, text
            return

        if not vosk_model:
            log.error("Vosk model not loaded, cannot perform local VAD.")
            return

        import sounddevice as sd
        from vosk import KaldiRecognizer
        import psutil

        from collections import deque
        self._listening = True
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()
        for offset in range(0, len(initial_audio), 8000):
            self._q.put_nowait(initial_audio[offset:offset+8000])

        ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        log.info(f"[Profiling] Groq (Vosk VAD) STT start | CPU: {psutil.cpu_percent()}% | RAM: {ram_mb:.1f} MB")
        t0 = time.monotonic()
        log.info(f"[Latency] STT start: 0.000s")

        rec = KaldiRecognizer(vosk_model, 16000)
        silence_chunks = 0
        has_spoken = False
        result_text = ""
        
        audio_buffer = io.BytesIO()
        pre_roll = deque(maxlen=4)
        has_audio = False
        energy_frames = 0
        quiet_frames = 0
        captured_seconds = 0.0
        import struct
        import math

        log.info("[STT] Microphone listening (VAD mode)...")
        log.info(f"[Latency] Command microphone opened: {time.monotonic() - t0:.3f}s")
        try:
            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype='int16', channels=1, callback=self._callback):
                while self._listening:
                    data = await asyncio.wait_for(self._q.get(), timeout=10.0)
                    if not self._listening:
                        break
                        
                    if has_spoken:
                        audio_buffer.write(data)
                    else:
                        pre_roll.append(data)
                        
                    fmt = f"<{len(data)//2}h"
                    shorts = struct.unpack(fmt, data)
                    rms = math.sqrt(sum(s*s for s in shorts) / len(shorts)) if shorts else 0

                    captured_seconds += len(data) / 32000.0
                    quiet_frames = quiet_frames + 1 if has_spoken and rms < 300 else 0
                    if not has_audio and rms > 150:
                        has_audio = True
                        log.info(f"[Latency] Non-silent audio first observed: {time.monotonic() - t0:.3f}s (rms={rms:.1f})")

                    if not has_spoken:
                        if rms > 400:
                            energy_frames += 1
                            if energy_frames >= 2:
                                has_spoken = True
                                log.info(f"[Latency] Energy speech threshold crossed: {time.monotonic() - t0:.3f}s (rms={rms:.1f})")
                                for pr_chunk in pre_roll:
                                    audio_buffer.write(pr_chunk)
                                pre_roll.clear()
                        else:
                            energy_frames = 0
                    else:
                        if rms > 300:
                            silence_chunks = 0

                    if rec.AcceptWaveform(data):
                        res = json.loads(rec.Result())
                        text = res.get("text", "")
                        if text:
                            result_text += " " + text
                            if not has_spoken:
                                has_spoken = True
                                log.info(f"[Latency] VAD speech threshold crossed (Fallback): {time.monotonic() - t0:.3f}s")
                                for pr_chunk in pre_roll:
                                    audio_buffer.write(pr_chunk)
                                pre_roll.clear()
                            silence_chunks = 0
                            # Vosk endpoints are advisory; retain speech following a short pause.
                            log.info(f"[Latency] Vosk speech segment ended: {time.monotonic() - t0:.3f}s")
                    else:
                        partial = json.loads(rec.PartialResult())
                        pt = partial.get("partial", "")
                        if pt:
                            if not has_spoken:
                                has_spoken = True
                                log.info(f"[Latency] VAD speech threshold crossed (Fallback): {time.monotonic() - t0:.3f}s")
                                for pr_chunk in pre_roll:
                                    audio_buffer.write(pr_chunk)
                                pre_roll.clear()
                            silence_chunks = 0
                            yield False, (result_text + " " + pt).strip()
                        elif has_spoken:
                            silence_chunks += 1
                            
                    # Timeout/end-of-utterance: ~1.25 seconds of silence
                    if has_spoken and (quiet_frames > 5 or captured_seconds >= 30.0):
                        log.info(f"[Latency] Last speech detected: {time.monotonic() - t0:.3f}s (estimated)")
                        res = json.loads(rec.FinalResult())
                        if res.get("text"):
                            result_text += " " + res.get("text")
                        break
                        
                    # Absolute timeout if no speech
                    if not has_spoken and captured_seconds >= 10.0:
                        yield True, ""
                        return

        except Exception as e:
            log.error(f"[STT] Error during capture: {e}")
            yield True, result_text.strip()
            self._listening = False
            return

        if not self._listening:
            # An interrupted capture must not upload abandoned audio or issue a command.
            yield True, ""
            return
        t_endpoint = time.monotonic()
        log.info(f"[Latency] Utterance committed: {t_endpoint - t0:.3f}s")
        self._listening = False

        vosk_final_text = result_text.strip()
        
        # Audio captured, now send to Groq
        audio_bytes = audio_buffer.getvalue()
        if not audio_bytes:
            yield True, vosk_final_text
            return
            
        # Convert raw PCM to WAV for Groq
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(audio_bytes)
            
        wav_data = wav_buffer.getvalue()

        t_groq_req = time.monotonic()
        log.info(f"[Latency] Groq request: {t_groq_req - t0:.3f}s")
        
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15.0)) as session:
                form = aiohttp.FormData()
                form.add_field('file', wav_data, filename='audio.wav', content_type='audio/wav')
                form.add_field('model', 'whisper-large-v3')
                form.add_field('response_format', 'verbose_json')
                stt_lang = os.environ.get("CORTEX_STT_LANGUAGE", "en")
                if stt_lang and stt_lang.lower() not in ("auto", "none"):
                    form.add_field('language', stt_lang)
                
                headers = {"Authorization": f"Bearer {self.api_key}"}
                
                async with session.post("https://api.groq.com/openai/v1/audio/transcriptions", data=form, headers=headers) as resp:
                    t_groq_res = time.monotonic()
                    log.info(f"[Latency] Groq response: {t_groq_res - t0:.3f}s (duration: {t_groq_res - t_groq_req:.3f}s)")
                    
                    if resp.status == 200:
                        result = await resp.json()
                        groq_text = result.get("text", "").strip()
                        self.last_transcription_uncertain = transcription_uncertain(result)
                        log.info(f"[Latency] Stable transcript produced: {time.monotonic() - t0:.3f}s")
                        
                        import re
                        wake_regex = r'^(yo[\s,]+cortex|your[\s,]+cortex)[\s,]*(.*)'
                        m = re.match(wake_regex, groq_text, re.IGNORECASE)
                        if m:
                            groq_text = m.group(2)
                        
                        yield True, groq_text
                        return
                    else:
                        log.warning("Groq transcription request failed with HTTP %s", resp.status)
                        # Circuit breaking on auth/quota errors
                        if resp.status in (401, 403, 429):
                            log.warning("Groq STT circuit breaker activated for 60 seconds.")
                            type(self)._circuit_broken_until = time.time() + 60
                            
        except Exception as e:
            log.error(f"Groq STT request failed: {e}")

        # Fallback to the Vosk transcript if Groq fails
        log.info("Falling back to local Vosk transcript.")
        log.info(f"[Latency] Stable transcript produced (fallback): {time.monotonic() - t0:.3f}s")
        yield True, vosk_final_text

    def stop_listening(self) -> None:
        self._listening = False
        if self._q and self._loop:
            self._loop.call_soon_threadsafe(self._q.put_nowait, b"")
        self.fallback.stop_listening()
