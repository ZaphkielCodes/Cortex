"""
hub/speakers.py
---------------
Phase 7B: Speaker Identity and Multi-Speaker Context Management.
Handles speaker profiles, resolution, linking with Phase 7A People,
and multi-speaker session context.
"""

import logging
import uuid
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from .database import (
    create_speaker_profile,
    get_speaker_profile,
    get_speaker_profile_by_label,
    get_speaker_profile_by_person_id,
    get_primary_speaker_profile,
    list_speaker_profiles,
    update_speaker_profile,
    delete_speaker_profile,
    get_person,
    update_person
)

log = logging.getLogger("cortex.speakers")


class SpeakerManager:
    """
    Manages speaker identity, resolution, and person linking for Cortex.
    """

    @staticmethod
    async def ensure_primary_speaker() -> dict:
        """
        Ensures a primary user speaker profile exists in the database.
        Returns the primary user speaker profile.
        """
        primary = await get_primary_speaker_profile()
        if primary:
            return primary

        now = datetime.now(timezone.utc).isoformat()
        primary_data = {
            "speaker_id": "spk_primary",
            "person_id": None,
            "label": "Primary User",
            "is_primary_user": True,
            "confidence": 1.0,
            "metadata": {"source": "default_primary_enrollment"},
            "created_at": now,
            "updated_at": now
        }
        return await create_speaker_profile(primary_data)

    @staticmethod
    async def create_speaker(
        label: str,
        is_primary: bool = False,
        person_id: Optional[str] = None,
        confidence: float = 1.0,
        metadata: Optional[dict] = None,
        speaker_id: Optional[str] = None
    ) -> dict:
        """Create a new speaker profile."""
        now = datetime.now(timezone.utc).isoformat()
        data = {
            "speaker_id": speaker_id or f"spk_{uuid.uuid4().hex[:8]}",
            "person_id": person_id,
            "label": label,
            "is_primary_user": is_primary,
            "confidence": confidence,
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now
        }
        return await create_speaker_profile(data)

    @staticmethod
    async def get_speaker(speaker_id: str) -> Optional[dict]:
        """Get speaker profile by ID."""
        return await get_speaker_profile(speaker_id)

    @staticmethod
    async def get_speaker_by_label(label: str) -> Optional[dict]:
        """Get speaker profile by label."""
        return await get_speaker_profile_by_label(label)

    @staticmethod
    async def list_speakers() -> List[dict]:
        """List all registered speaker profiles."""
        return await list_speaker_profiles()

    @staticmethod
    async def link_person(speaker_id: str, person_id: Optional[str]) -> Optional[dict]:
        """
        Link or unlink a speaker profile to a Phase 7A Person record.
        """
        speaker = await get_speaker_profile(speaker_id)
        if not speaker:
            return None

        if person_id:
            person = await get_person(person_id)
            if not person:
                from .people import PeopleManager
                p_res = await PeopleManager.resolve_person(person_id)
                if p_res.matched and p_res.person:
                    person_id = p_res.person["person_id"]
                else:
                    log.warning(f"Cannot link speaker {speaker_id}: person {person_id} not found")
                    return None

        updated = await update_speaker_profile(speaker_id, {"person_id": person_id})
        return updated

    @staticmethod
    async def resolve_speaker(
        speaker_hint: Optional[str] = None,
        session_refs: Optional[dict] = None,
        audio_metadata: Optional[dict] = None
    ) -> dict:
        """
        Resolve speaker identity from hint, audio metadata, or session context.
        Supports:
        - Primary user
        - Known speaker linked to Person
        - Unknown speaker
        - Ambiguity detection (prevents auto-merging)
        """
        if not speaker_hint and audio_metadata:
            speaker_hint = audio_metadata.get("speaker_id") or audio_metadata.get("speaker_label") or audio_metadata.get("speaker")

        hint_clean = (speaker_hint or "").strip()
        hint_lower = hint_clean.lower()

        # 1. Primary user resolution
        if not hint_clean or hint_lower in ("primary", "me", "primary user", "user", "owner", "admin"):
            primary = await SpeakerManager.ensure_primary_speaker()
            result = dict(primary)
            if primary.get("person_id"):
                person = await get_person(primary["person_id"])
                if person:
                    result["display_name"] = person["display_name"]
                    result["relationship_type"] = person.get("relationship_type", "")
            return result

        # 2. Direct speaker profile ID match
        spk_by_id = await get_speaker_profile(hint_clean)
        if spk_by_id:
            result = dict(spk_by_id)
            if spk_by_id.get("person_id"):
                person = await get_person(spk_by_id["person_id"])
                if person:
                    result["display_name"] = person["display_name"]
                    result["relationship_type"] = person.get("relationship_type", "")
                    # Touch last interaction on Person
                    await update_person(person["person_id"], {"last_interaction_at": datetime.now(timezone.utc).isoformat()})
            return result

        # 3. Direct speaker profile label match
        spk_by_label = await get_speaker_profile_by_label(hint_clean)
        if spk_by_label:
            result = dict(spk_by_label)
            if spk_by_label.get("person_id"):
                person = await get_person(spk_by_label["person_id"])
                if person:
                    result["display_name"] = person["display_name"]
                    result["relationship_type"] = person.get("relationship_type", "")
                    await update_person(person["person_id"], {"last_interaction_at": datetime.now(timezone.utc).isoformat()})
            return result

        # 4. Unknown speaker explicit flag
        if hint_lower.startswith("unknown") or "unrecognized" in hint_lower:
            # Deterministic/bounded unknown profile
            unknown_id = f"spk_unknown_{uuid.uuid4().hex[:6]}"
            return {
                "speaker_id": unknown_id,
                "label": hint_clean if hint_clean.lower() != "unknown" else "Unknown Speaker",
                "is_primary_user": False,
                "confidence": 0.5,
                "person_id": None,
                "display_name": None,
                "relationship_type": None,
                "is_unknown": True,
                "metadata": audio_metadata or {}
            }

        # 5. Check Phase 7A Person resolution (e.g. speaker hint is "Alex" or "brother")
        from .people import PeopleManager
        p_res = await PeopleManager.resolve_person(hint_clean, session_refs)

        if p_res.is_ambiguous:
            # Ambiguity detected! Do NOT auto-merge or arbitrarily guess!
            log.info(f"[SpeakerManager] Ambiguous speaker reference for '{hint_clean}'. Candidates: {[c['display_name'] for c in p_res.candidates]}")
            return {
                "speaker_id": f"spk_ambiguous_{uuid.uuid4().hex[:6]}",
                "label": hint_clean,
                "is_primary_user": False,
                "confidence": 0.4,
                "person_id": None,
                "is_ambiguous": True,
                "candidates": p_res.candidates,
                "metadata": {"ambiguous_hint": hint_clean}
            }

        if p_res.matched and p_res.person:
            person = p_res.person
            p_id = person["person_id"]
            # Check if speaker profile already exists for this person
            existing_spk = await get_speaker_profile_by_person_id(p_id)
            if not existing_spk:
                # Create a persistent speaker profile linked to this person
                existing_spk = await SpeakerManager.create_speaker(
                    label=person["display_name"],
                    is_primary=False,
                    person_id=p_id,
                    confidence=0.95,
                    metadata={"auto_enrolled_from_person": True}
                )
            result = dict(existing_spk)
            result["display_name"] = person["display_name"]
            result["relationship_type"] = person.get("relationship_type", "")
            # Update last interaction on person
            await update_person(p_id, {"last_interaction_at": datetime.now(timezone.utc).isoformat()})
            return result

        # 6. Default fallback for generic custom labels (e.g. "Speaker A", "Speaker B", "Guest")
        new_spk = await SpeakerManager.create_speaker(
            label=hint_clean,
            is_primary=False,
            person_id=None,
            confidence=0.8,
            metadata=audio_metadata or {}
        )
        return dict(new_spk)

    @staticmethod
    async def process_audio_turn(
        device_id: str,
        text: str,
        speaker_hint: Optional[str] = None,
        audio_data: Optional[bytes] = None,
        audio_metadata: Optional[dict] = None
    ) -> dict:
        """
        Processes an incoming audio turn:
        audio -> speaker segment/label -> speaker resolver -> person lookup -> tagged turn data.
        """
        speaker_info = await SpeakerManager.resolve_speaker(
            speaker_hint=speaker_hint,
            audio_metadata=audio_metadata
        )

        turn_data = {
            "turn_id": uuid.uuid4().hex,
            "device_id": device_id,
            "speaker_id": speaker_info.get("speaker_id"),
            "speaker_label": speaker_info.get("label", "Unknown Speaker"),
            "is_primary_user": speaker_info.get("is_primary_user", False),
            "person_id": speaker_info.get("person_id"),
            "display_name": speaker_info.get("display_name"),
            "relationship_type": speaker_info.get("relationship_type"),
            "text": text,
            "confidence": speaker_info.get("confidence", 1.0),
            "timestamp": time.time()
        }
        return turn_data

    @staticmethod
    async def get_active_speaker_context(device_id: str, session_refs: Optional[dict] = None) -> dict:
        """
        Builds the speaker dictionary for active context injection.
        """
        refs = session_refs or {}
        curr_spk_id = refs.get("current_speaker_id")
        spk = None
        if curr_spk_id:
            spk = await get_speaker_profile(curr_spk_id)

        if not spk:
            spk = await SpeakerManager.ensure_primary_speaker()

        ctx = dict(spk)
        if spk.get("person_id"):
            person = await get_person(spk["person_id"])
            if person:
                ctx["display_name"] = person["display_name"]
                ctx["relationship_type"] = person.get("relationship_type", "")
                ctx["notes"] = person.get("notes", "")

        ctx["recent_speakers"] = refs.get("recent_speakers", [])
        ctx["last_speaker_change"] = refs.get("last_speaker_change")
        return ctx

    @staticmethod
    async def get_current_speaker(device_id: str) -> dict:
        """
        Get the current active speaker for a device session.
        """
        from .session_context import SessionContextManager
        session = await SessionContextManager.get_session(device_id)
        refs = session.get("references", {})
        curr_id = refs.get("current_speaker_id")
        if curr_id:
            spk = await get_speaker_profile(curr_id)
            if spk:
                res = dict(spk)
                if spk.get("person_id"):
                    person = await get_person(spk["person_id"])
                    if person:
                        res["display_name"] = person["display_name"]
                        res["relationship_type"] = person.get("relationship_type", "")
                return res
        return await SpeakerManager.ensure_primary_speaker()

    @staticmethod
    async def list_recent_speakers(device_id: str) -> List[dict]:
        """
        List recent speakers active in the device session.
        """
        from .session_context import SessionContextManager
        session = await SessionContextManager.get_session(device_id)
        return session.get("references", {}).get("recent_speakers", [])
