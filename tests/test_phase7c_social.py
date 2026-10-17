"""
tests/test_phase7c_social.py
----------------------------
Deterministic tests for Cortex Phase 7C:
- relationship-aware context injection
- preference-aware answer
- primary vs known vs unknown speaker
- multi-speaker shared context
- relevant social memory retrieval
- explicit fact persistence
- temporary statements not persisted
- unified Cortex identity
- ambiguity handling
- DB safety
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
from hub.social import SocialContextManager, is_valid_social_fact, get_tone_guidance


@pytest.mark.asyncio
async def test_relationship_aware_context_injection():
    """Verify context includes current_speaker and social_context with relationship-aware tone."""
    device_id = "test_dev_social_ctx"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    # 1. Create Alex (brother)
    alex_id = await database.create_person({
        "display_name": "Alex",
        "relationship_type": "brother",
        "aliases": ["Alex"]
    })
    spk_alex = await SpeakerManager.resolve_speaker("Alex")

    # 2. Get active context for Alex speaking
    ctx_alex = await ContextManager.get_active_context(device_id, "What's the plan?", speaker_info=spk_alex)
    assert ctx_alex["current_speaker"]["display_name"] == "Alex"
    assert ctx_alex["current_speaker"]["relationship_type"] == "brother"
    assert "familiar" in ctx_alex["social_context"]["tone_guidance"]

    # 3. Create Dr. Smith (professor)
    prof_id = await database.create_person({
        "display_name": "Dr. Smith",
        "relationship_type": "professor",
        "aliases": ["Dr. Smith"]
    })
    spk_prof = await SpeakerManager.resolve_speaker("Dr. Smith")

    # 4. Get active context for Dr. Smith speaking
    ctx_prof = await ContextManager.get_active_context(device_id, "Review my draft", speaker_info=spk_prof)
    assert ctx_prof["current_speaker"]["display_name"] == "Dr. Smith"
    assert ctx_prof["current_speaker"]["relationship_type"] == "professor"
    assert "professional" in ctx_prof["social_context"]["tone_guidance"]


@pytest.mark.asyncio
async def test_preference_aware_answer():
    """Verify stored personal preferences are incorporated into responses."""
    device_id = "test_dev_pref"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # 1. Sarah prefers text instead of calls
    sarah_id = await database.create_person({"display_name": "Sarah", "relationship_type": "friend"})
    await database.add_person_fact({
        "person_id": sarah_id,
        "key": "communication_preference",
        "value": "prefers text instead of calls",
        "confidence": 1.0,
        "source": "user"
    })

    # 2. Ask how to contact Sarah
    ctx = await ContextManager.get_active_context(device_id, "How should I contact Sarah?")
    res = await harness.process_request(device_id, "How should I contact Sarah?", ctx, None)
    assert res.status == "success"
    assert "Sarah" in res.message
    assert "text instead of calls" in res.message


@pytest.mark.asyncio
async def test_primary_vs_known_vs_unknown_speaker():
    """Verify distinct tone guidance for primary, known, and unknown speakers."""
    # 1. Primary user
    tone_primary = get_tone_guidance(None, is_primary_user=True)
    assert "direct" in tone_primary or "collaborative" in tone_primary

    # 2. Known friend
    tone_friend = get_tone_guidance("friend", is_primary_user=False)
    assert "casual" in tone_friend or "warm" in tone_friend

    # 3. Known colleague
    tone_colleague = get_tone_guidance("colleague", is_primary_user=False)
    assert "professional" in tone_colleague or "polite" in tone_colleague

    # 4. Unknown speaker
    tone_unknown = get_tone_guidance(None, is_primary_user=False)
    assert "neutral" in tone_unknown or "objective" in tone_unknown


@pytest.mark.asyncio
async def test_multi_speaker_shared_context():
    """Verify group conversation tracking and constraints influencing answers."""
    device_id = "test_dev_group"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # Create Alex (friend)
    alex_id = await database.create_person({"display_name": "Alex", "relationship_type": "friend"})
    spk_alex = await SpeakerManager.resolve_speaker("Alex")
    spk_primary = await SpeakerManager.resolve_speaker("Primary User")

    # Turn 1: Primary User
    await SessionContextManager.update_session(
        device_id=device_id,
        request="Alex and I are deciding where to eat.",
        response={"status": "success", "message": "Sure, what are you considering?"},
        speaker_info=spk_primary
    )

    # Turn 2: Alex states dislike of sushi
    await SessionContextManager.update_session(
        device_id=device_id,
        request="I don't like sushi.",
        response={"status": "success", "message": "Noted, avoiding sushi."},
        speaker_info=spk_alex
    )

    # Turn 3: Primary user asks what to get
    ctx3 = await ContextManager.get_active_context(device_id, "What should we get?", speaker_info=spk_primary)
    assert "Alex" in ctx3["social_context"]["active_participants"]
    assert any("sushi" in f.get("value", "") for f in ctx3["social_context"]["relevant_shared_facts"])

    res3 = await harness.process_request(device_id, "What should we get?", ctx3, None)
    assert res3.status == "success"
    # Response incorporates Alex's dislike of sushi
    assert "Alex" in res3.message
    assert "sushi" in res3.message


@pytest.mark.asyncio
async def test_relevant_social_memory_retrieval():
    """Verify that social context provides bounded, relevant shared facts without dumping all database memories."""
    device_id = "test_dev_retrieval"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    p_id = await database.create_person({"display_name": "Jordan", "relationship_type": "roommate"})
    for i in range(15):
        await database.add_person_fact({
            "person_id": p_id,
            "key": f"preference_{i}",
            "value": f"value_{i}",
            "confidence": 1.0,
            "source": "test"
        })

    ctx = await ContextManager.get_active_context(device_id, "Jordan's food preference")
    shared_facts = ctx["social_context"]["relevant_shared_facts"]
    # Shared facts must remain strictly bounded (<= 8 items)
    assert len(shared_facts) <= 8


@pytest.mark.asyncio
async def test_explicit_fact_persistence():
    """Verify that explicit facts and preferences pass conservative write policy and are stored."""
    p_id = await database.create_person({"display_name": "Mike", "relationship_type": "friend"})

    # 1. Validate policy
    is_valid, reason = is_valid_social_fact("Mike works at Nvidia", "workplace", "Nvidia")
    assert is_valid is True

    # 2. Store via SocialContextManager
    res = await SocialContextManager.remember_social_interaction(p_id, "workplace", "Nvidia")
    assert res["status"] == "success"

    # 3. Retrieve from DB
    facts = await database.get_person_facts(p_id)
    assert any(f["key"] == "workplace" and f["value"] == "Nvidia" for f in facts)


@pytest.mark.asyncio
async def test_temporary_statements_not_persisted():
    """Verify conservative policy rejects temporary moods, sarcasm, and speculative personality judgments."""
    # Temporary mood
    v1, r1 = is_valid_social_fact("I'm feeling annoyed today", "mood", "annoyed today")
    assert v1 is False
    assert "temporary" in r1.lower()

    # Sarcasm / joke
    v2, r2 = is_valid_social_fact("I love getting up at 4 AM, just kidding haha", "habit", "wakes at 4am")
    assert v2 is False
    assert "sarcasm" in r2.lower() or "joke" in r2.lower()

    # Speculative judgment
    v3, r3 = is_valid_social_fact("He seems like a narcissist", "opinion", "toxic person")
    assert v3 is False
    assert "speculative" in r3.lower()


@pytest.mark.asyncio
async def test_unified_cortex_identity():
    """Verify that Cortex retains its unified AI assistant identity across all speakers and relationships."""
    from hub.llm_provider import _GEMINI_SYSTEM_PROMPT
    assert "You are the intelligence engine for Cortex, a personal AI assistant" in _GEMINI_SYSTEM_PROMPT
    assert "You are Cortex, a unified assistant identity" in _GEMINI_SYSTEM_PROMPT
    # No split personas or conflicting identity rules
    assert "personality split" not in _GEMINI_SYSTEM_PROMPT.lower()


@pytest.mark.asyncio
async def test_ambiguity_handling():
    """Verify that ambiguous participant references in social context ask for clarification."""
    device_id = "test_dev_social_ambig"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # Two friends named Alex
    await database.create_person({"display_name": "Alex Baker", "relationship_type": "friend"})
    await database.create_person({"display_name": "Alex Carter", "relationship_type": "friend"})

    ctx = await ContextManager.get_active_context(device_id, "How should I contact Alex?")
    res = await harness.process_request(device_id, "How should I contact Alex?", ctx, None)
    assert res.status == "success"
    # Asks for clarification rather than picking one or crashing
    assert "multiple people" in res.message.lower() or "which one" in res.message.lower()


@pytest.mark.asyncio
async def test_bounded_social_capabilities():
    """Verify social.context and social.remember_interaction capabilities execute cleanly."""
    device_id = "test_dev_social_caps"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # 1. social.context capability
    plan_ctx = ActionPlan(
        intent="get_social_context",
        source_device=device_id,
        target_device_reference="this device",
        capability="social.context",
        action="get_social_context",
        arguments={},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="getting social context"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_ctx))
    res_ctx = await harness.process_request(device_id, "Get social context", {}, None)
    assert res_ctx.status == "success"
    assert "social context" in res_ctx.message.lower()

    # 2. social.remember_interaction capability (valid)
    p_id = await database.create_person({"display_name": "Emma", "relationship_type": "coworker"})
    plan_rem = ActionPlan(
        intent="remember_social_interaction",
        source_device=device_id,
        target_device_reference="this device",
        capability="social.remember_interaction",
        action="remember_social_interaction",
        arguments={"person_id": p_id, "key": "dietary_preference", "value": "vegetarian"},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="remembering dietary preference"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_rem))
    res_rem = await harness.process_request(device_id, "Remember Emma is vegetarian", {}, None)
    assert res_rem.status == "success"
    assert "vegetarian" in res_rem.message.lower()

    # 3. social.remember_interaction capability (rejected by conservative policy)
    plan_rej = ActionPlan(
        intent="remember_social_interaction",
        source_device=device_id,
        target_device_reference="this device",
        capability="social.remember_interaction",
        action="remember_social_interaction",
        arguments={"person_id": p_id, "key": "mood", "value": "cranky today", "statement": "she is feeling cranky today"},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="remembering mood"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_rej))
    res_rej = await harness.process_request(device_id, "Remember Emma is cranky today", {}, None)
    assert res_rej.status == "rejected"
    assert "temporary" in res_rej.message.lower()


@pytest.mark.asyncio
async def test_production_db_safety():
    """Verify that production DB (data/cortex.db) remains 100% untouched and isolated."""
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    prod_db_path = Path("data/cortex.db").resolve()
    assert resolved_test_db != prod_db_path, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
    assert prod_db_path.exists(), "Production DB data/cortex.db must exist."
