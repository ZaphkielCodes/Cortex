import asyncio
import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch

from agent.voice.controller import VoiceController, VoiceState, AudioOwner
from agent.voice.mock_providers import MockWakeWordProvider, MockSTTProvider, MockTTSProvider
from hub.intent_schema import ActionPlan, ModelResult, ConversationalResponse
from hub.agent_harness import format_action_approval_phrase, AgentHarness
from hub.control_layer import CortexControlLayer
from hub.llm_provider import CircuitBreakerProvider, MockLLMProvider, LLMProvider


class FastTTS(MockTTSProvider):
    def __init__(self):
        super().__init__()
        self.spoken_texts = []
        
    async def speak(self, text: str):
        self._speaking = True
        self.spoken_texts.append(text)
        await asyncio.sleep(0.02)
        self._speaking = False


# ==============================================================================
# 1. Approval Phrasing Tests
# ==============================================================================
def test_approval_phrasing_polish():
    plan_screen = ActionPlan(
        intent="capture_screen",
        action="take_screenshot",
        capability="screen.capture",
        source_device="dev1",
        confidence=1.0,
        user_facing_summary="taking a screenshot"
    )
    phrase_screen = format_action_approval_phrase(plan_screen)
    assert phrase_screen == "take a screenshot"
    assert f"I need your approval to {phrase_screen}." == "I need your approval to take a screenshot."

    plan_status = ActionPlan(
        intent="get_device_status",
        action="get_status",
        capability="device.status",
        source_device="dev1",
        confidence=1.0,
        user_facing_summary="getting device status"
    )
    phrase_status = format_action_approval_phrase(plan_status)
    assert phrase_status == "check device status"
    assert f"I need your approval to {phrase_status}." == "I need your approval to check device status."

    plan_calc = ActionPlan(
        intent="open_app",
        action="open_app",
        capability="app.launch",
        source_device="dev1",
        confidence=1.0,
        arguments={"app": "calculator"},
        user_facing_summary="opening calculator"
    )
    phrase_calc = format_action_approval_phrase(plan_calc)
    assert phrase_calc == "open calculator"
    assert f"I need your approval to {phrase_calc}." == "I need your approval to open calculator."


# ==============================================================================
# 2. Audio State Machine & Invariant Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_audio_ownership_invariant_and_transitions():
    wakeword = MockWakeWordProvider()
    
    class ScriptedSTT(MockSTTProvider):
        def __init__(self, utterances):
            super().__init__()
            self.utterances = list(utterances)
            
        async def listen_stream(self):
            self._listening = True
            if self.utterances:
                text = self.utterances.pop(0)
            else:
                text = ""
            await asyncio.sleep(0.02)
            self._listening = False
            yield True, text

    stt = ScriptedSTT(["open calculator", ""])
    tts = FastTTS()
    states_observed = []
    
    async def dummy_recognizer(text):
        return f"Opened {text}."
        
    def on_state(state):
        states_observed.append(state)

    vc = VoiceController(
        wakeword=wakeword,
        stt=stt,
        tts=tts,
        on_text_recognized=dummy_recognizer,
        on_state_change=on_state,
        continuous_session=True,
        session_timeout_secs=0.1
    )

    vc.start()
    assert vc._audio_owner == AudioOwner.WAKE_LISTENER
    assert vc.state == VoiceState.IDLE

    # Trigger wake word
    wakeword.trigger()
    await asyncio.sleep(0.01)
    assert VoiceState.WAKE_DETECTED in states_observed

    # Wait for first turn to complete and enter silence window
    await asyncio.sleep(0.1)
    assert "Opened open calculator." in tts.spoken_texts

    # Wait for silence timeout
    await asyncio.sleep(0.2)
    assert VoiceState.ACTIVE_SESSION in states_observed
    assert VoiceState.SESSION_TIMEOUT in states_observed
    assert states_observed[-1] == VoiceState.IDLE
    assert vc._audio_owner == AudioOwner.WAKE_LISTENER
    assert wakeword._running

    vc.stop()


# ==============================================================================
# 3. Stop Phrase "Go To Sleep" Ends Active Session Immediately
# ==============================================================================
@pytest.mark.asyncio
async def test_go_to_sleep_ends_session():
    wakeword = MockWakeWordProvider()
    
    class SleepSTT(MockSTTProvider):
        async def listen_stream(self):
            self._listening = True
            await asyncio.sleep(0.02)
            self._listening = False
            yield True, "go to sleep"

    tts = FastTTS()
    vc = VoiceController(
        wakeword=wakeword,
        stt=SleepSTT(),
        tts=tts,
        on_text_recognized=AsyncMock(return_value=""),
        continuous_session=True,
        session_timeout_secs=5.0
    )

    vc.start()
    wakeword.trigger()
    await asyncio.sleep(0.1)

    assert vc.state == VoiceState.IDLE
    assert vc._audio_owner == AudioOwner.WAKE_LISTENER
    assert wakeword._running
    vc.stop()


# ==============================================================================
# 4. Unsolicited Response Interruption & No Overlapping Listeners
# ==============================================================================
@pytest.mark.asyncio
async def test_unsolicited_response_audio_serialization():
    wakeword = MockWakeWordProvider()
    
    class LongListenSTT(MockSTTProvider):
        def __init__(self):
            super().__init__()
            self.sessions_started = 0
            self.active_sessions = 0
            self.max_concurrent = 0

        async def listen_stream(self):
            self.sessions_started += 1
            self.active_sessions += 1
            self.max_concurrent = max(self.max_concurrent, self.active_sessions)
            self._listening = True
            try:
                while self._listening:
                    await asyncio.sleep(0.02)
                yield True, ""
            finally:
                self.active_sessions -= 1

    stt = LongListenSTT()
    tts = FastTTS()
    
    vc = VoiceController(
        wakeword=wakeword,
        stt=stt,
        tts=tts,
        on_text_recognized=AsyncMock(return_value=""),
        continuous_session=True,
        session_timeout_secs=0.5
    )

    vc.start()
    wakeword.trigger()
    await asyncio.sleep(0.05)
    
    # Verify STT is currently listening
    assert vc.state == VoiceState.LISTENING
    assert stt._listening
    assert stt.active_sessions == 1

    # Simulate unsolicited post-approval execution response arriving
    await vc.speak_result("Screenshot saved: screenshot_123.png")
    
    # Wait for unsolicited TTS to complete and resume STT
    await asyncio.sleep(0.1)

    # Invariant: there was NEVER more than 1 concurrent STT listener
    assert stt.max_concurrent == 1
    assert vc._audio_owner in (AudioOwner.ACTIVE_STT, AudioOwner.NONE)

    vc.stop()


# ==============================================================================
# 5. Circuit Breaker 429 Quota Immediate Failover Test
# ==============================================================================
@pytest.mark.asyncio
async def test_circuit_breaker_429_immediate_failover():
    class FailingPrimary(LLMProvider):
        @property
        def name(self): return "primary_gemini"
        @property
        def model(self): return "gemini-3.8-flash"
        async def generate_result(self, user_text, context, on_chunk=None):
            raise Exception("429 RESOURCE_EXHAUSTED: Quota exceeded for metric generate_content")

    class FastFallback(LLMProvider):
        @property
        def name(self): return "fallback_openrouter"
        @property
        def model(self): return "openai/gpt-4o-mini"
        async def generate_result(self, user_text, context, on_chunk=None):
            return ModelResult(type="RESPONSE", response_text="No need to apologize! How can I assist you today?")

    cb = CircuitBreakerProvider(primary=FailingPrimary(), fallback=FastFallback())
    
    t0 = time.monotonic()
    result = await cb.generate_result("I'm sorry.", {"force_response_only": True})
    elapsed = time.monotonic() - t0

    assert result.type == "RESPONSE"
    assert "No need to apologize" in result.response_text
    # Must fail over immediately without waiting 30s or sleeping 1s
    assert elapsed < 1.0
    # Circuit breaker must be open
    assert cb.circuit_open_until > time.time()


# ==============================================================================
# 6. Session Repeat ("Do it again") Test
# ==============================================================================
@pytest.mark.asyncio
async def test_session_repeat_do_it_again():
    control = CortexControlLayer(connected_devices_map={"dev1": AsyncMock()})
    harness = AgentHarness(control_layer=control)

    context = {
        "device": {"id": "dev1", "capabilities": ["screen.capture", "app.launch"]},
        "session": {
            "turns": [
                {"request": "Take a screenshot", "action": "take_screenshot"}
            ],
            "references": {
                "last_action": "take_screenshot",
                "last_intent": "capture_screen",
                "last_capability": "screen.capture"
            }
        }
    }

    resp = await harness.process_request(
        source_device_id="dev1",
        natural_request="Do it again",
        context=context,
        log_action_callback=None
    )

    assert resp.action_plan is not None
    assert resp.action_plan.action == "take_screenshot"
    assert resp.action_plan.capability == "screen.capture"
