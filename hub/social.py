"""
hub/social.py
--------------
Phase 7C: Social Context and Relationship-Aware Behavior.
Builds bounded social context, guides relationship-aware model behavior,
tracks group conversation participants, and enforces conservative social memory writes.
"""

import logging
import re
import time
from typing import Dict, Any, List, Optional, Tuple

from .database import (
    get_person,
    get_person_facts,
    add_person_fact,
    update_person,
    list_people
)

log = logging.getLogger("cortex.social")

# Temporary moods and jokes that MUST NOT be persisted to permanent memory
TEMPORARY_MOOD_PATTERNS = [
    r"\b(tired|sleepy|bored|grumpy|cranky|hangry|annoyed|mad|sad|stressed|exhausted|angry|upset|irritable)\s+(today|right now|at the moment|this morning|tonight)\b",
    r"\bfeeling\s+(moody|off|lazy|weird|down|cranky|grumpy|tired|upset)\b",
    r"\bin a (bad|good|terrible|weird|foul) mood\b",
    r"\b(having a bad day|rough day today)\b",
    r"\b(temporary\s+mood|bad\s+mood|foul\s+mood)\b"
]

SARCASM_JOKE_PATTERNS = [
    r"\b(just kidding|jk|haha|lol|rofl|lmao|sarcasm|not really|kidding)\b",
    r"\bi('m| am) totally joking\b"
]

SPECULATIVE_JUDGMENT_PATTERNS = [
    r"\b(seems like a narcissist|bad vibes|toxic person|passive aggressive|has an ego)\b",
    r"\bprobably (hates me|lying|crazy)\b"
]


def is_valid_social_fact(statement: str, key: str = "", value: str = "") -> Tuple[bool, str]:
    """
    Conservative memory write policy.
    Permits durable facts, explicit preferences, and relationships.
    Rejects temporary moods, jokes/sarcasm, and speculative personality judgments.
    """
    text = f"{statement} {key} {value}".lower().strip()

    # Check for temporary moods
    for pat in TEMPORARY_MOOD_PATTERNS:
        if re.search(pat, text):
            return False, "Rejected: temporary emotional states or daily moods are not saved to permanent memory."

    # Check for sarcasm or jokes
    for pat in SARCASM_JOKE_PATTERNS:
        if re.search(pat, text):
            return False, "Rejected: sarcasm or jokes are not saved as permanent facts."

    # Check for speculative judgments
    for pat in SPECULATIVE_JUDGMENT_PATTERNS:
        if re.search(pat, text):
            return False, "Rejected: speculative personality judgments are not saved to permanent memory."

    # Must have minimum substance
    if len(text.strip()) < 3:
        return False, "Rejected: fact is too short or empty."

    return True, "Valid social fact."


def get_tone_guidance(relationship_type: Optional[str], is_primary_user: bool = False) -> str:
    """
    Relationship-aware tone guidance.
    Provides generic behavioral tendencies rather than rigid template strings.
    """
    if is_primary_user:
        return "direct, helpful, collaborative"

    rel = (relationship_type or "").lower().strip()
    if rel in ("friend", "best friend", "buddy", "roommate"):
        return "warm, familiar, casual"
    elif rel in ("brother", "sister", "sibling", "mother", "father", "mom", "dad", "family", "cousin"):
        return "familiar, warm, respectful"
    elif rel in ("manager", "boss", "colleague", "coworker", "professor", "teacher", "client"):
        return "professional, polite, concise"
    else:
        return "neutral, polite, objective"


class SocialContextManager:
    """
    Assembles social context for group and relationship-aware interactions.
    """

    @staticmethod
    async def build_social_context(
        device_id: str,
        request: str,
        current_speaker: dict,
        session_refs: dict,
        turns: List[dict],
        relevant_people: List[dict]
    ) -> dict:
        """
        Constructs bounded social context:
        - recent_speakers: active speakers in the session
        - relationship_graph_hints: participant relationships to primary user
        - relevant_shared_facts: shared constraints, preferences, or context
        - active_participants: list of names/labels of participants in the conversation
        - tone_guidance: behavioral tone guidance based on current speaker
        """
        recent_speakers = session_refs.get("recent_speakers", [])
        
        # Build relationship graph hints
        graph_hints: Dict[str, str] = {}
        for spk in recent_speakers:
            name = spk.get("display_name") or spk.get("label")
            rel = spk.get("relationship_type")
            if name and rel:
                graph_hints[name] = rel
            elif name and spk.get("is_primary_user"):
                graph_hints[name] = "primary user"

        for p in relevant_people:
            name = p.get("display_name")
            rel = p.get("relationship_type")
            if name and rel and name not in graph_hints:
                graph_hints[name] = rel

        # Active participants from recent turns
        active_participants = set()
        for turn in turns[-5:]:
            name = turn.get("person_name") or turn.get("speaker_label")
            if name:
                active_participants.add(name)
        if current_speaker.get("display_name"):
            active_participants.add(current_speaker["display_name"])
        elif current_speaker.get("label"):
            active_participants.add(current_speaker["label"])

        # Extract relevant shared facts from present people and recent turns
        shared_facts = []
        for p in relevant_people:
            p_name = p.get("display_name", "Unknown")
            for f in p.get("facts", []):
                # Include preferences, dislikes, dietary, communication, context
                k = f.get("key", "").lower()
                v = f.get("value", "")
                if any(kw in k or kw in v.lower() for kw in ("preference", "dislike", "like", "food", "eat", "diet", "communication", "context", "work")):
                    shared_facts.append({
                        "person": p_name,
                        "key": f.get("key"),
                        "value": f.get("value"),
                        "relationship": p.get("relationship_type", "")
                    })

        # Also inspect recent turns for explicit constraints or stated preferences (e.g. "I don't like sushi")
        for turn in turns[-4:]:
            t_req = turn.get("request", "")
            t_spk = turn.get("person_name") or turn.get("speaker_label", "Speaker")
            m_dislike = re.search(r"\b(?:i don't like|i dislike|i hate|no|allergic to)\s+([a-zA-Z\s]+)", t_req, re.IGNORECASE)
            if m_dislike:
                disliked_item = m_dislike.group(1).strip().rstrip(".,!")
                shared_facts.append({
                    "person": t_spk,
                    "key": "dislike",
                    "value": f"dislikes {disliked_item}",
                    "relationship": turn.get("relationship_type", "")
                })

        tone = get_tone_guidance(
            relationship_type=current_speaker.get("relationship_type"),
            is_primary_user=current_speaker.get("is_primary_user", False)
        )

        return {
            "active_participants": list(active_participants),
            "recent_speakers": recent_speakers,
            "relationship_graph_hints": graph_hints,
            "relevant_shared_facts": shared_facts[:8],  # Bounded to top 8 facts
            "tone_guidance": tone
        }

    @staticmethod
    async def remember_social_interaction(
        target_person_id: Optional[str],
        key: str,
        value: str,
        source: str = "conversation"
    ) -> dict:
        """
        Record a social fact or interaction note under the conservative policy.
        """
        is_valid, reason = is_valid_social_fact(f"{key}: {value}", key, value)
        if not is_valid:
            log.info(f"[SocialMemory] Rejected social write: {reason}")
            return {"status": "rejected", "reason": reason}

        if target_person_id:
            person = await get_person(target_person_id)
            if not person:
                return {"status": "error", "message": f"Person {target_person_id} not found."}

            fact_id = await add_person_fact({
                "person_id": target_person_id,
                "key": key,
                "value": value,
                "confidence": 0.95,
                "source": source
            })
            # Update last interaction
            await update_person(target_person_id, {"last_interaction_at": time.time()})
            return {
                "status": "success",
                "fact_id": fact_id,
                "person_id": target_person_id,
                "key": key,
                "value": value
            }

        return {"status": "error", "message": "target_person_id is required."}
