"""
tests/test_phase7b_speakers.py
------------------------------
Deterministic tests for Cortex Phase 7B:
- primary user speaker
- unknown speaker
- known speaker linked to Person
- multi-speaker turns
- speaker switching
- recent speaker context
- ambiguity does not auto-merge
- persistence of speaker/person link
- bounded capabilities (speaker.get_current, speaker.resolve, speaker.link_person, speaker.list_recent)
- production DB safety
"""

import pytest
import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import hub.database as database
import hub.main
from hub.intent_schema import ActionPlan, ModelResult
from hub.control_layer import CortexControlLayer
from hub.agent_harness import AgentHarness
from hub.context import ContextManager
from hub.session_context import SessionContextManager
from hub.people import PeopleManager
from hub.speakers import SpeakerManager


@pytest.mark.asyncio
async def test_primary_user_speaker():
    """Verify primary user speaker resolution and context injection."""
    device_id = "test_dev_primary"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    primary = await SpeakerManager.resolve_speaker(speaker_hint=None)
    assert primary["is_primary_user"] is True
    assert primary["label"] == "Primary User"
    assert primary["speaker_id"] == "spk_primary"

    # Active context should reflect primary user
    ctx = await ContextManager.get_active_context(device_id, "What is my battery?")
    assert ctx["speaker"]["is_primary_user"] is True
    assert ctx["speaker"]["label"] == "Primary User"


@pytest.mark.asyncio
async def test_unknown_speaker():
    """Verify unknown speaker resolution does not silently merge with known entities."""
    device_id = "test_dev_unknown"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    unknown = await SpeakerManager.resolve_speaker(speaker_hint="Unknown Speaker 1")
    assert unknown["is_primary_user"] is False
    assert unknown["person_id"] is None
    assert unknown["confidence"] <= 0.7
    assert "unknown" in unknown["label"].lower()
    assert unknown.get("is_unknown") is True


@pytest.mark.asyncio
async def test_known_speaker_linked_to_person():
    """Verify known speaker linked to Person injects identity and updates last_interaction_at."""
    device_id = "test_dev_linked"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    # 1. Create Person Alex (brother)
    alex_id = await database.create_person({
        "display_name": "Alex",
        "relationship_type": "brother",
        "aliases": ["Alex", "brother", "my brother"],
        "notes": "Brother living in Seattle"
    })
    initial_last_interaction = ""

    # 2. Resolve speaker as "Alex"
    resolved = await SpeakerManager.resolve_speaker(speaker_hint="Alex")
    assert resolved["person_id"] == alex_id
    assert resolved["display_name"] == "Alex"
    assert resolved["relationship_type"] == "brother"

    # 3. Verify last_interaction_at was updated on Person in DB
    alex_db = await database.get_person(alex_id)
    assert alex_db["last_interaction_at"] != initial_last_interaction
    assert len(alex_db["last_interaction_at"]) > 0

    # 4. Context should include speaker relationship
    ctx = await ContextManager.get_active_context(device_id, "Turn off the lights", speaker_info=resolved)
    assert ctx["speaker_name"] == "Alex"
    assert ctx["speaker_relationship"] == "brother"
    assert any(person["person_id"] == alex_id for person in ctx["people"])


@pytest.mark.asyncio
async def test_multi_speaker_turns():
    """Verify distinct speaker-tagged turns in SessionContext."""
    device_id = "test_dev_multispeaker"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    spk_a = await SpeakerManager.resolve_speaker("Primary User")
    spk_b = await SpeakerManager.create_speaker("Guest Alice", is_primary=False, confidence=0.9)

    # Turn 1: Speaker A
    await SessionContextManager.update_session(
        device_id=device_id,
        request="What's the weather?",
        response={"status": "success", "message": "It is sunny."},
        speaker_info=spk_a
    )

    # Turn 2: Speaker B
    await SessionContextManager.update_session(
        device_id=device_id,
        request="And what about tomorrow?",
        response={"status": "success", "message": "Tomorrow will be cloudy."},
        speaker_info=spk_b
    )

    session = await SessionContextManager.get_session(device_id)
    assert len(session["turns"]) == 2
    assert session["turns"][0]["speaker_label"] == "Primary User"
    assert session["turns"][0]["request"] == "What's the weather?"
    assert session["turns"][1]["speaker_label"] == "Guest Alice"
    assert session["turns"][1]["request"] == "And what about tomorrow?"

    # Verify prompt formatted conversation retains speaker tags
    formatted = SessionContextManager.format_context_for_prompt(session)
    assert "Primary User: What's the weather?" in formatted
    assert "Guest Alice: And what about tomorrow?" in formatted


@pytest.mark.asyncio
async def test_speaker_switching():
    """Verify speaker switching updates last_speaker_change and transitions current_speaker_id."""
    device_id = "test_dev_switching"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    spk1 = await SpeakerManager.ensure_primary_speaker()
    spk2 = await SpeakerManager.create_speaker("Bob", is_primary=False)

    # Turn 1 from Speaker 1
    await SessionContextManager.update_session(
        device_id=device_id,
        request="Hello Cortex",
        response={"status": "success", "message": "Hello!"},
        speaker_info=spk1
    )
    s1 = await SessionContextManager.get_session(device_id)
    assert s1["references"]["current_speaker_id"] == spk1["speaker_id"]
    change_t1 = s1["references"].get("last_speaker_change")

    await asyncio.sleep(0.01)

    # Turn 2 from Speaker 2 (switch occurs)
    await SessionContextManager.update_session(
        device_id=device_id,
        request="Hi from Bob",
        response={"status": "success", "message": "Hello Bob!"},
        speaker_info=spk2
    )
    s2 = await SessionContextManager.get_session(device_id)
    assert s2["references"]["current_speaker_id"] == spk2["speaker_id"]
    change_t2 = s2["references"].get("last_speaker_change")
    assert change_t2 is not None
    if change_t1:
        assert change_t2 >= change_t1


@pytest.mark.asyncio
async def test_recent_speakers_context():
    """Verify session context tracks multiple recent speakers in order."""
    device_id = "test_dev_recents"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    spk_user = await SpeakerManager.ensure_primary_speaker()
    spk_charlie = await SpeakerManager.create_speaker("Charlie", is_primary=False)
    spk_david = await SpeakerManager.create_speaker("David", is_primary=False)

    for spk, text in [(spk_user, "Turn 1"), (spk_charlie, "Turn 2"), (spk_david, "Turn 3")]:
        await SessionContextManager.update_session(
            device_id=device_id,
            request=text,
            response={"status": "success", "message": "ack"},
            speaker_info=spk
        )

    recents = await SpeakerManager.list_recent_speakers(device_id)
    labels = [r["label"] for r in recents]
    assert "Primary User" in labels
    assert "Charlie" in labels
    assert "David" in labels
    assert labels[-1] == "David"


@pytest.mark.asyncio
async def test_ambiguity_does_not_auto_merge():
    """Verify that multiple matching candidates flag ambiguity rather than guessing or merging."""
    device_id = "test_dev_ambig"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    # Create two people named Alex
    await database.create_person({"display_name": "Alex Miller", "aliases": ["Alex"]})
    await database.create_person({"display_name": "Alex Smith", "aliases": ["Alex"]})

    res = await SpeakerManager.resolve_speaker(speaker_hint="Alex")
    assert res.get("is_ambiguous") is True
    assert res.get("person_id") is None
    assert len(res.get("candidates", [])) >= 2


@pytest.mark.asyncio
async def test_persistence_of_speaker_person_link():
    """Verify speaker profile and its Person link survive memory cache reload."""
    device_id = "test_dev_persist"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    # 1. Create person Maria
    m_id = await database.create_person({"display_name": "Maria", "relationship_type": "colleague"})

    # 2. Create and link speaker
    spk = await SpeakerManager.create_speaker("Maria", is_primary=False, person_id=m_id)
    s_id = spk["speaker_id"]

    # 3. Clear in-memory caches
    SessionContextManager._memory_cache.clear()

    # 4. Fetch directly from DB reload
    reloaded_spk = await database.get_speaker_profile(s_id)
    assert reloaded_spk is not None
    assert reloaded_spk["person_id"] == m_id
    assert reloaded_spk["label"] == "Maria"

    # 5. Resolve speaker again after restart
    resolved = await SpeakerManager.resolve_speaker("Maria")
    assert resolved["person_id"] == m_id
    assert resolved["display_name"] == "Maria"
    assert resolved["relationship_type"] == "colleague"


@pytest.mark.asyncio
async def test_bounded_speaker_capabilities():
    """Verify speaker bounded capabilities (get_current, resolve, link_person, list_recent) execute properly."""
    device_id = "test_dev_caps"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # 1. speaker.get_current capability
    plan_curr = ActionPlan(
        intent="get_current_speaker",
        source_device=device_id,
        target_device_reference="this device",
        capability="speaker.get_current",
        action="get_current_speaker",
        arguments={},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="getting current speaker"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_curr))
    res_curr = await harness.process_request(device_id, "Who is the current speaker?", {}, None)
    assert res_curr.status == "success"
    assert "speaker" in res_curr.message.lower()

    # 2. speaker.resolve capability
    plan_res = ActionPlan(
        intent="resolve_speaker",
        source_device=device_id,
        target_device_reference="this device",
        capability="speaker.resolve",
        action="resolve_speaker",
        arguments={"speaker_hint": "Primary User"},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="resolving speaker"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_res))
    res_res = await harness.process_request(device_id, "Resolve primary speaker", {}, None)
    assert res_res.status == "success"
    assert "primary user" in res_res.message.lower()

    # 3. speaker.link_person capability
    person_id = await database.create_person({"display_name": "Tom", "relationship_type": "friend"})
    spk_tom = await SpeakerManager.create_speaker("Tom Speaker", is_primary=False)
    plan_link = ActionPlan(
        intent="link_speaker_person",
        source_device=device_id,
        target_device_reference="this device",
        capability="speaker.link_person",
        action="link_speaker_person",
        arguments={"speaker_id": spk_tom["speaker_id"], "person_id": person_id},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="linking speaker to person"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_link))
    res_link = await harness.process_request(device_id, "Link Tom", {}, None)
    assert res_link.status == "success"
    assert "linked" in res_link.message.lower()

    # 4. speaker.list_recent capability
    plan_list = ActionPlan(
        intent="list_recent_speakers",
        source_device=device_id,
        target_device_reference="this device",
        capability="speaker.list_recent",
        action="list_recent_speakers",
        arguments={},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="listing recent speakers"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_list))
    res_list = await harness.process_request(device_id, "List recent speakers", {}, None)
    assert res_list.status == "success"


@pytest.mark.asyncio
async def test_production_db_safety():
    """Verify that production DB (data/cortex.db) is untouched and isolated."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_db_path = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_db_path, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_db_path.exists(), "Production DB data/cortex.db must exist."
