"""
agent/voice/controller.py
-------------------------
Manages the Voice FSM and coordinates STT, TTS, and Wake Word detection.
Enforces the audio ownership invariant:
  WAKE_LISTENER xor ACTIVE_STT xor TTS (or NONE).
Never allows simultaneous conflicting audio operations.
"""

import asyncio
import logging
import re
import uuid
from typing import Callable, Awaitable, Optional, List
from .interfaces import WakeWordProvider, STTProvider, TTSProvider

log = logging.getLogger("cortex.voice.controller")

def is_session_exit_command(raw_text: str) -> bool:
    """Detect if a voice transcript is a natural session-exit request.

    Normalizes punctuation, casing, and conversational prefixes ('okay', 'ok', 'alright', 'hey', 'yo').
    Keeps matching bounded so commands like 'stop the music' are not treated as session exit.
    """
    if not raw_text:
        return False

    # 1. Lowercase and normalize whitespace/punctuation
    text = raw_text.lower().strip()
    text = re.sub(r'^[^\w]+|[^\w]+$', '', text)
    text = re.sub(r'[,;:\-—!?.]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    if not text:
        return False

    # 2. Strip harmless conversational prefixes
    prefix_pattern = r'^(?:okay|ok|alright|all\s+right|hey|yo)\s+'
    while True:
        stripped = re.sub(prefix_pattern, '', text)
        if stripped == text:
            break
        text = stripped

    # Normalize "that is" -> "that's"
    text = re.sub(r'\bthat\s+is\b', "that's", text)

    # 3. Exact exit phrases (after prefix stripping)
    exit_phrases = {
        "stop",
        "cortex stop",
        "stop cortex",
        "go to sleep",
        "sleep",
        "stop listening",
        "exit session",
        "stop session",
        "end session",
        "exit",
        "quit",
        "cortex exit",
        "exit cortex",
        "that's all",
        "that's all cortex",
        "cortex that's all",
        "that's it",
        "that's it cortex",
        "cortex that's it",
        "bye",
        "goodbye",
        "bye cortex",
        "goodbye cortex",
        "cortex bye",
        "cortex goodbye",
    }

    if text in exit_phrases:
        return True

    # Also handle combinations where "cortex" precedes or follows an exit phrase
    cortex_stripped_start = re.sub(r'^cortex\s+', '', text).strip()
    cortex_stripped_end = re.sub(r'\s+cortex$', '', text).strip()

    if cortex_stripped_start in exit_phrases or cortex_stripped_end in exit_phrases:
        return True

    return False

def is_sleep_command(raw_text: str) -> bool:
    """Deterministically detect if text maps to semantic intent runtime.sleep (Phase 9F)."""
    if not raw_text:
        return False
    text = raw_text.lower().strip()
    text = re.sub(r'^[^\w]+|[^\w]+$', '', text)
    text = re.sub(r'[,;:\-—!?.]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    prefix_pattern = r'^(?:okay|ok|alright|all\s+right|hey|yo)\s+'
    while True:
        stripped = re.sub(prefix_pattern, '', text)
        if stripped == text:
            break
        text = stripped
    sleep_phrases = {
        "sleep",
        "go to sleep",
        "cortex sleep",
        "sleep cortex",
        "cortex go to sleep",
    }
    if text in sleep_phrases:
        return True
    cortex_stripped_start = re.sub(r'^cortex\s+', '', text).strip()
    cortex_stripped_end = re.sub(r'\s+cortex$', '', text).strip()
    if cortex_stripped_start in sleep_phrases or cortex_stripped_end in sleep_phrases:
        return True
    return False

class VoiceState:
    IDLE = "idle"
    WAKE_DETECTED = "wake_detected"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ACTIVE_SESSION = "active_session"
    SESSION_TIMEOUT = "session_timeout"
    MUTED = "muted"

class AudioOwner:
    NONE = "NONE"
    WAKE_LISTENER = "WAKE_LISTENER"
    ACTIVE_STT = "ACTIVE_STT"
    TTS = "TTS"

class VoiceController:
    def __init__(
        self,
        wakeword: WakeWordProvider,
        stt: STTProvider,
        tts: TTSProvider,
        on_text_recognized: Callable[[str], Awaitable[str]],
        on_partial_text: Optional[Callable[[str], Awaitable[None]]] = None,
        on_state_change: Optional[Callable[[str], None]] = None,
        on_sleep_command: Optional[Callable[[], Awaitable[None]]] = None,
        continuous_session: bool = False,
        session_timeout_secs: float = 10.0
    ):
        self.wakeword = wakeword
        self.stt = stt
        self.tts = tts
        self.on_text_recognized = on_text_recognized
        self.on_partial_text = on_partial_text
        self.on_state_change = on_state_change
        self.on_sleep_command = on_sleep_command
        self.continuous_session = continuous_session
        self.session_timeout_secs = session_timeout_secs
        
        self.state = VoiceState.IDLE
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()
            
        self._audio_owner = AudioOwner.NONE
        self._audio_lock = asyncio.Lock()
        self._unsolicited_queue = asyncio.Queue()
        
        self._current_session_id = None
        self._session_task: Optional[asyncio.Task] = None
        self._in_session = False
        self._stopped = True
        self._pending_wake_audio = b""
        self._already_running = False
        self._last_wake_time: Optional[float] = None
        self.last_latency_metrics: dict = {}

    def _ensure_async_primitives(self):
        """Ensure lock and queue are bound to the currently running event loop."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = self._loop

        if self._loop != current_loop:
            self._loop = current_loop
            self._audio_lock = asyncio.Lock()
            self._unsolicited_queue = asyncio.Queue()
        elif self._audio_lock is None:
            self._audio_lock = asyncio.Lock()
            self._unsolicited_queue = asyncio.Queue()

    def _set_state(self, new_state: str, session_id: Optional[str] = None):
        old_state = self.state
        self.state = new_state
        sid = session_id or self._current_session_id or "none"
        owner = self._audio_owner
        log.info(f"[StateTransition][session={sid}][owner={owner}] {old_state.upper()} -> {new_state.upper()}")
        if self.on_state_change:
            self.on_state_change(self.state)

    def _acquire_audio_owner(self, requested_owner: str, session_id: str):
        if self._audio_owner != AudioOwner.NONE and self._audio_owner != requested_owner:
            err_msg = f"[AudioConflict][session={session_id}] Cannot acquire {requested_owner} while {self._audio_owner} is active!"
            log.error(err_msg)
            raise RuntimeError(err_msg)
        self._audio_owner = requested_owner
        log.info(f"[AudioOwner][session={session_id}] Acquired {requested_owner}")

    def _release_audio_owner(self, owner: str, session_id: str):
        if self._audio_owner == owner:
            self._audio_owner = AudioOwner.NONE
            log.info(f"[AudioOwner][session={session_id}] Released {owner}")

    def start(self):
        """Start the voice controller (begins listening for wake word)."""
        if getattr(self, '_already_running', False):
            log.info("VoiceController is already running. Ignoring start().")
            return
            
        if self.state == VoiceState.MUTED:
            log.info("Cannot start wake word: Voice is MUTED.")
            return
            
        self._ensure_async_primitives()
        self._stopped = False
        self._already_running = True
        self._audio_owner = AudioOwner.WAKE_LISTENER
        self._set_state(VoiceState.IDLE)
        self.wakeword.start(self._on_wake_word_detected)
        log.info("VoiceController started (WAKE_LISTENER active).")

    def stop(self):
        """Stop all voice operations."""
        self._stopped = True
        self._already_running = False
        self._in_session = False
        if self._session_task and not self._session_task.done():
            self._session_task.cancel()
            self._session_task = None
        self.wakeword.stop()
        self.stt.stop_listening()
        self.tts.stop_speaking()
        self._audio_owner = AudioOwner.NONE
        self._set_state(VoiceState.IDLE)
        log.info("VoiceController stopped.")

    def mute(self):
        self.stop()
        self._set_state(VoiceState.MUTED)
        log.info("VoiceController MUTED.")

    def unmute(self):
        self._set_state(VoiceState.IDLE)
        self.start()
        log.info("VoiceController UNMUTED.")

    def _on_wake_word_detected(self):
        """Callback from WakeWord provider (thread-safe)."""
        if self.state != VoiceState.IDLE or self._audio_owner != AudioOwner.WAKE_LISTENER:
            log.debug("Wake word ignored: controller is not IDLE or owner is not WAKE_LISTENER.")
            return
            
        import time
        self._last_wake_time = time.monotonic()
        log.info(f"[Latency] Wake detected: {self._last_wake_time:.3f}s")
        self.wakeword.stop()
        take_audio = getattr(self.wakeword, "take_audio", None)
        self._pending_wake_audio = take_audio() if callable(take_audio) else b""
        self._audio_owner = AudioOwner.NONE
        self._loop.call_soon_threadsafe(self._start_session, "wake_word")

    def push_to_talk(self):
        """Trigger listening immediately, bypassing wake word."""
        if self.state in (VoiceState.THINKING, VoiceState.SPEAKING):
            log.info("Push-to-talk ignored: thinking or speaking in progress.")
            return
            
        import time
        self._last_wake_time = time.monotonic()
        log.info(f"[Latency] Push-to-talk triggered: {self._last_wake_time:.3f}s")
        self.wakeword.stop()
        self._audio_owner = AudioOwner.NONE
        self._start_session(source="push_to_talk")

    def _start_session(self, source: str = "wake_word"):
        self._ensure_async_primitives()
        self._stopped = False
        self.wakeword.stop()
        self._audio_owner = AudioOwner.NONE
        if self._session_task and not self._session_task.done():
            log.info(f"Cancelling previous active session task before starting new session ({source}).")
            self._session_task.cancel()
        self.stt.stop_listening()
        if source != "wake_word":
            self._pending_wake_audio = b""
        session_id = uuid.uuid4().hex[:8]
        self._current_session_id = session_id
        
        while not self._unsolicited_queue.empty():
            try:
                self._unsolicited_queue.get_nowait()
            except Exception:
                break
                
        if source == "wake_word":
            self._set_state(VoiceState.WAKE_DETECTED, session_id)
        else:
            self._set_state(VoiceState.LISTENING, session_id)
            
        self._session_task = self._loop.create_task(self._session_loop(session_id))

    async def speak_result(self, text: str):
        """Play an unsolicited TTS result without breaking the state machine."""
        if self.state == VoiceState.MUTED:
            return
            
        sid = self._current_session_id or "unsolicited"
        log.info(f"[UnsolicitedTTS][session={sid}] Received text to speak: '{text[:80]}'")
        
        # If in an active session, queue it and interrupt current STT
        if self._in_session and self._session_task and not self._session_task.done():
            log.info(f"[UnsolicitedTTS][session={sid}] In active session; queueing and interrupting STT listener.")
            await self._unsolicited_queue.put(text)
            self.stt.stop_listening()
            return
            
        # If IDLE (not in session), speak directly under audio lock without starting a session loop
        log.info(f"[UnsolicitedTTS][session={sid}] Idle state; playing directly without spawning interaction loop.")
        was_idle = (self.state == VoiceState.IDLE)
        if was_idle:
            self.wakeword.stop()
            self._audio_owner = AudioOwner.NONE
            
        await self._play_tts(text, sid)
        
        if not getattr(self, '_stopped', False) and self.state != VoiceState.MUTED:
            if was_idle:
                self._return_to_idle(sid)

    async def _play_tts(self, text: str, session_id: Optional[str] = None, latency_metrics: Optional[dict] = None):
        sid = session_id or self._current_session_id or "none"
        self._ensure_async_primitives()
        async with self._audio_lock:
            self._acquire_audio_owner(AudioOwner.TTS, sid)
            self._set_state(VoiceState.SPEAKING, sid)
            log.info(f"[TTS][session={sid}] Speaking: '{text}'")
            import time
            t_req = time.monotonic()
            if latency_metrics is not None:
                latency_metrics["tts_request_started"] = t_req
            try:
                await self.tts.speak(text)
                t_done = time.monotonic()
                if latency_metrics is not None:
                    first_audio = getattr(self.tts, "last_first_audio_time", None) or t_req
                    latency_metrics["tts_first_audio"] = first_audio
                    latency_metrics["playback_started"] = first_audio
                    latency_metrics["playback_complete"] = t_done
            except Exception as e:
                log.error(f"[TTS][session={sid}] Failed to speak: {e}")
            finally:
                self._release_audio_owner(AudioOwner.TTS, sid)

    async def _session_loop(self, session_id: str):
        log.info(f"[Session][session={session_id}] Active session loop started.")
        self._in_session = True
        try:
            while self._in_session and not getattr(self, '_stopped', False) and self.state != VoiceState.MUTED:
                # 0. Check for any pending unsolicited speech before listening
                while not self._unsolicited_queue.empty():
                    queued_text = await self._unsolicited_queue.get()
                    await self._play_tts(queued_text, session_id)

                # 1. LISTENING
                interaction_id = uuid.uuid4().hex[:6]
                log.info(f"[Interaction][session={session_id}][id={interaction_id}] Beginning STT capture.")
                
                final_text = ""
                import time
                t_mic_open = time.monotonic()
                t_speech_first = None
                t_speech_end = None
                self._ensure_async_primitives()
                async with self._audio_lock:
                    self._acquire_audio_owner(AudioOwner.ACTIVE_STT, session_id)
                    self._set_state(VoiceState.LISTENING, session_id)
                    try:
                        wait_stopped = getattr(self.wakeword, "wait_stopped", None)
                        if callable(wait_stopped) and not await asyncio.to_thread(wait_stopped):
                            raise RuntimeError("Wake microphone did not close before command capture")
                        initial_audio = getattr(self, "_pending_wake_audio", b"")
                        self._pending_wake_audio = b""
                        seed_audio = getattr(self.stt, "set_initial_audio", None)
                        if callable(seed_audio):
                            seed_audio(initial_audio)
                        async for is_final, text in self.stt.listen_stream():
                            if not self._unsolicited_queue.empty():
                                log.info(f"[Interaction][session={session_id}][id={interaction_id}] STT interrupted by unsolicited speech.")
                                break
                            if not text.strip():
                                continue
                            if t_speech_first is None:
                                t_speech_first = time.monotonic()
                            if is_final:
                                t_speech_end = time.monotonic()
                                final_text = text
                                break
                            else:
                                if self.on_partial_text:
                                    await self.on_partial_text(text)
                    finally:
                        self.stt.stop_listening()
                        self._release_audio_owner(AudioOwner.ACTIVE_STT, session_id)

                t_stt_complete = time.monotonic()
                if final_text and t_speech_end is None:
                    t_speech_end = t_stt_complete

                # If interrupted by unsolicited speech, speak it now and proceed to next turn
                if not self._unsolicited_queue.empty():
                    queued_text = await self._unsolicited_queue.get()
                    await self._play_tts(queued_text, session_id)
                    continue

                text = final_text.strip()
                # No intent repair or app-specific grammar: unreliable audio is repeated.
                if getattr(self.stt, "last_transcription_uncertain", False):
                    await self._play_tts("I couldn't hear that clearly. Please repeat the full request.", session_id)
                    continue
                import re
                wake_word = getattr(self.wakeword, "wake_word", "")
                if isinstance(wake_word, str) and wake_word:
                    text = re.sub(r"^" + re.escape(wake_word) + r"(?:[\s,.!?]+|$)", "", text, flags=re.IGNORECASE).strip()

                # Local session-exit intent detection
                if is_session_exit_command(text):
                    log.info(f"[Session][session={session_id}] Stop phrase detected: '{text}'. Ending session.")
                    await self._play_tts("Going to sleep.", session_id)
                    self._in_session = False
                    if is_sleep_command(text) and getattr(self, "on_sleep_command", None):
                        try:
                            await self.on_sleep_command()
                        except Exception as e:
                            log.warning(f"Error executing on_sleep_command: {e}")
                    break

                # Silence (no speech detected)
                if not text:
                    if not self.continuous_session:
                        log.info(f"[Session][session={session_id}] Single-turn mode completed; returning to IDLE.")
                        self._in_session = False
                        break

                    log.info(f"[Session][session={session_id}] No speech detected. Waiting in ACTIVE_SESSION for up to {self.session_timeout_secs}s.")
                    self._set_state(VoiceState.ACTIVE_SESSION, session_id)
                    
                    unsolicited_speech = None
                    try:
                        unsolicited_speech = await asyncio.wait_for(
                            self._unsolicited_queue.get(),
                            timeout=self.session_timeout_secs
                        )
                    except asyncio.TimeoutError:
                        pass
                        
                    if unsolicited_speech:
                        log.info(f"[Session][session={session_id}] Unsolicited speech received during silence window.")
                        await self._play_tts(unsolicited_speech, session_id)
                        continue
                    else:
                        log.info(f"[Session][session={session_id}] Session timeout reached ({self.session_timeout_secs}s).")
                        self._set_state(VoiceState.SESSION_TIMEOUT, session_id)
                        self._in_session = False
                        break

                # 2. THINKING
                log.info(f"[Interaction][session={session_id}][id={interaction_id}] Command text: '{text}'. Transitioning to THINKING.")
                self._set_state(VoiceState.THINKING, session_id)

                wake_ts = self._last_wake_time or t_mic_open
                self._last_wake_time = None
                turn_latency_metrics = {
                    "wake_detected": wake_ts,
                    "microphone_open": t_mic_open,
                    "speech_first_detected": t_speech_first or t_mic_open,
                    "speech_end": t_speech_end or t_stt_complete,
                    "stt_complete": t_stt_complete,
                    "interaction_id": interaction_id,
                    "session_id": session_id,
                }

                try:
                    response_text = await self.on_text_recognized(text, latency_metrics=turn_latency_metrics)
                except TypeError:
                    response_text = await self.on_text_recognized(text)

                # Check if unsolicited speech arrived while thinking
                while not self._unsolicited_queue.empty():
                    queued_text = await self._unsolicited_queue.get()
                    await self._play_tts(queued_text, session_id)

                # 3. SPEAKING
                if response_text:
                    await self._play_tts(response_text, session_id, latency_metrics=turn_latency_metrics)
                self.last_latency_metrics = turn_latency_metrics

                if not self.continuous_session:
                    log.info(f"[Session][session={session_id}] Single-turn complete; returning to IDLE.")
                    self._in_session = False
                    break

        except asyncio.CancelledError:
            log.info(f"[Session][session={session_id}] Session loop cancelled.")
        except Exception as e:
            log.error(f"[Session][session={session_id}] Session loop error: {e}", exc_info=True)
        finally:
            self._in_session = False
            if not getattr(self, '_stopped', False) and self.state != VoiceState.MUTED:
                self._return_to_idle(session_id)

    async def _handle_interaction(self):
        """Single interaction step: listen -> think -> speak."""
        self._ensure_async_primitives()
        self._stopped = False
        self.wakeword.stop()
        self._audio_owner = AudioOwner.NONE
        session_id = uuid.uuid4().hex[:8]
        self._current_session_id = session_id
        await self._session_loop(session_id)

    def _return_to_idle(self, session_id: Optional[str] = None):
        sid = session_id or self._current_session_id or "none"
        if not getattr(self, '_stopped', False) and self.state != VoiceState.MUTED:
            self._in_session = False
            self.stt.stop_listening()
            self.tts.stop_speaking()
            self._audio_owner = AudioOwner.WAKE_LISTENER
            self._set_state(VoiceState.IDLE, sid)
            self.wakeword.start(self._on_wake_word_detected)
            log.info(f"[Session][session={sid}] Returned to IDLE state. WakeWord active.")

