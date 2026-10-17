"""
hub/target_resolver.py
----------------------
Structured entity and target resolution layer for Cortex (Phase 9G).

Responsibilities:
  1. Target / Entity Resolution:
     Extract action, target_device, target_kind, target_name, location, qualifiers.
  2. Device vs Object Disambiguation:
     A noun phrase (e.g. 'Once Human') must NEVER become a device target merely
     because no immediate match exists.
     Device resolution is strictly grounded in:
       - Enrolled device IDs
       - Known device aliases
       - Current session / active device
       - Explicit device wording
     Phrases like 'my desktop', 'this computer', 'here' resolve to the active/local device.
  3. Bounded Filesystem Matching:
     Matches target_name against direct children returned by fs.list.
     Uses normalized stem, extension-insensitive match, shortcut/display name,
     and conservative fuzzy similarity.
     Enforces ambiguity guard: if multiple materially plausible matches exist,
     stops and requests clarification.
"""

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .environment import get_known_folder, resolve_location_path

log = logging.getLogger("cortex.target_resolver")

# Words indicating current device
CURRENT_DEVICE_KEYWORDS = {
    "this device",
    "this computer",
    "this laptop",
    "this machine",
    "here",
    "local",
    "current",
    "my laptop",
    "my computer",
    "my pc",
    "my desktop",  # When used in device context
}

KNOWN_LOCATIONS = {
    "desktop": "desktop",
    "my desktop": "desktop",
    "downloads": "downloads",
    "my downloads": "downloads",
    "downloads folder": "downloads",
    "documents": "documents",
    "my documents": "documents",
    "documents folder": "documents",
    "pictures": "pictures",
    "my pictures": "pictures",
    "pictures folder": "pictures",
}


@dataclass
class ResolvedTarget:
    """Structured resolution output for user requests."""
    action: str                                       # "delete", "list", "count", "move", "open", etc.
    target_device: str                                # "current", "asus", or enrolled device ID
    target_kind: str                                  # "filesystem_item", "device", "application", "unknown"
    target_name: Optional[str] = None                 # e.g. "Once Human", "Project Alpha"
    location: Optional[str] = None                    # e.g. "desktop", "downloads"
    qualifiers: List[str] = field(default_factory=list) # e.g. ["shortcut", "game"]
    resolved_path: Optional[str] = None               # Canonical path if resolved
    owning_device: Optional[str] = None               # Device owning target/resource
    is_ambiguous: bool = False
    clarification_reason: Optional[str] = None

    @property
    def resolved_entity(self) -> Optional[str]:
        return self.target_name or self.resolved_path

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "target_device": self.target_device,
            "target_kind": self.target_kind,
            "target_name": self.target_name,
            "resolved_entity": self.resolved_entity,
            "location": self.location,
            "qualifiers": self.qualifiers,
            "resolved_path": self.resolved_path,
            "owning_device": self.owning_device,
            "is_ambiguous": self.is_ambiguous,
            "clarification_reason": self.clarification_reason,
        }


@dataclass
class MatchResult:
    """Result of filesystem item matching."""
    matched_item: Optional[Dict[str, Any]] = None
    is_ambiguous: bool = False
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    clarification_message: Optional[str] = None


class TargetResolver:
    """
    Structured resolution layer for grounding local requests.
    Enforces strict device vs object separation.
    """

    @classmethod
    def ground_reference(
        cls,
        target_reference: Optional[str] = None,
        operation: Optional[str] = None,
        session_context: Optional[Dict[str, Any]] = None,
        world_state: Optional[Any] = None,
        current_device: str = "asus",
        enrolled_devices: Optional[List[Dict[str, Any]]] = None,
        raw_request: Optional[str] = None,
    ) -> ResolvedTarget:
        """
        Ground a target reference after semantic interpretation.
        Decoupled from natural-language intent classification.
        Grounds references like 'previous_app', 'this app', 'that file',
        'my other laptop', 'the window I was using' against session context,
        world state, device registry, and file locality.
        """
        ref = (target_reference or "").strip()
        ref_lower = ref.lower()

        # 1. Previous App / Previous Window reference
        if ref_lower in (
            "previous_app", "previous_window", "previous", "last_app",
            "last_window", "last app", "previous app", "previous window",
            "the app i was on", "the window i was using", "the previous window",
            "the previous app",
        ):
            from .world_state import WorldStateManager
            prev_fg = WorldStateManager.get_instance().get_previous_foreground()
            if prev_fg and prev_fg.get("app_name"):
                return ResolvedTarget(
                    action=operation or "window.focus",
                    target_device=prev_fg.get("device_id") or current_device,
                    target_kind="window" if (operation and "window" in operation) else "application",
                    target_name=prev_fg.get("app_name"),
                    qualifiers=["previous"],
                )
            if session_context:
                refs = session_context.get("references", {})
                last_app = refs.get("last_app") or refs.get("last_confirmed_app")
                if last_app:
                    return ResolvedTarget(
                        action=operation or "window.focus",
                        target_device=current_device,
                        target_kind="application",
                        target_name=last_app,
                        qualifiers=["previous"],
                    )
            return ResolvedTarget(
                action=operation or "window.focus",
                target_device=current_device,
                target_kind="application",
                target_name=None,
                is_ambiguous=True,
                clarification_reason="No previous window or application recorded in foreground history.",
            )

        # 2. Current App / This App / Foreground reference
        if ref_lower in (
            "current_app", "this_app", "this app", "current window",
            "the app i'm using", "the app that i am using", "active_window", "foreground",
        ):
            fg_name = None
            if world_state and isinstance(world_state, dict):
                fg_name = world_state.get("foreground_app") or world_state.get("foreground_activity", {}).get("app_name")
            elif world_state and hasattr(world_state, "foreground_activity"):
                fg_name = getattr(world_state, "foreground_activity", {}).get("app_name")
            if not fg_name:
                from .world_state import WorldStateManager
                wsm = WorldStateManager.get_instance()
                fg_hist = wsm.get_foreground_history(limit=1)
                if fg_hist and fg_hist[-1].get("app_name"):
                    fg_name = fg_hist[-1]["app_name"]
                if not fg_name:
                    sig = wsm.get_signal(f"device.foreground:{current_device}")
                    if sig and sig.payload.get("app_name") and sig.payload.get("app_name") != "unknown":
                        fg_name = sig.payload["app_name"]
            if fg_name and fg_name != "unknown":
                return ResolvedTarget(
                    action=operation or "app.close",
                    target_device=current_device,
                    target_kind="application",
                    target_name=fg_name,
                    qualifiers=["current"],
                )
            if session_context:
                refs = session_context.get("references", {})
                fg_name = refs.get("last_confirmed_app") or refs.get("foreground_app")
                if fg_name:
                    return ResolvedTarget(
                        action=operation or "app.close",
                        target_device=current_device,
                        target_kind="application",
                        target_name=fg_name,
                        qualifiers=["current"],
                    )
            return ResolvedTarget(
                action=operation or "app.close",
                target_device=current_device,
                target_kind="application",
                target_name=None,
                is_ambiguous=True,
                clarification_reason="No active foreground application detected.",
            )

        # 3. Explicit named target or fallback to full resolution
        synthesized_text = f"{operation or 'action'} {ref}" if operation else ref
        if raw_request:
            synthesized_text = raw_request
        return cls.resolve(
            user_request=synthesized_text,
            current_device=current_device,
            enrolled_devices=enrolled_devices,
            session_context=session_context,
            world_state=world_state if isinstance(world_state, dict) else (world_state.to_dict() if hasattr(world_state, "to_dict") else None),
        )

    @staticmethod
    def resolve(
        user_request: str,
        current_device: str = "asus",
        enrolled_devices: Optional[List[Dict[str, Any]]] = None,
        base_dir: Optional[Path] = None,
        session_context: Optional[Dict[str, Any]] = None,
        world_state: Optional[Dict[str, Any]] = None,
    ) -> ResolvedTarget:
        """
        Convert a user request into a validated, structured ResolvedTarget.
        Never treats object noun phrases as devices.
        Implements strict grounding precedence:
          explicit target in current utterance
          > semantic current-foreground reference (fresh world/screen observation)
          > most recent confirmed interaction target
          > session historical target
          > ask clarification
        """
        text = user_request.strip()
        lower = text.lower()

        # Enrolled device vocabulary
        if enrolled_devices is None:
            try:
                from .executor_registry import ExecutorRegistry
                er = ExecutorRegistry.get_instance()
                enrolled_devices = [
                    {"device_id": h.device_id, "device_name": h.display_name}
                    for h in er.list_device_handles()
                ]
            except Exception:
                enrolled_devices = []

        enrolled_ids = set()
        enrolled_names = set()
        if enrolled_devices:
            for d in enrolled_devices:
                dev_id = d.get("device_id", "").lower()
                dev_name = (d.get("device_name") or "").lower()
                if dev_id:
                    enrolled_ids.add(dev_id)
                if dev_name:
                    enrolled_names.add(dev_name)

        # -------------------------------------------------------------------
        # 1. Action Identification
        # -------------------------------------------------------------------
        action = "unknown"
        if any(w in lower for w in ("clean up", "cleanup", "clean my", "clean the", "organize my", "organize the")) or ("keep " in lower and ("remove " in lower or "delete " in lower)):
            action = "cleanup"
        elif any(w in lower for w in ("delete", "remove", "erase", "trash", "destroy")):
            action = "delete"
        elif any(w in lower for w in ("how many files", "count files", "count the files", "count items")):
            action = "count"
        elif any(w in lower for w in ("list the files", "list files", "show files", "show the files", "list")):
            action = "list"
        elif any(w in lower for w in ("rename",)):
            action = "rename"
        elif any(w in lower for w in ("move", "relocate", "transfer")):
            action = "move"
        elif any(w in lower for w in ("copy", "duplicate")):
            action = "copy"
        elif any(w in lower for w in ("close", "exit", "quit", "terminate", "kill")):
            action = "close"
        elif any(w in lower for w in ("switch to", "switch back to", "switch back", "focus on", "focus the", "focus", "bring up", "bring to front")):
            action = "focus"
        elif "minimize" in lower:
            action = "minimize"
        elif "maximize" in lower:
            action = "maximize"
        elif "restore" in lower:
            action = "restore"
        elif any(w in lower for w in ("open", "launch", "start", "run")):
            action = "open"
        elif any(w in lower for w in ("stat", "info on", "inspect")):
            action = "stat"

        # -------------------------------------------------------------------
        # 2. Location Identification
        # -------------------------------------------------------------------
        location: Optional[str] = None
        for loc_key, canonical_loc in KNOWN_LOCATIONS.items():
            pattern = r"\b(?:in|from|on|of)?\s*(?:my\s+|the\s+)?" + re.escape(loc_key) + r"\b"
            if re.search(pattern, lower):
                location = canonical_loc
                break

        # -------------------------------------------------------------------
        # 3. Qualifiers Identification
        # -------------------------------------------------------------------
        qualifiers: List[str] = []
        if "shortcut" in lower or ".lnk" in lower:
            qualifiers.append("shortcut")
        if "game" in lower:
            qualifiers.append("game")
        if "folder" in lower or "directory" in lower:
            qualifiers.append("folder")
        if "file" in lower:
            qualifiers.append("file")
        if "pdf" in lower or ".pdf" in lower:
            qualifiers.append("pdf")
        if "window" in lower:
            qualifiers.append("window")
        if "app" in lower or "application" in lower:
            qualifiers.append("app")

        # -------------------------------------------------------------------
        # 4. Target Name & Target Kind Extraction
        # -------------------------------------------------------------------
        target_name: Optional[str] = None
        target_kind = "unknown"

        if action == "cleanup":
            location = location or "desktop"
            target_kind = "collection"
            target_name = location
        elif action in ("list", "count") and location:
            target_kind = "filesystem_item"
            target_name = None
        elif action in ("close", "focus", "minimize", "maximize", "restore"):
            # Patterns like "close Discord", "close the Discord app", "close it", "switch to Chrome", "focus the terminal"
            m_win_act = re.search(
                r"(?:close|exit|quit|terminate|kill|switch\s+to|switch\s+back\s+to|switch\s+back|focus(?:\s+on)?|bring\s+up|minimize|maximize|restore)\s+(?:the\s+)?(?:app\s+|application\s+|window\s+)?['\"]?([^'\",\.\n]+?)['\"]?$",
                text,
                re.IGNORECASE,
            )
            if m_win_act:
                target_name = m_win_act.group(1).strip()
                if action == "focus":
                    target_kind = "window"
                else:
                    target_kind = "application"
            elif lower in ("close it", "close that", "switch back to it", "switch to it", "focus it"):
                target_name = "it"
                target_kind = "application" if action == "close" else "window"
        elif action == "copy":
            # Pattern like "copy alpha.txt to Backup"
            m_copy = re.search(r"copy\s+['\"]?([^'\",\n]+?)['\"]?\s+to\s+['\"]?([^'\",\n]+?)['\"]?$", text, re.IGNORECASE)
            if m_copy:
                target_name = m_copy.group(1).strip()
                target_kind = "filesystem_item"
        elif action == "rename":
            # Pattern like "rename the copy to beta.txt" or "rename alpha.txt to beta.txt"
            m_rename = re.search(r"rename\s+['\"]?([^'\",\n]+?)['\"]?\s+to\s+['\"]?([^'\",\n]+?)['\"]?$", text, re.IGNORECASE)
            if m_rename:
                target_name = m_rename.group(1).strip()
                target_kind = "filesystem_item"
        elif action == "move":
            m_move = re.search(r"move\s+['\"]?([^'\",\n]+?)['\"]?\s+to\s+['\"]?([^'\",\n]+?)['\"]?$", text, re.IGNORECASE)
            if m_move:
                target_name = m_move.group(1).strip()
                target_kind = "filesystem_item"
        else:
            # Check for patterns like "the shortcut named <target>"
            m_named = re.search(
                r"(?:shortcut|file|item|game|program|app)?\s*named\s+['\"]?([^'\",\.\n]+?)['\"]?(?:\s+(?:from|on|in|to)\b|$)",
                text,
                re.IGNORECASE,
            )
            if m_named:
                target_name = m_named.group(1).strip()
                target_kind = "filesystem_item"
            else:
                # Check for "delete/remove [the shortcut] <target> from/on <location>"
                m_action_from = re.search(
                    r"(?:delete|remove|erase)\s+(?:the\s+)?(?:shortcut\s+|game\s+|file\s+)?['\"]?([^'\",\.\n]+?)['\"]?\s+(?:from|on|in)\s+(?:my\s+|the\s+)?(?:desktop|downloads|documents|pictures)",
                    text,
                    re.IGNORECASE,
                )
                if m_action_from:
                    target_name = m_action_from.group(1).strip()
                    if target_name.lower().endswith(" game"):
                        target_name = target_name[:-5].strip()
                    if target_name.lower().endswith(" shortcut"):
                        target_name = target_name[:-9].strip()
                    target_kind = "filesystem_item"
                else:
                    # Check for simple "open/launch <target>", "delete <target>", etc.
                    m_simple = re.search(
                        r"(?:open|launch|start|run|delete|remove|erase)\s+(?:the\s+)?(?:shortcut\s+|game\s+|file\s+|app\s+|window\s+)?(?:my\s+)?['\"]?([^'\",\.\n]+?)['\"]?$",
                        text,
                        re.IGNORECASE,
                    )
                    if m_simple:
                        target_name = m_simple.group(1).strip()
                        if any(ext in target_name.lower() for ext in (".pdf", ".txt", ".doc", ".docx", ".png", ".jpg", ".csv", ".json", " review", " document", " file")):
                            target_kind = "filesystem_item"
                        elif action in ("open", "launch", "start", "run"):
                            target_kind = "application"
                        else:
                            target_kind = "filesystem_item"

        if target_name:
            target_name = target_name.strip("?.,! \t\n")

        # -------------------------------------------------------------------
        # 4b. Explicit Precedence Model for Targets & Entity Grounding
        # -------------------------------------------------------------------
        # Precedence:
        # 1. Explicit target in current utterance (e.g. "close Discord", "open Notepad")
        # 2. Semantic current-foreground reference (e.g. "close the app I'm using", "close this app", "close current app")
        #    -> Resolved strictly from fresh foreground-app / world-state observation!
        # 3. Most recent confirmed interaction target (e.g. "What app am I using?" -> "Discord" -> "close it" -> Discord)
        # 4. Session historical target (e.g. last_app, last_window)
        # 5. Clarification needed
        # -------------------------------------------------------------------
        refs = {}
        grounded = None
        if session_context:
            refs = session_context.get("references", {})
            grounded = (
                refs.get("grounded_target")
                or session_context.get("grounded_target")
            )

        # Discover live/fresh foreground app from world_state or session
        live_foreground_app = None
        if world_state and isinstance(world_state, dict):
            live_foreground_app = world_state.get("foreground_app") or world_state.get("foreground_activity", {}).get("app_name")
        if not live_foreground_app and refs:
            live_foreground_app = refs.get("foreground_app") or refs.get("current_app")
        if not live_foreground_app and session_context and "world_state" in session_context:
            ws_ctx = session_context["world_state"]
            if isinstance(ws_ctx, dict):
                live_foreground_app = ws_ctx.get("foreground_app") or ws_ctx.get("foreground_activity", {}).get("app_name")
        if not live_foreground_app:
            try:
                from .world_state import WorldStateManager
                sig = WorldStateManager.get_instance().get_signal(f"device.foreground:{current_device}")
                if sig and sig.payload.get("app_name") and sig.payload.get("app_name") != "unknown":
                    live_foreground_app = sig.payload["app_name"]
            except Exception:
                pass
        if live_foreground_app == "unknown":
            live_foreground_app = None

        # Check for semantic foreground references in text or target_name
        SEMANTIC_FG_PATTERNS = [
            r"\b(?:the\s+)?(?:app|application|window|program)?\s*(?:that\s+)?i(?:'m|\s+am)\s+(?:currently\s+)?(?:using|in|on)(?:\s+right\s+now)?\b",
            r"\b(?:current|active|this|foreground)\s+(?:app|application|window|program)\b",
            r"\bthe\s+app\s+currently\s+open\b",
            r"\bwhat\s+i(?:'m|\s+am)\s+(?:using|on)\b",
            r"\b(?:the\s+)?app\s+i(?:\'m|\s+am)\s+using\b",
            r"\bthe\s+app\s+that\s+i(?:\'m|\s+am)\s+using\b",
        ]
        is_semantic_fg_ref = False
        for pat in SEMANTIC_FG_PATTERNS:
            if re.search(pat, lower):
                is_semantic_fg_ref = True
                break

        if target_name:
            t_clean = target_name.lower().strip(" \t.,!?;:'\"")
            if t_clean in (
                "that i'm using right now", "that i am using right now", "that i'm using", "that i am using",
                "i'm using right now", "i am using right now", "i'm using", "i am using",
                "the app i'm using", "the app that i'm using", "current app", "the current app", "this app",
                "the app i'm on", "that i'm on", "what i'm using", "the app in the foreground",
                "the current window", "this window", "the window i'm using"
            ) or any(re.search(pat, t_clean) for pat in SEMANTIC_FG_PATTERNS):
                is_semantic_fg_ref = True

        pronouns = {"it", "that", "this", "the item", "the shortcut", "the file", "the game", "the folder", "the copy", "the app", "the window"}

        if action in ("close", "focus", "minimize", "maximize", "restore"):
            # Precedence 2: Semantic current-foreground reference (resolve strictly from fresh observation)
            if is_semantic_fg_ref:
                if live_foreground_app:
                    target_name = live_foreground_app
                    target_kind = "application"
                else:
                    return ResolvedTarget(
                        action=action,
                        target_device=current_device,
                        target_kind="application",
                        target_name=None,
                        is_ambiguous=True,
                        clarification_reason=f"Which application would you like me to {action}?",
                    )
            # Precedence 3 / 4: Pronoun resolution ("it", "that app", etc.)
            elif target_name and target_name.lower() in ("it", "that", "this", "the app", "the window", "that app"):
                # Precedence 3: Most recent confirmed interaction target (e.g. from "What app am I currently using?")
                confirmed_app = refs.get("last_confirmed_app")
                if confirmed_app:
                    target_name = confirmed_app
                    target_kind = "application"
                # Fresh foreground app if active
                elif live_foreground_app:
                    target_name = live_foreground_app
                    target_kind = "application"
                # Precedence 4: Historical target ONLY if no newer confirmed app contradicts it
                elif refs.get("last_app"):
                    target_name = refs["last_app"]
                    target_kind = "application"
                elif refs.get("last_window"):
                    target_name = refs["last_window"]
                    target_kind = "window"
                elif grounded and grounded.get("target_kind") == "application":
                    target_name = grounded.get("target_name")
                    target_kind = "application"
                else:
                    return ResolvedTarget(
                        action=action,
                        target_device=current_device,
                        target_kind="application",
                        target_name=None,
                        is_ambiguous=True,
                        clarification_reason=f"Which application would you like me to {action}?",
                    )
            elif not target_name:
                if action == "focus":
                    from .world_state import WorldStateManager
                    prev_fg = WorldStateManager.get_instance().get_previous_foreground()
                    if prev_fg and prev_fg.get("app_name"):
                        target_name = prev_fg["app_name"]
                        target_kind = "application"
                    elif refs.get("last_app"):
                        target_name = refs["last_app"]
                        target_kind = "application"
                    elif live_foreground_app:
                        target_name = live_foreground_app
                        target_kind = "application"
                    else:
                        return ResolvedTarget(
                            action=action,
                            target_device=current_device,
                            target_kind="application",
                            target_name=None,
                            is_ambiguous=True,
                            clarification_reason=f"Which application would you like me to {action}?",
                        )
                elif is_semantic_fg_ref and live_foreground_app:
                    target_name = live_foreground_app
                    target_kind = "application"
                elif live_foreground_app:
                    target_name = live_foreground_app
                    target_kind = "application"
                elif refs.get("last_confirmed_app"):
                    target_name = refs["last_confirmed_app"]
                    target_kind = "application"
                elif refs.get("last_app"):
                    target_name = refs["last_app"]
                    target_kind = "application"
                else:
                    return ResolvedTarget(
                        action=action,
                        target_device=current_device,
                        target_kind="application",
                        target_name=None,
                        is_ambiguous=True,
                        clarification_reason=f"Which application would you like me to {action}?",
                    )
        elif target_name and target_name.lower() in pronouns:
            if target_name.lower() == "the copy":
                last_copy = refs.get("last_copied_dst") or refs.get("last_copied_file") or refs.get("last_file")
                if last_copy:
                    from pathlib import Path
                    target_name = Path(last_copy).name
                    target_kind = "filesystem_item"
            elif grounded:
                target_name = grounded.get("target_name") or target_name
                target_kind = grounded.get("target_kind", "filesystem_item")
                if not location:
                    location = grounded.get("location") or "desktop"
                qualifiers = qualifiers or grounded.get("qualifiers", [])
            elif refs.get("last_file"):
                from pathlib import Path
                target_name = Path(refs["last_file"]).name
                target_kind = "filesystem_item"
        elif not target_name and action in ("delete",) and grounded:
            target_name = grounded.get("target_name")
            target_kind = grounded.get("target_kind", "filesystem_item")
            if not location and target_kind == "filesystem_item":
                location = grounded.get("location") or "desktop"
            qualifiers = qualifiers or grounded.get("qualifiers", [])

        # -------------------------------------------------------------------
        # 5. Device vs Object Disambiguation (STRONG RULE)
        # -------------------------------------------------------------------
        # A noun phrase MUST NOT become a device target merely because no immediate match exists.
        # Check if the user explicitly targeted an enrolled remote device (e.g. "on tv", "on lenovo")
        target_device = current_device
        is_remote_device_explicit = False

        # First check explicit prepositional destination: "on <device>" or "in <device>"
        m_on = re.search(r"\b(?:on|in)\s+([a-zA-Z0-9_-]+)$", text, re.IGNORECASE)
        if m_on:
            cand = m_on.group(1).strip().lower()
            if cand in enrolled_ids or cand in enrolled_names:
                target_device = cand
                is_remote_device_explicit = True
                if target_name:
                    m_strip = re.search(r"^(.*?)\s+(?:on|in)\s+" + re.escape(cand) + r"$", target_name, re.IGNORECASE)
                    if m_strip:
                        target_name = m_strip.group(1).strip()

        if not is_remote_device_explicit:
            for word in lower.split():
                clean_word = word.strip(".,;:?!'\"")
                if clean_word in enrolled_ids or clean_word in enrolled_names:
                    # User mentioned an explicit enrolled device
                    # Ensure it's not simply our current device or an object
                    target_device = clean_word
                    is_remote_device_explicit = True
                    break

        # If user said "my desktop", "this laptop", "here", target_device is current device
        if not is_remote_device_explicit:
            target_device = current_device

        # CRITICAL ASSERTION:
        # If target_name is extracted, it must NEVER be assigned to target_device
        if target_name and target_device.lower() == target_name.lower():
            target_device = current_device

        # Common application names must never be treated as remote devices
        APP_DEVICE_NAMES = {"discord", "chrome", "spotify", "notepad", "terminal", "calc", "firefox", "edge", "code", "vscode", "explorer", "steam"}
        if target_device.lower() in APP_DEVICE_NAMES:
            target_device = current_device

        # Also: If target_name was not extracted, but a noun phrase was present,
        # never let target_device become that phrase unless enrolled
        if target_device.lower() not in enrolled_ids and target_device.lower() not in enrolled_names:
            target_device = current_device

        # If location is present or action is file-related, target_kind is filesystem_item
        if location or action in ("delete", "list", "count", "stat", "move", "copy", "rename"):
            if target_kind == "unknown":
                target_kind = "filesystem_item"

        # Resolve path if location is known or already grounded
        resolved_path: Optional[str] = None
        if grounded and (grounded.get("matched_path") or grounded.get("path")):
            resolved_path = grounded.get("matched_path") or grounded.get("path")
        elif location:
            loc_p = resolve_location_path(location, base_dir=base_dir)
            if loc_p:
                resolved_path = str(loc_p)

        # Resolve owning device
        owning_device: Optional[str] = None
        if grounded and (grounded.get("owning_device") or grounded.get("target_device")):
            owning_device = grounded.get("owning_device") or grounded.get("target_device")
            if owning_device == "current":
                owning_device = current_device
        elif is_remote_device_explicit:
            owning_device = target_device
        elif resolved_path:
            owning_device = current_device
        else:
            owning_device = target_device if target_device != "current" else current_device

        return ResolvedTarget(
            action=action,
            target_device=target_device,
            target_kind=target_kind,
            target_name=target_name,
            location=location,
            qualifiers=qualifiers,
            resolved_path=resolved_path,
            owning_device=owning_device,
        )


def match_filesystem_items(
    target_name: str,
    items: List[Dict[str, Any]],
    qualifiers: Optional[List[str]] = None,
) -> MatchResult:
    """
    Match target_name against direct children returned by fs.list.

    Matching criteria:
      - Normalized filename
      - Extension-insensitive comparison where sensible (stem match)
      - Shortcut/display name
      - Conservative fuzzy similarity (SequenceMatcher >= 0.85)

    Ambiguity Policy:
      - Does NOT silently select if multiple materially plausible matches exist.
      - If multiple candidates match (e.g. 'Project Alpha.lnk' and 'Project Alpha Old.lnk'),
        returns is_ambiguous=True and requires clarification.
    """
    if not target_name or not items:
        return MatchResult(matched_item=None)

    clean_target = target_name.strip().lower()
    quals = [q.lower() for q in (qualifiers or [])]

    # Strip filler words from target for comparison
    comp_target = clean_target
    for filler in (" game", " shortcut", " app", " file", " the "):
        comp_target = comp_target.replace(filler, " ").strip()
    comp_target = re.sub(r"\s+", " ", comp_target).strip()

    scored_candidates: List[Tuple[float, Dict[str, Any]]] = []

    for item in items:
        name = item.get("name", "")
        stem = Path(name).stem.lower().strip()
        ext = item.get("extension", "").lower().strip()
        norm_name = name.lower().strip()

        # Score components
        score = 0.0

        # 1. Exact stem or exact full name match
        if stem == comp_target or norm_name == comp_target:
            score = 1.0
            # Prefer shortcut (.lnk) if user asked for shortcut
            if "shortcut" in quals and ext == ".lnk":
                score += 0.05
        # 2. Exact word prefix match (e.g. target is "Project Alpha" and stem is "Project Alpha Old")
        elif stem.startswith(comp_target) or comp_target.startswith(stem) or comp_target in stem:
            # Overlap ratio
            ratio = len(comp_target) / max(len(stem), 1)
            score = 0.85 + (0.10 * ratio)
        # 3. Fuzzy similarity
        else:
            sim_stem = SequenceMatcher(None, comp_target, stem).ratio()
            sim_name = SequenceMatcher(None, comp_target, norm_name).ratio()
            best_sim = max(sim_stem, sim_name)
            if best_sim >= 0.85:
                score = best_sim

        if score >= 0.80:
            scored_candidates.append((score, item))

    if not scored_candidates:
        return MatchResult(matched_item=None)

    # Sort descending by score
    scored_candidates.sort(key=lambda x: x[0], reverse=True)

    # Check for ambiguity:
    # If there is more than 1 candidate and both are materially plausible (e.g. both >= 0.80):
    if len(scored_candidates) > 1:
        top_score, top_item = scored_candidates[0]
        second_score, second_item = scored_candidates[1]

        # Check if the user's target matches BOTH candidates plausibly
        # (e.g. 'Project Alpha.lnk' vs 'Project Alpha Old.lnk')
        top_stem = Path(top_item["name"]).stem.lower()
        sec_stem = Path(second_item["name"]).stem.lower()

        # If both stems contain the target or both have high scores:
        if (comp_target in top_stem and comp_target in sec_stem) or second_score >= 0.85:
            candidate_names = [c[1]["name"] for c in scored_candidates if c[0] >= 0.80]
            names_str = ", ".join(f"'{n}'" for n in candidate_names)
            return MatchResult(
                matched_item=None,
                is_ambiguous=True,
                candidates=[c[1] for c in scored_candidates if c[0] >= 0.80],
                clarification_message=(
                    f"Found multiple items matching '{target_name}': {names_str}. "
                    f"Please clarify which one you want to delete."
                ),
            )

        # If top score is substantially better than second (e.g. 1.05 vs 0.85):
        if top_score >= 1.0 and second_score < 0.90 and comp_target not in sec_stem:
            return MatchResult(matched_item=top_item, is_ambiguous=False)

        # If scores are close:
        if abs(top_score - second_score) < 0.20:
            candidate_names = [c[1]["name"] for c in scored_candidates if c[0] >= 0.80]
            names_str = ", ".join(f"'{n}'" for n in candidate_names)
            return MatchResult(
                matched_item=None,
                is_ambiguous=True,
                candidates=[c[1] for c in scored_candidates if c[0] >= 0.80],
                clarification_message=(
                    f"Found multiple potential matches for '{target_name}': {names_str}. "
                    f"Please clarify which one you mean."
                ),
            )

    return MatchResult(matched_item=scored_candidates[0][1], is_ambiguous=False)
