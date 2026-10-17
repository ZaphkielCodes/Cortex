import asyncio
import logging
import os
import time

from .interfaces import STTProvider

log = logging.getLogger("cortex.voice.deepgram")

class DeepgramSTTProvider(STTProvider):
    def __init__(self, fallback_provider: STTProvider):
        self.api_key = os.environ.get("DEEPGRAM_API_KEY")
        if self.api_key:
            log.info("Deepgram API key found. Deepgram STT selected.")
        self.fallback = fallback_provider
        self._listening = False
        self._q = None
        self._loop = None

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
        if not self.api_key:
            log.warning("No DEEPGRAM_API_KEY found, falling back.")
            async for is_final, text in self.fallback.listen_stream():
                yield is_final, text
            return

        import sounddevice as sd
        from deepgram import (
            DeepgramClient,
            DeepgramClientOptions,
            LiveTranscriptionEvents,
            LiveOptions,
        )

        self._listening = True
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()

        import psutil
        ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        log.info(f"[Profiling] Deepgram STT start | CPU: {psutil.cpu_percent()}% | RAM: {ram_mb:.1f} MB")
        t0 = time.monotonic()
        log.info(f"[Latency] STT start: 0.000s")

        result_text = ""
        speech_started = False
        silence_chunks = 0
        
        # We need a way to communicate from Deepgram callbacks back to this generator
        dg_events = asyncio.Queue()

        def on_message(self_dg, result, **kwargs):
            sentence = result.channel.alternatives[0].transcript
            if len(sentence) == 0:
                return
            is_final = result.is_final
            if self._loop:
                self._loop.call_soon_threadsafe(dg_events.put_nowait, ("msg", is_final, sentence))

        def on_error(self_dg, error, **kwargs):
            log.error(f"[STT] Deepgram Error: {error}")
            if self._loop:
                self._loop.call_soon_threadsafe(dg_events.put_nowait, ("err", True, str(error)))

        deepgram = DeepgramClient(self.api_key)
        dg_connection = deepgram.listen.websocket.v("1")

        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        dg_connection.on(LiveTranscriptionEvents.Error, on_error)

        options = LiveOptions(
            model="nova-2",
            punctuate=True,
            language="en-US",
            encoding="linear16",
            channels=1,
            sample_rate=16000,
            endpointing=True,
            interim_results=True,
            vad_events=True,
        )

        try:
            if not dg_connection.start(options):
                log.error("Failed to start Deepgram connection")
                raise Exception("Deepgram connection failed")
                
            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype='int16', channels=1, callback=self._callback):
                log.info("[STT] Deepgram Microphone listening...")
                
                while self._listening:
                    # Wait for either audio from mic or event from Deepgram
                    try:
                        audio_task = asyncio.create_task(self._q.get())
                        event_task = asyncio.create_task(dg_events.get())
                        done, pending = await asyncio.wait(
                            [audio_task, event_task],
                            return_when=asyncio.FIRST_COMPLETED,
                            timeout=0.1
                        )
                        
                        if not self._listening:
                            for task in pending: task.cancel()
                            break
                            
                        if audio_task in done:
                            data = audio_task.result()
                            dg_connection.send(data)
                            
                            if speech_started:
                                silence_chunks += 1
                                # Failsafe timeout
                                if silence_chunks > 40: # ~10s absolute max
                                    yield True, result_text
                                    break
                                    
                        if event_task in done:
                            ev_type, is_final, text = event_task.result()
                            if ev_type == "msg":
                                speech_started = True
                                if is_final:
                                    result_text += " " + text
                                    result_text = result_text.strip()
                                    log.info(f"[Latency] End-of-utterance (Deepgram): {time.monotonic() - t0:.3f}s")
                                    yield True, result_text
                                    break
                                else:
                                    silence_chunks = 0
                                    yield False, (result_text + " " + text).strip()
                            elif ev_type == "err":
                                log.error("Deepgram reported error, falling back")
                                raise Exception(text)
                                
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                                
                    except asyncio.TimeoutError:
                        pass
        except Exception as e:
            log.error(f"[STT] Deepgram STT error: {e}. Falling back.")
            async for is_final, text in self.fallback.listen_stream():
                yield is_final, text
        finally:
            self._listening = False
            try:
                dg_connection.finish()
            except:
                pass
            log.info(f"[STT] Heard: '{result_text}'")

    def stop_listening(self) -> None:
        self._listening = False
        if self._q and self._loop:
            self._loop.call_soon_threadsafe(self._q.put_nowait, b"")
