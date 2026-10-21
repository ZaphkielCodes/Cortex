import asyncio
import queue
import json
import logging
import threading
import sys
import os
import time

log = logging.getLogger("cortex.voice.real")

try:
    import sounddevice as sd
    from vosk import Model, KaldiRecognizer
    import pyttsx3
    
    # Supress vosk logging
    from vosk import SetLogLevel
    SetLogLevel(-1)
    
    MODEL_PATH = os.path.join(os.path.dirname(__file__), "vosk-model")
    if os.path.exists(MODEL_PATH):
        model = Model(MODEL_PATH)
    else:
        log.warning("Vosk model not found. Real voice will fail.")
        model = None
except ImportError:
    log.warning("Voice libraries not installed.")
    sd = None
    model = None

from .interfaces import WakeWordProvider, STTProvider, TTSProvider

class LocalWakeWordProvider(WakeWordProvider):
    def __init__(self, wake_word: str = "yo cortex"):
        self.wake_word = wake_word.lower()
        self._running = False
        self._q = queue.Queue()
        self._thread = None
        self._on_wake = None
        self._handoff_audio = b""
        
    def _callback(self, indata, frames, time, status):
        if status:
            pass # ignore overflow warnings for wake word
        self._q.put(bytes(indata))
        
    def _listen_loop(self):
        if not model:
            return
            
        import psutil
        ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        log.info(f"[Profiling] Wake-word listening start | CPU: {psutil.cpu_percent()}% | RAM: {ram_mb:.1f} MB")
        t_start = time.monotonic()
        
        # Grammar restriction vastly improves recall, reduces false positives, and lowers CPU usage
        grammar = json.dumps([self.wake_word, "[unk]"])
        rec = KaldiRecognizer(model, 16000, grammar)
        from collections import deque
        recent_audio = deque(maxlen=12)  # Three seconds, in memory only.
        
        try:
            # Use a smaller blocksize (4000) for faster reaction (0.25s) instead of 8000
            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype='int16', channels=1, callback=self._callback):
                while self._running:
                    data = self._q.get()
                    if not self._running:
                        break
                        
                    recent_audio.append(data)
                    if rec.AcceptWaveform(data):
                        text = json.loads(rec.Result()).get("text", "")
                    else:
                        text = json.loads(rec.PartialResult()).get("partial", "")
                    if self.wake_word in text:
                        # Detect during speech, retaining any command onset already captured.
                        self._handoff_audio = b"".join(recent_audio)
                        self._running = False
                        log.info(f"[Latency] Wake-word detected: {time.monotonic() - t_start:.3f}s since listening began")
                        log.info(f"[WakeWord] Detected: '{text}'")
                        if self._on_wake:
                            self._on_wake()
                        break
        except Exception as e:
            log.error(f"[WakeWord] Error: {e}")
            
    def start(self, on_wake) -> None:
        if self._running: return
        self._on_wake = on_wake
        self._handoff_audio = b""
        with self._q.mutex:
            self._q.queue.clear()
        self._running = True
        log.info(f"[WakeWord] Started listening for '{self.wake_word}'...")
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        
    def take_audio(self):
        audio, self._handoff_audio = self._handoff_audio, b""
        return audio

    def wait_stopped(self):
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        return not self._thread or not self._thread.is_alive()

    def stop(self) -> None:
        self._running = False
        self._q.put(b"") # unblock

class VoskSTTProvider(STTProvider):
    def __init__(self):
        self._initial_audio = b""
        self._listening = False
        self._q = None
        self._loop = None
        
    def set_initial_audio(self, audio):
        self._initial_audio = bytes(audio)[-96000:]

    def _callback(self, indata, frames, time, status):
        if self._q and self._loop:
            self._loop.call_soon_threadsafe(self._q.put_nowait, bytes(indata))
            
    async def listen(self) -> str:
        final_text = ""
        async for is_final, text in self.listen_stream():
            if is_final:
                final_text = text
        return final_text

    async def listen_stream(self):
        if not model: 
            yield True, ""
            return
            
        self._listening = True
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()
        initial, self._initial_audio = self._initial_audio, b""
        for offset in range(0, len(initial), 8000):
            self._q.put_nowait(initial[offset:offset+8000])
        
        import psutil
        ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        log.info(f"[Profiling] STT listening start | CPU: {psutil.cpu_percent()}% | RAM: {ram_mb:.1f} MB")
        t0 = time.monotonic()
        log.info(f"[Latency] STT start: 0.000s")
        
        rec = KaldiRecognizer(model, 16000)
        silence_chunks = 0
        has_spoken = False
        result_text = ""
        
        log.info("[STT] Microphone listening...")
        try:
            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype='int16', channels=1, callback=self._callback):
                while self._listening:
                    data = await self._q.get()
                    if not self._listening:
                        break
                        
                    # Run CPU-intensive Vosk processing in a thread to prevent asyncio starvation
                    is_accepted = await self._loop.run_in_executor(None, rec.AcceptWaveform, data)
                    
                    if is_accepted:
                        res = json.loads(rec.Result())
                        text = res.get("text", "")
                        if text:
                            result_text += " " + text
                            has_spoken = True
                            silence_chunks = 0
                            yield True, result_text.strip()
                            break
                    else:
                        partial = json.loads(rec.PartialResult())
                        pt = partial.get("partial", "")
                        if pt:
                            has_spoken = True
                            silence_chunks = 0
                            yield False, (result_text + " " + pt).strip()
                        elif has_spoken:
                            silence_chunks += 1
                            
                    # Timeout/end-of-utterance: ~2 seconds of silence
                    if has_spoken and silence_chunks > 8:
                        log.info(f"[Latency] End-of-utterance detection: {time.monotonic() - t0:.3f}s")
                        res = json.loads(rec.FinalResult())
                        if res.get("text"):
                            result_text += " " + res.get("text")
                        yield True, result_text.strip()
                        break
                        
                    # Absolute timeout if no speech
                    if not has_spoken and silence_chunks > 40:
                        yield True, ""
                        break
        except Exception as e:
            log.error(f"[STT] Error: {e}")
            yield True, result_text.strip()
            
        log.info(f"[Latency] STT result parsed: {time.monotonic() - t0:.3f}s")
        self._listening = False
        
    def stop_listening(self) -> None:
        self._listening = False
        if self._q and self._loop:
            self._loop.call_soon_threadsafe(self._q.put_nowait, b"")

class PyTTSx3Provider(TTSProvider):
    def __init__(self, on_audio_level=None):
        self._speaking = False
        self._on_audio_level = on_audio_level
        
    async def speak(self, text: str) -> None:
        self._speaking = True
        loop = asyncio.get_running_loop()
        log.info(f"[TTS] Speaking: '{text}'")
        
        def run_tts():
            import psutil
            ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            log.info(f"[Profiling] Speaking/TTS start | CPU: {psutil.cpu_percent()}% | RAM: {ram_mb:.1f} MB")
            t_tts = time.monotonic()
            log.info(f"[Latency] TTS start: 0.000s")
            try:
                engine = pyttsx3.init()
                if self._on_audio_level:
                    import tempfile
                    import os
                    import wave
                    import math
                    import struct
                    import sounddevice as sd
                    
                    temp_file = tempfile.mktemp(suffix='.wav')
                    engine.save_to_file(text, temp_file)
                    engine.runAndWait()
                    
                    if os.path.exists(temp_file):
                        try:
                            wf = wave.open(temp_file, 'rb')
                            fs = wf.getframerate()
                            channels = wf.getnchannels()
                            
                            def callback(outdata, frames, time_info, status):
                                data = wf.readframes(frames)
                                if len(data) == 0:
                                    if self._on_audio_level:
                                        self._on_audio_level(0.0)
                                    raise sd.CallbackStop()
                                
                                try:
                                    if wf.getsampwidth() == 2:
                                        fmt = f"<{len(data)//2}h"
                                        shorts = struct.unpack(fmt, data)
                                        rms = math.sqrt(sum(s*s for s in shorts) / len(shorts)) if shorts else 0
                                        # Normalize RMS to 0..1 range (max ~32768 for 16-bit)
                                        amp = min(rms / 10000.0, 1.0)
                                        if self._on_audio_level:
                                            self._on_audio_level(amp)
                                except Exception as e:
                                    pass
                                    
                                chunk = data.ljust(frames * channels * wf.getsampwidth(), b'\x00')
                                outdata[:] = chunk
                                
                            with sd.RawOutputStream(samplerate=fs, channels=channels, dtype='int16', callback=callback):
                                sd.sleep(int((wf.getnframes() / fs) * 1000) + 200)
                        finally:
                            try:
                                os.remove(temp_file)
                            except:
                                pass
                else:
                    engine.say(text)
                    engine.runAndWait()
            except Exception as e:
                log.error(f"[TTS] Error: {e}")
            
            log.info(f"[Latency] TTS completion: {time.monotonic() - t_tts:.3f}s")
                
        await loop.run_in_executor(None, run_tts)
        self._speaking = False
        
    def stop_speaking(self) -> None:
        self._speaking = False
