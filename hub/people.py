"""
hub/people.py
-------------
Phase 7A: People Memory and Relationship Model.
Handles person resolution, alias matching, fact tracking, duplicate prevention,
ambiguity detection, and memory integration.
"""

import json
import logging
import uuid
import re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from .database import (
    create_person as db_create_person,
    get_person as db_get_person,
    get_person_by_name as db_get_person_by_name,
    list_people as db_list_people,
    update_person as db_update_person,
    delete_person as db_delete_person,
    add_person_fact as db_add_person_fact,
    get_person_facts as db_get_person_facts,
    delete_person_fact as db_delete_person_fact,
    add_relationship as db_add_relationship,
    get_relationships as db_get_relationships,
    add_memory_item,
    get_memory_items,
)

log = logging.getLogger("cortex.people")


PRONOUNS = {"he", "him", "his", "she", "her", "hers", "they", "them", "their", "this person", "that person"}
RELATIONSHIP_TERMS = {
    "brother", "sister", "friend", "mother", "mom", "father", "dad",
    "coworker", "colleague", "boss", "manager", "partner", "wife", "husband",
    "girlfriend", "boyfriend", "son", "daughter", "cousin", "roommate"
}


@dataclass
class ResolutionResult:
    matched: bool
    person: Optional[dict] = None
    candidates: List[dict] = field(default_factory=list)
    is_ambiguous: bool = False
    confidence: float = 0.0
    match_type: str = "none"
    explanation: str = ""


class PeopleManager:
    """
    Central manager for people entities, aliases, relationships, and facts.
    """

    @staticmethod
    def normalize_identifier(identifier: str) -> str:
        """Strip prefixes and clean up natural identifier."""
        if not identifier:
            return ""
        s = identifier.strip().lower()
        # Strip trailing punctuation
        s = s.rstrip(".,!?:;'\"")
        # Strip common natural language prefixes
        for prefix in ["my ", "the ", "a ", "my friend ", "my brother ", "my sister ", "my colleague ", "my coworker "]:
            if s.startswith(prefix) and len(s) > len(prefix):
                s = s[len(prefix):].strip()
        return s

    @staticmethod
    async def resolve_person(
        identifier: str, session_references: Optional[dict] = None
    ) -> ResolutionResult:
        """
        Resolve an identifier (name, alias, relationship, or pronoun) to a Person.
        Detects ambiguity and prevents merging when multiple candidates match.
        """
        if not identifier:
            identifier = ""
            
        raw_clean = identifier.strip().lower().rstrip(".,!?:;'\"")
        session_refs = session_references or {}

        # 1. Pronoun / Session Reference Resolution
        if raw_clean in PRONOUNS or not raw_clean:
            last_id = session_refs.get("last_person_id")
            last_name = session_refs.get("last_person_name")
            if last_id:
                p = await db_get_person(last_id)
                if p:
                    return ResolutionResult(
                        matched=True,
                        person=p,
                        confidence=0.92,
                        match_type="session_reference",
                        explanation=f"Resolved '{identifier}' to recently referenced person {p['display_name']} via session context."
                    )
            if last_name:
                p = await db_get_person_by_name(last_name)
                if p:
                    return ResolutionResult(
                        matched=True,
                        person=p,
                        confidence=0.90,
                        match_type="session_reference",
                        explanation=f"Resolved '{identifier}' to recently referenced name '{last_name}'."
                    )

        all_people = await db_list_people()
        if not all_people:
            return ResolutionResult(matched=False, confidence=0.0, match_type="none", explanation="No people registered.")

        # 2. Exact display_name match (case-insensitive)
        exact_matches = [
            p for p in all_people
            if p["display_name"].strip().lower() == raw_clean
        ]
        if len(exact_matches) == 1:
            return ResolutionResult(
                matched=True,
                person=exact_matches[0],
                confidence=1.0,
                match_type="exact_name",
                explanation=f"Exact match on display name '{exact_matches[0]['display_name']}'."
            )
        elif len(exact_matches) > 1:
            return ResolutionResult(
                matched=False,
                is_ambiguous=True,
                candidates=exact_matches,
                confidence=0.5,
                match_type="ambiguous",
                explanation=f"Found {len(exact_matches)} people with the exact name '{raw_clean}'."
            )

        # Also check with normalized identifier (prefixes stripped)
        norm_clean = PeopleManager.normalize_identifier(identifier)
        if norm_clean != raw_clean:
            norm_matches = [
                p for p in all_people
                if p["display_name"].strip().lower() == norm_clean
            ]
            if len(norm_matches) == 1:
                return ResolutionResult(
                    matched=True,
                    person=norm_matches[0],
                    confidence=0.98,
                    match_type="exact_name",
                    explanation=f"Exact match on display name '{norm_matches[0]['display_name']}'."
                )
            elif len(norm_matches) > 1:
                return ResolutionResult(
                    matched=False,
                    is_ambiguous=True,
                    candidates=norm_matches,
                    confidence=0.5,
                    match_type="ambiguous",
                    explanation=f"Found {len(norm_matches)} people matching '{norm_clean}'."
                )

        # 3. Alias match
        alias_matches = []
        for p in all_people:
            aliases = [a.strip().lower() for a in p.get("aliases", []) if isinstance(a, str)]
            if raw_clean in aliases or norm_clean in aliases:
                alias_matches.append(p)

        if len(alias_matches) == 1:
            return ResolutionResult(
                matched=True,
                person=alias_matches[0],
                confidence=0.95,
                match_type="alias",
                explanation=f"Matched alias for '{alias_matches[0]['display_name']}'."
            )
        elif len(alias_matches) > 1:
            return ResolutionResult(
                matched=False,
                is_ambiguous=True,
                candidates=alias_matches,
                confidence=0.5,
                match_type="ambiguous",
                explanation=f"Found {len(alias_matches)} people with matching alias."
            )

        # 4. Relationship match (e.g. "my brother", "brother")
        rel_target = None
        for r_term in RELATIONSHIP_TERMS:
            if r_term in raw_clean.split() or raw_clean == r_term or raw_clean == f"my {r_term}":
                rel_target = r_term
                break

        if rel_target:
            rel_matches = [
                p for p in all_people
                if p.get("relationship_type", "").strip().lower() == rel_target
                or rel_target in [a.strip().lower() for a in p.get("aliases", [])]
            ]
            if len(rel_matches) == 1:
                return ResolutionResult(
                    matched=True,
                    person=rel_matches[0],
                    confidence=0.90,
                    match_type="relationship",
                    explanation=f"Single person matching relationship '{rel_target}': {rel_matches[0]['display_name']}."
                )
            elif len(rel_matches) > 1:
                return ResolutionResult(
                    matched=False,
                    is_ambiguous=True,
                    candidates=rel_matches,
                    confidence=0.5,
                    match_type="ambiguous",
                    explanation=f"Multiple people ({len(rel_matches)}) match relationship '{rel_target}'."
                )

        # 5. Partial / First Name match
        partial_matches = []
        for p in all_people:
            name_parts = [part.lower() for part in p["display_name"].strip().split()]
            if norm_clean in name_parts or any(part.startswith(norm_clean) for part in name_parts):
                partial_matches.append(p)

        if len(partial_matches) == 1:
            return ResolutionResult(
                matched=True,
                person=partial_matches[0],
                confidence=0.85,
                match_type="partial_name",
                explanation=f"Partial name match for '{partial_matches[0]['display_name']}'."
            )
        elif len(partial_matches) > 1:
            return ResolutionResult(
                matched=False,
                is_ambiguous=True,
                candidates=partial_matches,
                confidence=0.5,
                match_type="ambiguous",
                explanation=f"Multiple people match partial name '{norm_clean}': {[p['display_name'] for p in partial_matches]}."
            )

        return ResolutionResult(
            matched=False,
            confidence=0.0,
            match_type="none",
            explanation=f"No matching person found for '{identifier}'."
        )

    @staticmethod
    async def remember_person(data: dict, session_references: Optional[dict] = None) -> dict:
        """
        Create or update a person record with duplicate prevention and fact persistence.
        """
        name = (data.get("name") or data.get("display_name") or "").strip()
        if not name and "relationship_type" in data:
            # E.g. "my brother's name is Alex"
            name = (data.get("value") or "").strip()

        if not name:
            return {"status": "error", "message": "Person name cannot be empty."}

        rel_type = (data.get("relationship_type") or "").strip().lower()
        new_aliases = data.get("aliases", [])
        if isinstance(new_aliases, str):
            new_aliases = [new_aliases]
        notes = (data.get("notes") or "").strip()

        # Check resolution for duplicate prevention
        resolution = await PeopleManager.resolve_person(name, session_references)

        if resolution.is_ambiguous:
            candidate_names = [p["display_name"] for p in resolution.candidates]
            return {
                "status": "ambiguous",
                "message": f"I found multiple people matching '{name}': {', '.join(candidate_names)}. Which one did you mean?",
                "candidates": resolution.candidates
            }

        now = datetime.now(timezone.utc).isoformat()

        if resolution.matched and resolution.person:
            # DUPLICATE PREVENTION: update existing person
            existing = resolution.person
            person_id = existing["person_id"]
            existing_aliases = existing.get("aliases", [])
            merged_aliases = list(set(existing_aliases + new_aliases))
            
            updates = {"last_interaction_at": now}
            if merged_aliases != existing_aliases:
                updates["aliases"] = merged_aliases
            if rel_type and not existing.get("relationship_type"):
                updates["relationship_type"] = rel_type
            if notes:
                existing_notes = existing.get("notes", "")
                updates["notes"] = f"{existing_notes}\n{notes}".strip() if existing_notes else notes

            await db_update_person(person_id, updates)
            action = "updated"
            display_name = existing["display_name"]
        else:
            # CREATE NEW PERSON
            person_id = uuid.uuid4().hex
            display_name = name
            initial_aliases = list(set([name] + new_aliases))
            if rel_type and rel_type not in initial_aliases:
                initial_aliases.append(rel_type)

            person_row = {
                "person_id": person_id,
                "display_name": display_name,
                "aliases": initial_aliases,
                "relationship_type": rel_type,
                "notes": notes,
                "created_at": now,
                "updated_at": now,
                "last_interaction_at": now,
            }
            await db_create_person(person_row)
            action = "created"

            # Mirror basic person memory
            await add_memory_item({
                "id": uuid.uuid4().hex,
                "category": "person",
                "key": f"{display_name} ({rel_type})" if rel_type else display_name,
                "value": f"Known person: {display_name}" + (f", relationship: {rel_type}" if rel_type else ""),
                "source": "user",
                "confidence": 1.0,
                "relationship_entity_id": person_id,
            })

        # Add single fact if provided in payload
        fact_key = data.get("key")
        fact_val = data.get("value")
        if fact_key and fact_val:
            fact_id = uuid.uuid4().hex
            confidence = float(data.get("confidence", 1.0))
            source = data.get("source", "user")
            await db_add_person_fact({
                "id": fact_id,
                "person_id": person_id,
                "key": str(fact_key).strip(),
                "value": str(fact_val).strip(),
                "confidence": confidence,
                "source": source,
                "timestamp": now,
            })
            # Mirror to Cortex persistent memory
            await add_memory_item({
                "id": uuid.uuid4().hex,
                "category": "person",
                "key": f"{display_name}: {fact_key}",
                "value": str(fact_val).strip(),
                "source": source,
                "confidence": confidence,
                "relationship_entity_id": person_id,
            })

        # Add multiple facts if provided
        for f in data.get("facts", []):
            if isinstance(f, dict) and "key" in f and "value" in f:
                fid = uuid.uuid4().hex
                conf = float(f.get("confidence", 1.0))
                src = f.get("source", "user")
                await db_add_person_fact({
                    "id": fid,
                    "person_id": person_id,
                    "key": str(f["key"]).strip(),
                    "value": str(f["value"]).strip(),
                    "confidence": conf,
                    "source": src,
                    "timestamp": now,
                })
                await add_memory_item({
                    "id": uuid.uuid4().hex,
                    "category": "person",
                    "key": f"{display_name}: {f['key']}",
                    "value": str(f["value"]).strip(),
                    "source": src,
                    "confidence": conf,
                    "relationship_entity_id": person_id,
                })

        updated_person = await db_get_person(person_id)
        facts = await db_get_person_facts(person_id)
        if updated_person:
            updated_person["facts"] = facts

        return {
            "status": "success",
            "action": action,
            "person_id": person_id,
            "display_name": display_name,
            "person": updated_person,
        }

    @staticmethod
    async def get_person_details(identifier: str, session_references: Optional[dict] = None) -> Optional[dict]:
        """Retrieve full person profile including all facts and relationships."""
        if not identifier:
            return None
        # Direct person_id lookup check
        person = await db_get_person(identifier)
        if not person:
            res = await PeopleManager.resolve_person(identifier, session_references)
            if not res.matched or not res.person:
                return None
            person = res.person

        person["facts"] = await db_get_person_facts(person["person_id"])
        person["relationships"] = await db_get_relationships(person["person_id"])
        return person

    @staticmethod
    async def search_people(query: str) -> List[dict]:
        """Search people across display_name, aliases, notes, relationships, and facts."""
        q = (query or "").strip().lower()
        all_people = await db_list_people()
        results = []
        for p in all_people:
            p_facts = await db_get_person_facts(p["person_id"])
            p["facts"] = p_facts
            
            if not q:
                results.append(p)
                continue

            # Check matches
            matched = False
            if q in p["display_name"].lower():
                matched = True
            elif any(q in str(a).lower() for a in p.get("aliases", [])):
                matched = True
            elif q in p.get("relationship_type", "").lower():
                matched = True
            elif q in p.get("notes", "").lower():
                matched = True
            elif any(q in f["key"].lower() or q in f["value"].lower() for f in p_facts):
                matched = True

            if matched:
                results.append(p)
        return results

    @staticmethod
    async def find_relevant_people(request: str, session_references: Optional[dict] = None) -> List[dict]:
        """
        Given a natural language request and session references, retrieve all relevant people
        and their facts to inject into prompt context.
        """
        if not request:
            return []

        words = set(re.findall(r'[a-zA-Z0-9]+', request.lower()))
        session_refs = session_references or {}
        relevant: Dict[str, dict] = {}

        # 1. Check if session references point to a recent person
        last_id = session_refs.get("last_person_id")
        if last_id and (words.intersection(PRONOUNS) or len(words) <= 5):
            p = await db_get_person(last_id)
            if p:
                p["facts"] = await db_get_person_facts(p["person_id"])
                relevant[p["person_id"]] = p

        # 2. Check relationship terms in request (e.g. "brother", "friend")
        for term in RELATIONSHIP_TERMS:
            if term in words:
                res = await PeopleManager.resolve_person(term, session_references)
                if res.matched and res.person and res.person["person_id"] not in relevant:
                    res.person["facts"] = await db_get_person_facts(res.person["person_id"])
                    relevant[res.person["person_id"]] = res.person

        # 3. Check all people in DB for name, alias, or fact keyword hits
        all_people = await db_list_people()
        for p in all_people:
            pid = p["person_id"]
            if pid in relevant:
                continue

            p_name = p["display_name"].lower()
            p_name_words = set(re.findall(r'[a-zA-Z0-9]+', p_name))
            aliases = [str(a).lower() for a in p.get("aliases", [])]
            rel_type = p.get("relationship_type", "").lower()

            # Hit on name or alias
            if words.intersection(p_name_words) or any(a in words for a in aliases) or (rel_type and rel_type in words):
                p["facts"] = await db_get_person_facts(pid)
                relevant[pid] = p
                continue

            # Hit on fact keywords
            p_facts = await db_get_person_facts(pid)
            fact_hit = False
            for f in p_facts:
                f_words = set(re.findall(r'[a-zA-Z0-9]+', (f["key"] + " " + f["value"]).lower()))
                # Ignore very common words like "the", "a", "is"
                meaningful_overlap = words.intersection(f_words) - {"the", "a", "an", "is", "at", "in", "to", "of", "and"}
                if meaningful_overlap:
                    fact_hit = True
                    break

            if fact_hit:
                p["facts"] = p_facts
                relevant[pid] = p

        return list(relevant.values())[:5]  # Cap at top 5 relevant people
