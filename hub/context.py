"""
hub/context.py
--------------
Context Manager for Phase 5.
Combines active device, task, relevant memory, and permissions to construct
a structured context object.
"""

from typing import Dict, Any, Optional
from .database import get_memory_items, get_device, get_device_capabilities

class ContextManager:
    @staticmethod
    async def get_active_context(
        device_id: str,
        request: str = "",
        active_task_id: Optional[str] = None,
        related_entities: Optional[list[str]] = None,
        speaker_info: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        Retrieves context relevant to the current operation without blindly
        dumping all memory.
        """
        context = {
            "device": {},
            "task": {},
            "memory": [],
            "permissions": [],
            "session": {},
            "speaker": {}
        }

        # 1. Device Context
        device = await get_device(device_id)
        if device:
            context["device"]["id"] = device["device_id"]
            context["device"]["name"] = device["device_name"]
            context["device"]["role"] = device["role"]
            caps = await get_device_capabilities(device_id)
            context["device"]["capabilities"] = caps

        # 2. Active Task Context
        if active_task_id:
            from .database import get_task
            task = await get_task(active_task_id)
            if task:
                context["task"] = {
                    "id": task["id"],
                    "title": task["title"],
                    "status": task["status"],
                    "command": task["command"]
                }

        # 3. Session Context
        from .session_context import SessionContextManager
        session = await SessionContextManager.get_session(device_id)
        # Bounded recent session context
        if "turns" in session:
            session["turns"] = session["turns"][-10:] # Cap at 10 turns
        context["session"] = session

        # 4. Phase 7A: Relevant People Context
        from .people import PeopleManager
        relevant_people = await PeopleManager.find_relevant_people(request, session.get("references"))

        # 5. Phase 7B/7C: Speaker Context & Social Context
        from .speakers import SpeakerManager
        from .social import SocialContextManager
        if not speaker_info:
            speaker_info = await SpeakerManager.get_active_speaker_context(device_id, session.get("references"))
        context["speaker"] = speaker_info

        # If speaker is linked to a Person, ensure that person is included in context["people"]
        speaker_facts = []
        speaker_interactions = ""
        if speaker_info.get("person_id"):
            spk_pid = speaker_info["person_id"]
            spk_person = next((p for p in relevant_people if p.get("person_id") == spk_pid), None)
            if not spk_person:
                spk_person = await PeopleManager.get_person_details(spk_pid)
                if spk_person:
                    relevant_people.append(spk_person)
            if spk_person:
                speaker_facts = spk_person.get("facts", [])
                speaker_interactions = spk_person.get("last_interaction_at", "")

            context["speaker_relationship"] = speaker_info.get("relationship_type", "")
            context["speaker_name"] = speaker_info.get("display_name", "")

        context["people"] = relevant_people

        # Phase 7C: Structured current_speaker and social_context
        context["current_speaker"] = {
            "speaker_id": speaker_info.get("speaker_id"),
            "person_id": speaker_info.get("person_id"),
            "display_name": speaker_info.get("display_name"),
            "relationship_type": speaker_info.get("relationship_type"),
            "is_primary_user": speaker_info.get("is_primary_user", False),
            "relevant_person_facts": speaker_facts[:5],
            "recent_interactions": speaker_interactions
        }

        social_ctx = await SocialContextManager.build_social_context(
            device_id=device_id,
            request=request,
            current_speaker=context["current_speaker"],
            session_refs=session.get("references", {}),
            turns=session.get("turns", []),
            relevant_people=relevant_people
        )
        context["social_context"] = social_ctx

        # 5. Relevant Memory
        all_memories = await get_memory_items()
        relevant_memories = []
        
        # Inject relevant people facts into memory list for generic consumers
        for p in relevant_people:
            p_desc = f"{p['display_name']}" + (f" ({p.get('relationship_type')})" if p.get('relationship_type') else "")
            relevant_memories.append({
                "key": p["display_name"],
                "value": p_desc + (f" - {p.get('notes')}" if p.get('notes') else ""),
                "category": "person",
                "confidence": 1.0,
                "relationship_entity_id": p["person_id"]
            })
            for f in p.get("facts", []):
                relevant_memories.append({
                    "key": f"{p['display_name']}: {f['key']}",
                    "value": f["value"],
                    "category": "person",
                    "confidence": f.get("confidence", 1.0),
                    "relationship_entity_id": p["person_id"]
                })

        import string
        def tokenize(text: str) -> set[str]:
            if not text: return set()
            clean_text = text.lower().replace("_", " ")
            for p in string.punctuation:
                clean_text = clean_text.replace(p, " ")
            return set(clean_text.split())

        req_words = tokenize(request)
        
        for mem in all_memories:
            # Always include if explicitly related entity
            if related_entities and mem.get("relationship_entity_id") in related_entities:
                relevant_memories.append(mem)
                continue
                
            # Filter by matching words in key or value
            mem_key_words = tokenize(mem.get("key", ""))
            mem_val_words = tokenize(mem.get("value", ""))
            
            # Simple keyword intersection
            if mem.get("category") == "preference" or req_words.intersection(mem_key_words) or req_words.intersection(mem_val_words):
                relevant_memories.append(mem)

        # Ensure memory context remains bounded
        context["memory"] = [
            {"key": m["key"], "value": m["value"], "category": m["category"], "confidence": m["confidence"],
             **({"privacy_class": m["privacy_class"]} if m.get("privacy_class") else {})}
            for m in relevant_memories[:15] # Cap at 15 items
        ]

        # 6. Phase 8A: World State & Situation Context
        from .world_state import WorldStateManager
        wsm = WorldStateManager.get_instance()
        wsm.set_active_device(device_id)
        compact_world = await wsm.get_compact_world_summary(device_id)
        context["world_state"] = compact_world
        
        return context
