"""
tests/test_phase7a_people.py
----------------------------
Deterministic tests for Phase 7A: People Memory and Relationship Model.

Covers:
- create person
- aliases
- relationship lookup
- facts
- duplicate prevention
- ambiguous names
- persistence across restart
- session follow-up references
- bounded capabilities (people.search, people.get, people.remember, people.update)
- natural language statements and questions
"""

import pytest
import pytest_asyncio
import uuid
from unittest.mock import AsyncMock

import hub.database as database
from hub.people import PeopleManager
from hub.session_context import SessionContextManager
from hub.context import ContextManager
from hub.agent_harness import AgentHarness
from hub.control_layer import CortexControlLayer
from hub.intent_schema import ActionPlan, ModelResult
import hub.main


@pytest_asyncio.fixture(autouse=True)
async def setup_test_state():
    hub.main.CONNECTED_DEVICES.clear()
    SessionContextManager._memory_cache.clear()
    yield


# ---------------------------------------------------------------------------
# 1. Create Person
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_person():
    res = await PeopleManager.remember_person({
        "name": "Alex",
        "relationship_type": "brother",
        "notes": "Loves hiking and chess",
    })
    assert res["status"] == "success"
    assert res["action"] == "created"
    assert res["display_name"] == "Alex"
    person_id = res["person_id"]

    # Verify database persistence
    person = await database.get_person(person_id)
    assert person is not None
    assert person["person_id"] == person_id
    assert person["display_name"] == "Alex"
    assert person["relationship_type"] == "brother"
    assert "Loves hiking and chess" in person["notes"]
    assert person["created_at"] != ""
    assert person["updated_at"] != ""

    # Verify mirrored to Cortex memory
    memories = await database.get_memory_items(category="person")
    assert any("Alex" in m["key"] for m in memories)


# ---------------------------------------------------------------------------
# 2. Aliases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aliases():
    res = await PeopleManager.remember_person({
        "name": "Alexander",
        "aliases": ["Alex", "Lex", "bro"],
        "relationship_type": "brother"
    })
    assert res["status"] == "success"
    alex_id = res["person_id"]

    # Resolve by alias "Lex"
    res_lex = await PeopleManager.resolve_person("Lex")
    assert res_lex.matched is True
    assert res_lex.person["person_id"] == alex_id
    assert res_lex.match_type == "alias"

    # Resolve by alias "Alex"
    res_alex = await PeopleManager.resolve_person("Alex")
    assert res_alex.matched is True
    assert res_alex.person["person_id"] == alex_id

    # Add an additional alias via remember_person
    await PeopleManager.remember_person({
        "name": "Alexander",
        "aliases": ["little bro"]
    })
    res_little = await PeopleManager.resolve_person("little bro")
    assert res_little.matched is True
    assert res_little.person["person_id"] == alex_id


# ---------------------------------------------------------------------------
# 3. Relationship Lookup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_relationship_lookup():
    res_alex = await PeopleManager.remember_person({
        "name": "Alex",
        "relationship_type": "brother"
    })
    res_john = await PeopleManager.remember_person({
        "name": "John",
        "relationship_type": "friend"
    })

    # Lookup by "my brother"
    res_rel = await PeopleManager.resolve_person("my brother")
    assert res_rel.matched is True
    assert res_rel.person["person_id"] == res_alex["person_id"]
    assert res_rel.person["display_name"] == "Alex"

    # Lookup by "brother"
    res_b = await PeopleManager.resolve_person("brother")
    assert res_b.matched is True
    assert res_b.person["display_name"] == "Alex"

    # Lookup by "my friend"
    res_f = await PeopleManager.resolve_person("my friend")
    assert res_f.matched is True
    assert res_f.person["display_name"] == "John"

    # Inter-person relationship
    rel_id = await database.add_relationship({
        "source_person_id": res_alex["person_id"],
        "target_person_id": res_john["person_id"],
        "relationship_type": "coworker",
        "metadata": {"team": "Engineering"}
    })
    assert rel_id is not None
    rels = await database.get_relationships(res_alex["person_id"])
    assert len(rels) >= 1
    assert rels[0]["relationship_type"] == "coworker"
    assert rels[0]["metadata"]["team"] == "Engineering"


# ---------------------------------------------------------------------------
# 4. Facts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_facts():
    res = await PeopleManager.remember_person({
        "name": "Mike",
        "key": "workplace",
        "value": "Nvidia"
    })
    mike_id = res["person_id"]

    # Add another fact directly
    fid = await database.add_person_fact({
        "person_id": mike_id,
        "key": "college",
        "value": "Stanford",
        "confidence": 0.95,
        "source": "user"
    })
    assert fid is not None

    # Retrieve facts
    facts = await database.get_person_facts(mike_id)
    assert len(facts) == 2
    fact_map = {f["key"]: f["value"] for f in facts}
    assert fact_map["workplace"] == "Nvidia"
    assert fact_map["college"] == "Stanford"

    # Retrieve person details with attached facts
    details = await PeopleManager.get_person_details("Mike")
    assert details is not None
    assert len(details["facts"]) == 2

    # Delete a fact
    deleted = await database.delete_person_fact(fid)
    assert deleted is True
    facts_after = await database.get_person_facts(mike_id)
    assert len(facts_after) == 1


# ---------------------------------------------------------------------------
# 5. Duplicate Prevention
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_prevention():
    # Turn 1: Initial remember
    res1 = await PeopleManager.remember_person({
        "name": "Alex",
        "relationship_type": "brother"
    })
    assert res1["action"] == "created"
    alex_id = res1["person_id"]

    people_count_1 = len(await database.list_people())
    assert people_count_1 == 1

    # Turn 2: Subsequent fact for the same person
    res2 = await PeopleManager.remember_person({
        "name": "Alex",
        "key": "workplace",
        "value": "Nvidia"
    })
    assert res2["action"] == "updated"
    assert res2["person_id"] == alex_id

    people_count_2 = len(await database.list_people())
    assert people_count_2 == 1, "Duplicate prevention failed: multiple rows created for Alex!"

    # Turn 3: Add alias without creating a new entity
    res3 = await PeopleManager.remember_person({
        "name": "Alex",
        "aliases": ["Lex"]
    })
    assert res3["action"] == "updated"
    assert res3["person_id"] == alex_id

    people_count_3 = len(await database.list_people())
    assert people_count_3 == 1

    # Verify all data consolidated on single record
    alex = await database.get_person(alex_id)
    assert alex["relationship_type"] == "brother"
    assert "Lex" in alex["aliases"]
    alex_facts = await database.get_person_facts(alex_id)
    assert any(f["key"] == "workplace" and f["value"] == "Nvidia" for f in alex_facts)


# ---------------------------------------------------------------------------
# 6. Ambiguous Names
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ambiguous_names():
    # Create two people with the same first name
    p1 = await database.create_person({
        "display_name": "Sarah Connor",
        "aliases": ["Sarah"],
        "notes": "Hero"
    })
    p2 = await database.create_person({
        "display_name": "Sarah Jones",
        "aliases": ["Sarah"],
        "notes": "Colleague"
    })

    # Resolve "Sarah" -> should detect ambiguity and NOT merge them
    res = await PeopleManager.resolve_person("Sarah")
    assert res.matched is False
    assert res.is_ambiguous is True
    assert len(res.candidates) == 2
    candidate_names = [c["display_name"] for c in res.candidates]
    assert "Sarah Connor" in candidate_names
    assert "Sarah Jones" in candidate_names

    # Attempting to remember fact for ambiguous "Sarah" returns clarification request
    rem_res = await PeopleManager.remember_person({
        "name": "Sarah",
        "key": "status",
        "value": "busy"
    })
    assert rem_res["status"] == "ambiguous"
    assert "multiple people" in rem_res["message"].lower()

    # Total people count remains untouched
    all_people = await database.list_people()
    assert len(all_people) == 2


# ---------------------------------------------------------------------------
# 7. Persistence Across Restart
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persistence_across_restart():
    # Insert person, facts, and relationships
    alex_id = await database.create_person({
        "display_name": "Alex",
        "aliases": ["bro", "Alexander"],
        "relationship_type": "brother",
        "notes": "Birthday in March"
    })
    await database.add_person_fact({
        "person_id": alex_id,
        "key": "workplace",
        "value": "Nvidia",
        "confidence": 1.0,
        "source": "user"
    })
    emma_id = await database.create_person({
        "display_name": "Emma",
        "relationship_type": "sister"
    })
    await database.add_relationship({
        "source_person_id": alex_id,
        "target_person_id": emma_id,
        "relationship_type": "sibling",
        "metadata": {"confirmed": True}
    })

    # Simulate restart
    await database.close_db()
    SessionContextManager._memory_cache.clear()
    await database.init_db()

    # Verify Alex survived restart
    alex = await database.get_person(alex_id)
    assert alex is not None
    assert alex["display_name"] == "Alex"
    assert alex["relationship_type"] == "brother"
    assert "bro" in alex["aliases"]
    assert alex["notes"] == "Birthday in March"

    # Verify facts survived restart
    facts = await database.get_person_facts(alex_id)
    assert len(facts) == 1
    assert facts[0]["key"] == "workplace"
    assert facts[0]["value"] == "Nvidia"

    # Verify relationships survived restart
    rels = await database.get_relationships(alex_id)
    assert len(rels) == 1
    assert rels[0]["relationship_type"] == "sibling"
    assert rels[0]["metadata"]["confirmed"] is True


# ---------------------------------------------------------------------------
# 8. Session Follow-up References ("he" -> John)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_follow_up_references():
    device_id = "test_device_1"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    # Seed person John with workplace
    await PeopleManager.remember_person({
        "name": "John",
        "relationship_type": "friend",
        "key": "workplace",
        "value": "Google",
        "notes": "friend from college"
    })

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # Turn 1: User mentions John
    context_1 = await ContextManager.get_active_context(device_id, request="Tell me about John.")
    resp_1 = await harness.process_request(device_id, "Tell me about John.", context_1, None)
    assert resp_1.status == "success"
    assert "John" in resp_1.message

    # Update session context
    await SessionContextManager.update_session(
        device_id,
        "Tell me about John.",
        {"status": resp_1.status, "message": resp_1.message, "display_name": "John"}
    )

    # Check session references recorded John
    session = await SessionContextManager.get_session(device_id)
    assert session["references"].get("last_person_name") == "John"

    # Turn 2: User asks follow-up with pronoun "he"
    context_2 = await ContextManager.get_active_context(device_id, request="Where does he work?")
    assert context_2["session"]["references"].get("last_person_name") == "John"
    
    resp_2 = await harness.process_request(device_id, "Where does he work?", context_2, None)
    assert resp_2.status == "success"
    assert "Google" in resp_2.message
    assert "John" in resp_2.message


# ---------------------------------------------------------------------------
# 9. Bounded Capabilities (people.remember, people.get, people.search, people.update)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bounded_capabilities():
    device_id = "test_dev"
    await database.enroll_device(device_id, "Admin Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # 1. people.remember capability
    plan_remember = ActionPlan(
        intent="remember_person",
        source_device=device_id,
        target_device_reference="this device",
        capability="people.remember",
        action="remember_person",
        arguments={"name": "Sarah", "relationship_type": "colleague", "key": "city", "value": "New York"},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="remembering Sarah"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_remember))
    res_rem = await harness.process_request(device_id, "Remember Sarah", {}, None)
    assert res_rem.status == "success"
    assert "Sarah" in res_rem.message

    # 2. people.get capability
    plan_get = ActionPlan(
        intent="get_person",
        source_device=device_id,
        target_device_reference="this device",
        capability="people.get",
        action="get_person",
        arguments={"name": "Sarah"},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="getting details for Sarah"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_get))
    res_get = await harness.process_request(device_id, "Get details for Sarah", {}, None)
    assert res_get.status == "success"
    assert "Sarah" in res_get.message
    assert "New York" in res_get.message

    # 3. people.search capability
    plan_search = ActionPlan(
        intent="search_people",
        source_device=device_id,
        target_device_reference="this device",
        capability="people.search",
        action="search_people",
        arguments={"query": "New York"},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="searching people in New York"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_search))
    res_search = await harness.process_request(device_id, "Search people in New York", {}, None)
    assert res_search.status == "success"
    assert "Sarah" in res_search.message

    # 4. people.update capability
    plan_update = ActionPlan(
        intent="update_person",
        source_device=device_id,
        target_device_reference="this device",
        capability="people.update",
        action="update_person",
        arguments={"name": "Sarah", "relationship_type": "manager"},
        requires_confirmation=False,
        confidence=1.0,
        user_facing_summary="updating Sarah"
    )
    harness.router.generate_result = AsyncMock(return_value=ModelResult(type="ACTION", action_plan=plan_update))
    res_update = await harness.process_request(device_id, "Update Sarah", {}, None)
    assert res_update.status == "success"
    assert "Sarah" in res_update.message


# ---------------------------------------------------------------------------
# 10. Natural Language Flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_natural_language_flow():
    device_id = "test_dev_nl"
    await database.enroll_device(device_id, "Laptop", "admin", "secret_hash")
    await database.update_connection_state(device_id, "connected")

    control = CortexControlLayer(hub.main.CONNECTED_DEVICES)
    harness = AgentHarness(control)

    # 1. "My brother's name is Alex."
    ctx1 = await ContextManager.get_active_context(device_id, request="My brother's name is Alex.")
    res1 = await harness.process_request(device_id, "My brother's name is Alex.", ctx1, None)
    assert res1.status == "success"
    assert "Alex" in res1.message
    await SessionContextManager.update_session(device_id, "My brother's name is Alex.", {"status": res1.status, "message": res1.message, "display_name": "Alex"})

    # 2. "What's my brother's name?"
    ctx2 = await ContextManager.get_active_context(device_id, request="What's my brother's name?")
    res2 = await harness.process_request(device_id, "What's my brother's name?", ctx2, None)
    assert res2.status == "success"
    assert "Alex" in res2.message

    # 3. "John is my friend from college."
    ctx3 = await ContextManager.get_active_context(device_id, request="John is my friend from college.")
    res3 = await harness.process_request(device_id, "John is my friend from college.", ctx3, None)
    assert res3.status == "success"
    assert "John" in res3.message
    await SessionContextManager.update_session(device_id, "John is my friend from college.", {"status": res3.status, "message": res3.message, "display_name": "John"})

    # 4. "Who do I know from college?"
    ctx4 = await ContextManager.get_active_context(device_id, request="Who do I know from college?")
    res4 = await harness.process_request(device_id, "Who do I know from college?", ctx4, None)
    assert res4.status == "success"
    assert "John" in res4.message

    # 5. "Sarah prefers text instead of calls."
    ctx5 = await ContextManager.get_active_context(device_id, request="Sarah prefers text instead of calls.")
    res5 = await harness.process_request(device_id, "Sarah prefers text instead of calls.", ctx5, None)
    assert res5.status == "success"
    assert "Sarah" in res5.message
    await SessionContextManager.update_session(device_id, "Sarah prefers text instead of calls.", {"status": res5.status, "message": res5.message, "display_name": "Sarah"})

    # 6. "What do you remember about Sarah?"
    ctx6 = await ContextManager.get_active_context(device_id, request="What do you remember about Sarah?")
    res6 = await harness.process_request(device_id, "What do you remember about Sarah?", ctx6, None)
    assert res6.status == "success"
    assert "Sarah" in res6.message
    assert "text instead of calls" in res6.message

    # 7. "Remember that Mike works at Nvidia."
    ctx7 = await ContextManager.get_active_context(device_id, request="Remember that Mike works at Nvidia.")
    res7 = await harness.process_request(device_id, "Remember that Mike works at Nvidia.", ctx7, None)
    assert res7.status == "success"
    assert "Mike" in res7.message
    assert "Nvidia" in res7.message
    await SessionContextManager.update_session(device_id, "Remember that Mike works at Nvidia.", {"status": res7.status, "message": res7.message, "display_name": "Mike"})

    # 8. Follow-up "Where does he work?"
    ctx8 = await ContextManager.get_active_context(device_id, request="Where does he work?")
    res8 = await harness.process_request(device_id, "Where does he work?", ctx8, None)
    assert res8.status == "success"
    assert "Nvidia" in res8.message
    assert "Mike" in res8.message
