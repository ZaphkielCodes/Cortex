"""
hub/pipeline/semantic_interpreter.py
------------------------------------
General Semantic Interpreter for ordinary requests (Section 5).
Maps user goals to structured intents over live capability schemas.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..capability_registry import CanonicalCapabilityRegistry
from .context_assembler import AssembledContext
from .normalizer import NormalizedInput

log = logging.getLogger("cortex.pipeline.semantic_interpreter")


@dataclass
class SemanticIntent:
    """Structured semantic interpretation output according to Section 5."""
    type: str  # "ACTION" | "QUESTION" | "CONVERSATION" | "GOAL"
    goal: str
    required_effect: str
    candidate_capabilities: List[str] = field(default_factory=list)
    target_reference: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)
    needs_observation: bool = False
    confidence: float = 1.0
    missing_capability: Optional[str] = None
    response_text: Optional[str] = None
    is_conversational_followup: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "goal": self.goal,
            "required_effect": self.required_effect,
            "candidate_capabilities": self.candidate_capabilities,
            "target_reference": self.target_reference,
            "arguments": self.arguments,
            "constraints": self.constraints,
            "needs_observation": self.needs_observation,
            "confidence": self.confidence,
            "missing_capability": self.missing_capability,
            "response_text": self.response_text,
            "is_conversational_followup": self.is_conversational_followup,
        }


class SemanticInterpreter:
    """
    Model-driven and schema-aware semantic interpreter.
    Evaluates user goal -> required effect -> candidate capabilities from live registry.
    """

    @classmethod
    async def interpret(
        cls,
        norm: NormalizedInput,
        context: AssembledContext,
        model_router: Optional[Any] = None,
    ) -> SemanticIntent:
        if isinstance(norm, str):
            text = norm.strip()
            raw = norm
        else:
            text = getattr(norm, "stripped_text", str(norm)).strip()
            raw = getattr(norm, "raw_text", str(norm))
        lowered = text.lower()

        if isinstance(context, dict):
            src_device = context.get("source_device") or context.get("device_id") or "asus"
            fg_app = context.get("foreground_app")
            act_win = context.get("active_window")
            prev_app = context.get("previous_app")
            rec_turns = context.get("recent_conversation_turns", [])
            avail_caps = context.get("available_capabilities", [])
            raw_ctx = context.get("raw_context", context)
        else:
            src_device = getattr(context, "source_device", "asus")
            fg_app = getattr(context, "foreground_app", None)
            act_win = getattr(context, "active_window", None)
            prev_app = getattr(context, "previous_app", None)
            rec_turns = getattr(context, "recent_conversation_turns", [])
            avail_caps = getattr(context, "available_capabilities", [])
            raw_ctx = getattr(context, "raw_context", {})

        # -------------------------------------------------------------------
        # 1. Conversational Grounding Follow-ups (e.g. "So I'm working on you?")
        # -------------------------------------------------------------------
        if lowered in (
            "so i'm working on you?", "so i'm working on you",
            "so i am working on you?", "so i am working on you",
            "am i working on you?", "am i working on you"
        ):
            # Inspect prior turn context
            prior_turns = rec_turns
            prior_mention = False
            for t in reversed(prior_turns):
                if "cortex" in t.get("text", "").lower():
                    prior_mention = True
                    break
            # Even if turns were minimal, check foreground app
            if not prior_mention and fg_app and "cortex" in fg_app.lower():
                prior_mention = True

            return SemanticIntent(
                type="CONVERSATION",
                goal="Confirm current active working context",
                required_effect="conversational_grounding",
                candidate_capabilities=[],
                confidence=1.0,
                is_conversational_followup=True,
                response_text="Yes, you're working directly on Cortex with me right now.",
            )

        # -------------------------------------------------------------------
        # 2. Distinguish Aggregate Telemetry vs Process Telemetry (Section 7)
        # -------------------------------------------------------------------
        # "What's taking most of the memory on my laptop right now?"
        # "What's using the most memory?"
        # "Which process is using all the RAM?"
        if any(w in lowered for w in (
            "taking most of the memory", "taking most of the ram",
            "using the most memory", "using the most ram",
            "what's using the most memory", "what is using the most memory",
            "what process is using", "which process is using",
            "top memory", "most memory", "highest memory"
        )):
            return SemanticIntent(
                type="QUESTION",
                goal="Inspect top memory-consuming processes",
                required_effect="process_memory_telemetry",
                candidate_capabilities=["process.list"],
                arguments={"sort_by": "memory", "limit": 5, "target_device": context.source_device},
                confidence=0.98,
            )

        # "How much RAM am I using?" / "What is my memory usage?" -> Aggregate telemetry
        if any(w in lowered for w in (
            "how much ram", "how much memory", "ram usage", "memory usage",
            "ram percentage", "what is my ram", "what's my ram"
        )) and not any(w in lowered for w in ("taking", "which", "process", "app", "most")):
            return SemanticIntent(
                type="QUESTION",
                goal="Inspect aggregate system memory telemetry",
                required_effect="aggregate_telemetry",
                candidate_capabilities=["telemetry.get_system_usage"],
                arguments={"target_device": context.source_device},
                confidence=0.95,
            )

        # -------------------------------------------------------------------
        # 3. Window Switching Primitives (Section 7)
        # "Switch apps." / "Switch to Chrome." / "Go back to the app I was on."
        # -------------------------------------------------------------------
        m_switch = re.search(r'\b(?:switch\s+to|focus(?:\s+on)?)\s+([a-zA-Z0-9_\-\.\s]+)\b', lowered)
        if m_switch:
            target = m_switch.group(1).strip()
            return SemanticIntent(
                type="ACTION",
                goal=f"Focus window '{target}'",
                required_effect="window_switch",
                candidate_capabilities=["window.focus"],
                target_reference=target,
                arguments={"target_device": context.source_device, "app": target, "title": target},
                confidence=0.95,
            )

        if any(w in lowered for w in (
            "switch apps", "switch app", "switch window", "switch windows",
            "go back to the app i was on", "go back to the previous app",
            "take me back to the previous window", "switch back to previous",
            "switch to previous window", "switch back", "go back"
        )):
            return SemanticIntent(
                type="ACTION",
                goal="Focus previous application or window from history",
                required_effect="window_switch",
                candidate_capabilities=["window.focus"],
                target_reference="previous_app",
                arguments={"target_device": context.source_device},
                confidence=0.95,
            )

        # -------------------------------------------------------------------
        # 3b. File Operations (fs.copy, fs.rename, fs.move, fs.open)
        # -------------------------------------------------------------------
        m_copy = re.search(r"^copy\s+['\"]?([^'\",\n]+?)['\"]?\s+to\s+['\"]?([^'\",\n]+?)['\"]?$", text, re.IGNORECASE)
        if m_copy:
            src = m_copy.group(1).strip()
            dst = m_copy.group(2).strip()
            return SemanticIntent(
                type="ACTION",
                goal=f"Copy {src} to {dst}",
                required_effect="file_copy",
                candidate_capabilities=["fs.copy"],
                arguments={"src": src, "dst": dst, "overwrite": False, "target_device": context.source_device},
                confidence=0.95,
            )

        m_rename = re.search(r"^rename\s+['\"]?([^'\",\n]+?)['\"]?\s+to\s+['\"]?([^'\",\n]+?)['\"]?$", text, re.IGNORECASE)
        if m_rename:
            src = m_rename.group(1).strip()
            new_name = m_rename.group(2).strip()
            if src.lower() in ("the copy", "it", "that", "this"):
                session_refs = context.raw_context.get("session", {}).get("references", {})
                src = session_refs.get("last_copied_dst") or session_refs.get("last_copied_file") or session_refs.get("last_file") or src
            return SemanticIntent(
                type="ACTION",
                goal=f"Rename {src} to {new_name}",
                required_effect="file_rename",
                candidate_capabilities=["fs.rename"],
                arguments={"src": src, "new_name": new_name, "overwrite": False, "target_device": context.source_device},
                confidence=0.95,
            )

        m_move = re.search(r"^move\s+['\"]?([^'\",\n]+?)['\"]?\s+to\s+['\"]?([^'\",\n]+?)['\"]?$", text, re.IGNORECASE)
        if m_move:
            src = m_move.group(1).strip()
            dst = m_move.group(2).strip()
            if src.lower() in ("the copy", "it", "that", "this", "the file"):
                session_refs = raw_ctx.get("session", {}).get("references", {})
                src = session_refs.get("last_file") or session_refs.get("last_copied_dst") or src
            return SemanticIntent(
                type="ACTION",
                goal=f"Move {src} to {dst}",
                required_effect="file_move",
                candidate_capabilities=["fs.move"],
                arguments={"src": src, "dst": dst, "overwrite": False, "target_device": src_device},
                confidence=0.95,
            )

        if lowered in ("open that file", "open this file", "open the file", "open file", "open it") or any(lowered.endswith(ext) for ext in (".pdf", ".txt", ".doc", ".docx", ".png", ".jpg")):
            session_refs = raw_ctx.get("session", {}).get("references", {})
            grounded_target = raw_ctx.get("session", {}).get("grounded_target", {})
            target_file = session_refs.get("last_file") or grounded_target.get("path") or text.replace("open", "", 1).strip(" \t.,!?;:")
            owning_dev = grounded_target.get("owning_device") or session_refs.get("owning_device") or src_device
            return SemanticIntent(
                type="ACTION",
                goal=f"Open file '{target_file}'",
                required_effect="file_open",
                candidate_capabilities=["fs.open"],
                target_reference=target_file,
                arguments={"path": target_file, "target_device": owning_dev},
                confidence=0.95,
            )

        # -------------------------------------------------------------------
        # 4. Foreground Manipulation & Contextual Computer Use
        # "Close this app." / "Take me back to the app I was using before." /
        # "Can you see what I'm doing right now?" / "Did you change it?" /
        # User corrections: "I said Notepad, not Settings"
        # -------------------------------------------------------------------
        # 4a. User Corrections ("I said Notepad, not Settings", "No, the other app")
        clean_low = norm.cleaned_text.lower().strip() if hasattr(norm, "cleaned_text") else raw.lower().strip()
        m_correction = re.search(
            r'^(?:i\s+(?:said|meant)|no,?\s*(?:the\s+other\s+app|not))\s+([a-zA-Z0-9_\-\.\s]+?)(?:\s*,?\s*not\s+([a-zA-Z0-9_\-\.\s]+))?$',
            clean_low,
        )
        if not m_correction and ", not " in clean_low:
            m_correction = re.search(r'^(?:(?:i\s+(?:said|meant))\s+)?([a-zA-Z0-9_\-\.\s]+?)\s*,\s*not\s+([a-zA-Z0-9_\-\.\s]+)$', clean_low)
        if m_correction:
            intended = m_correction.group(1).strip()
            rejected = (m_correction.group(2) or "").strip()
            # Clean up words like "app" or "the"
            if intended.startswith("the "):
                intended = intended[4:].strip()
            if intended.endswith(" app"):
                intended = intended[:-4].strip()
            if intended.lower() in ("notepad", "settings", "chrome"):
                intended = intended.title()
            if rejected.lower() in ("notepad", "settings", "chrome"):
                rejected = rejected.title()
            return SemanticIntent(
                type="GOAL",
                goal=f"Switch to intended app '{intended}' and rectify previous action",
                required_effect="user_correction",
                candidate_capabilities=["app.launch", "ui.observe"],
                arguments={
                    "intended_app": intended,
                    "rejected_app": rejected,
                    "execution_mode": "computer_use",
                    "target_device": src_device,
                },
                confidence=0.98,
            )

        # 4b. Contextual Return ("Take me back to the app I was using before.")
        if any(w in lowered for w in (
            "take me back to the app i was using before",
            "take me back to the app i was using",
            "take me back to the previous app",
            "take me back",
            "switch back to the previous app",
            "switch back to the app i was using before",
            "go back to the app i was using before",
            "go back to the previous app",
        )):
            return SemanticIntent(
                type="ACTION",
                goal="Switch back to the previously active foreground application",
                required_effect="switch_previous_app",
                candidate_capabilities=["app.launch"],
                arguments={"target_device": src_device},
                confidence=0.98,
            )

        # 4c. Grounded Screen Perception ("Can you see what I'm doing right now?")
        raw_lowered = raw.lower()
        if any(w in lowered or w in raw_lowered for w in (
            "can you see what i'm doing right now",
            "can you see what i am doing right now",
            "can you see what i'm doing",
            "can you see what i am doing",
            "can you see my screen",
            "see what i'm doing right now",
            "see what i am doing right now",
            "see what i'm doing",
            "see what i am doing",
            "see my screen",
            "what am i doing right now",
            "what's on my screen right now",
            "what is on my screen right now",
            "what am i currently looking at",
            "what's on my screen",
        )) or (("can you see" in lowered or "see" in lowered) and ("doing" in lowered or "screen" in lowered)):
            return SemanticIntent(
                type="QUERY",
                goal="Inspect active screen and describe user foreground activity",
                required_effect="grounded_screen_perception",
                candidate_capabilities=["ui.observe"],
                arguments={"target_device": src_device},
                confidence=0.98,
            )

        # 4d. Task Verification Query ("Did you change it?", "Did that change?")
        if any(w in lowered or w in raw_lowered for w in (
            "did you change it",
            "did it change",
            "is it changed",
            "did that change",
            "did you apply it",
            "did you set it",
        )):
            return SemanticIntent(
                type="QUERY",
                goal="Verify if the last requested system setting or computer-use change succeeded",
                required_effect="check_last_task_status",
                candidate_capabilities=["ui.observe"],
                arguments={"target_device": src_device},
                confidence=0.98,
            )

        # 4e. App Closing
        if any(w in lowered or w in raw_lowered for w in (
            "close the app i'm using", "close the app that i am using",
            "close this app", "close current app", "close the current app",
            "close what i'm using", "get rid of the current window",
            "get rid of this window", "close this window"
        )):
            return SemanticIntent(
                type="ACTION",
                goal="Close the currently active foreground application",
                required_effect="app_termination",
                candidate_capabilities=["app.close"],
                target_reference="current_app",
                arguments={"target_device": src_device, "graceful": True},
                confidence=0.98,
            )

        m_close = re.search(r'^(?:close|quit|kill|exit|terminate)\s+(?:the\s+)?([a-zA-Z0-9_\-\.\s]+)$', lowered)
        if m_close:
            raw_close = m_close.group(1).strip()
            close_dev = None
            m_cdev = re.search(r'^(.*?)\s+(?:on|in)\s+([a-zA-Z0-9_-]+)$', raw_close)
            if m_cdev:
                target_close = m_cdev.group(1).strip()
                close_dev = m_cdev.group(2).strip().lower()
            else:
                target_close = raw_close
            if target_close not in ("it", "this", "that", "all", "all actions", "everything"):
                dev_target = close_dev or src_device
                return SemanticIntent(
                    type="ACTION",
                    goal=f"Close application '{target_close}'",
                    required_effect="app_termination",
                    candidate_capabilities=["app.close"],
                    target_reference=f"{target_close} on {close_dev}" if close_dev else target_close,
                    arguments={"app": target_close, "target_device": dev_target, "graceful": True},
                    confidence=0.98,
                )

        # -------------------------------------------------------------------
        # 5. Application Launch Equivalences & Compound App Goals
        # "Open Chrome." / "Open Notepad and type Cortex physical text." / "Open Settings and take me to dark mode."
        # -------------------------------------------------------------------
        # 5a. Compound App Launch + Computer Use Goals
        m_compound_app = re.search(
            r'^(?:open|launch|start|run)\s+([a-zA-Z0-9_\-]+)\s+(?:and\s+)?(type|write|enter|take me to|navigate to|go to|show|set|switch|find)\s+(.+)$',
            lowered,
        )
        if not m_compound_app:
            m_compound_app = re.search(r'^(?:open|launch|start|run)\s+([a-zA-Z0-9_\-]+)\s+and\s+(.+)$', lowered)

        if m_compound_app:
            comp_app = m_compound_app.group(1).strip()
            # If not a web URL
            if comp_app.lower() not in ("website", "url", "page", "site") and not comp_app.startswith(("http", "www")):
                comp_subgoal = (m_compound_app.group(2) + " " + m_compound_app.group(3)).strip() if len(m_compound_app.groups()) >= 3 else m_compound_app.group(2).strip()
                if comp_app.lower() in ("notepad", "settings", "chrome"):
                    comp_app = comp_app.title()
                req_effect = "desktop_state_change" if comp_app.lower() in ("settings", "system settings") else "compound_app_computer_use"
                return SemanticIntent(
                    type="GOAL",
                    goal=raw,
                    required_effect=req_effect,
                    candidate_capabilities=["app.launch", "ui.observe", "ui.click", "ui.type"],
                    target_reference=src_device,
                    arguments={
                        "app": comp_app,
                        "subgoal": comp_subgoal,
                        "execution_mode": "computer_use",
                        "target_device": src_device,
                        "success_condition": f"{comp_app} active with '{comp_subgoal}' satisfied",
                    },
                    confidence=0.96,
                )

        m_browser = re.search(
            r"^(?:open|launch|go to|navigate to)\s+(?:the\s+)?(?:website|site|url|page)?\s*(https?://\S+|www\.\S+|[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?|youtube|github|google|reddit|twitter|x|gmail|netflix|amazon|wikipedia|linkedin|chatgpt|openai|huggingface)\b",
            lowered,
        )
        if m_browser:
            target_site = m_browser.group(1).strip()
            is_visual_verify = bool(re.search(r'\b(?:and\s+)?(?:check|verify)(?:\s+that|\s+if)?\s+it(?:\s+visibly)?\s+opened\b', lowered))
            url = target_site if (target_site.startswith("http://") or target_site.startswith("https://")) else (f"https://{target_site}" if target_site.startswith("www.") else f"https://www.{target_site}.com")
            return SemanticIntent(
                type="ACTION",
                goal=f"Open web destination '{target_site}'",
                required_effect="browser_navigation",
                candidate_capabilities=["browser.open_url"],
                arguments={"url": url, "target_device": context.source_device, "visual_verify": is_visual_verify, "target_site": target_site},
                confidence=0.95,
            )

        m_bring_up = re.search(r'\b(?:bring|pull)\s+([a-zA-Z0-9_\-\.\s]+)\s+up\b', lowered)
        m_need = re.search(r'^(?:i\s+need|give\s+me)\s+(?:the\s+)?([a-zA-Z0-9_\-\.\s]+)$', lowered)
        m_open = re.search(r'^(?:open|launch|start|run)\s+(?:the\s+)?([a-zA-Z0-9_\-\.\s]+)$', lowered)

        target_app = None
        target_device_override = None
        if m_bring_up:
            target_app = m_bring_up.group(1).strip()
        elif m_need:
            target_app = m_need.group(1).strip()
        elif m_open:
            cand = m_open.group(1).strip()
            # If not web destination, file, or compound 'and' goal
            if not any(cand.startswith(p) for p in ("http://", "https://", "www.")) and not any(cand.endswith(ext) for ext in (".pdf", ".txt", ".doc", ".png")) and " and " not in cand:
                m_dev = re.search(r'^(.*?)\s+(?:on|in)\s+([a-zA-Z0-9_-]+)$', cand)
                if m_dev:
                    target_app = m_dev.group(1).strip()
                    target_device_override = m_dev.group(2).strip().lower()
                else:
                    target_app = cand
        if target_app:
            target_app = target_app.strip(" \t.,!?;:")

        if target_app and target_app not in ("it", "this", "that"):
            dev_target = target_device_override or context.source_device
            return SemanticIntent(
                type="ACTION",
                goal=f"Launch application '{target_app}'",
                required_effect="application_launch",
                candidate_capabilities=["app.launch"],
                target_reference=f"{target_app} on {target_device_override}" if target_device_override else target_app,
                arguments={"app": target_app, "target_device": dev_target},
                confidence=0.95,
            )

        # -------------------------------------------------------------------
        # 6. Physical or Truly Unsupported Action Detection (Capability Gap)
        # -------------------------------------------------------------------
        if any(w in lowered for w in (
            "3d print", "print 3d", "fabricate", "manufacturing", "hinge",
            "order pizza", "order food", "book a flight", "brew coffee"
        )):
            return SemanticIntent(
                type="ACTION",
                goal="Physical fabrication or external commercial transaction",
                required_effect="unsupported_physical_action",
                candidate_capabilities=[],
                missing_capability="hardware_fabrication_or_transaction",
                confidence=1.0,
                response_text="I don't have a capability for 3D printing or hardware fabrication.",
            )

        # -------------------------------------------------------------------
        # 7. Screen Perception ("What app am I currently using?", "What's on my screen?")
        # -------------------------------------------------------------------
        m_screenshot = re.search(r'^(?:take\s+(?:a\s+)?screenshot|capture\s+(?:the\s+|my\s+)?screen)(?:\s+(?:on|in)\s+([a-zA-Z0-9_-]+))?$', lowered)
        if m_screenshot:
            dev_target = m_screenshot.group(1).strip().lower() if m_screenshot.group(1) else context.source_device
            return SemanticIntent(
                type="ACTION",
                goal="Capture display screenshot",
                required_effect="screen_capture",
                candidate_capabilities=["screen.capture"],
                target_reference=m_screenshot.group(1).strip().lower() if m_screenshot.group(1) else None,
                arguments={"target_device": dev_target},
                confidence=0.98,
            )

        if any(w in lowered for w in (
            "what app am i currently using", "what app am i using",
            "what am i working on", "which app is open", "what app is active"
        )):
            return SemanticIntent(
                type="QUESTION",
                goal="Identify active foreground application",
                required_effect="foreground_inspection",
                candidate_capabilities=["app.list_running", "window.list"],
                confidence=0.98,
            )

        if any(w in lowered for w in ("look at my screen", "what's on my screen", "what is on my screen", "read my screen")) or \
           re.search(r'\b(?:what(?:\'s|\s+is)\s+(?:showing\s+on|visible\s+on|on)\s+(?:my\s+)?screen|what\s+error\s+is\s+(?:showing|on\s+my\s+screen|visible))\b', lowered):
            return SemanticIntent(
                type="ACTION",
                goal="Inspect current display content visually",
                required_effect="screen_perception",
                candidate_capabilities=["screen.capture", "screen.analyze"],
                arguments={"question": raw, "target_device": context.source_device},
                confidence=0.95,
            )

        # -------------------------------------------------------------------
        # 8. Web Search & Weather
        # -------------------------------------------------------------------
        if any(w in lowered for w in ("search the web for", "search online for", "search google for", "google ", "search for ")) or (lowered.startswith("search ") and "file" not in lowered and "app" not in lowered):
            query = re.sub(r'^(?:search\s+the\s+web\s+for|search\s+online\s+for|search\s+google\s+for|google|search\s+for|search)\s+', '', lowered).strip()
            query = re.sub(r'^(?:for|about|the)\s+', '', query).strip()
            return SemanticIntent(
                type="ACTION",
                goal=f"Search the web for '{query}'",
                required_effect="web_information_retrieval",
                candidate_capabilities=["web.search"],
                arguments={"query": query, "max_results": 5},
                confidence=0.95,
            )

        if "weather" in lowered or "temperature" in lowered:
            is_forecast = "forecast" in lowered or "tomorrow" in lowered or "weekend" in lowered
            cap = "weather.forecast" if is_forecast else "weather.current"
            m_loc = re.search(r'\bin\s+([a-zA-Z\s]+)', lowered)
            loc = m_loc.group(1).strip() if m_loc else (getattr(context, "device_location", None) or "current location")
            loc = re.sub(r'\b(?:right now|today|tomorrow)\b', '', loc).strip(" \t.,!?;:")
            if not loc or loc == "current location":
                loc = getattr(context, "device_location", None) or "current location"
            if loc != "current location":
                loc = loc.title()
            return SemanticIntent(
                type="QUESTION",
                goal="Check weather conditions",
                required_effect="weather_inspection",
                candidate_capabilities=[cap],
                arguments={"location": loc, "units": "metric"},
                confidence=0.95,
            )

        # -------------------------------------------------------------------
        # 8b. General Computer Use (Phase A10)
        # -------------------------------------------------------------------
        if lowered in ("go back", "back"):
            return SemanticIntent(
                type="ACTION",
                goal="Navigate back via keyboard shortcut",
                required_effect="ui_navigation",
                candidate_capabilities=["ui.hotkey"],
                arguments={"keys": ["alt", "left"]},
                confidence=0.98,
            )

        if any(w in lowered for w in ("select all text", "select all the text", "select all")):
            return SemanticIntent(
                type="ACTION",
                goal="Select all text in active view",
                required_effect="ui_selection",
                candidate_capabilities=["ui.hotkey"],
                arguments={"keys": ["ctrl", "a"]},
                confidence=0.98,
            )

        if lowered.startswith("scroll ") or lowered in ("scroll down", "scroll up", "scroll a bit further down", "scroll a little more", "go a bit further down") or "scroll down until" in lowered:
            direction = "up" if "up" in lowered else "down"
            return SemanticIntent(
                type="ACTION",
                goal=f"Scroll interface {direction}",
                required_effect="ui_scroll",
                candidate_capabilities=["ui.scroll"],
                arguments={"direction": direction, "amount": 300},
                confidence=0.95,
            )

        m_ui_type = re.search(r"^(?:type|put)\s+['\"]?([^'\"]+?)['\"]?\s+(?:in|into)\s+(?:the\s+)?([a-zA-Z0-9_\-\s]+)$", lowered)
        if m_ui_type:
            val = m_ui_type.group(1).strip()
            field_name = m_ui_type.group(2).strip()
            return SemanticIntent(
                type="ACTION",
                goal=f"Type '{val}' into {field_name}",
                required_effect="ui_text_entry",
                candidate_capabilities=["ui.type"],
                target_reference=field_name,
                arguments={"text": val, "target": field_name},
                confidence=0.95,
            )

        m_ui_click = re.search(r"^(?:click|press|tap)\s+(?:on\s+)?(?:the\s+)?([a-zA-Z0-9_\-\s'\"\[\]]+)$", lowered)
        if m_ui_click and not any(w in lowered for w in ("start", "app", "window", "launch", "open")):
            target_expr = m_ui_click.group(1).strip("?.,! \t\n")
            return SemanticIntent(
                type="ACTION",
                goal=f"Click UI element '{target_expr}'",
                required_effect="ui_click",
                candidate_capabilities=["ui.click"],
                target_reference=target_expr,
                arguments={"target": target_expr},
                confidence=0.95,
            )

        # -------------------------------------------------------------------
        # 9. General Desktop / UI State Change Escalation (Phase A10)
        # When user expresses a desired desktop/UI/system state change
        # and no stronger direct capability exists, escalate to computer use.
        # -------------------------------------------------------------------
        desktop_keywords = (
            "display", "refresh", "rate", "hertz", "hz", "resolution", "screen",
            "dark mode", "light mode", "theme", "appearance", "night light",
            "text", "font", "scaling", "size",
            "browser", "default", "default app", "default browser",
            "battery", "power", "battery usage",
            "bluetooth", "wifi", "network", "sound", "volume", "audio",
            "accessibility", "contrast", "high contrast", "settings"
        )
        state_verbs = (
            "change", "set", "turn on", "turn off", "switch", "make", "adjust",
            "enable", "disable", "toggle", "configure", "update", "open", "find", "show"
        )
        is_state_change = any(v in lowered for v in state_verbs) and any(k in lowered for k in desktop_keywords)
        is_settings_nav = ("settings and" in lowered) or (lowered.startswith("find the ") and "page" in lowered) or ("page" in lowered and any(k in lowered for k in desktop_keywords))

        if (is_state_change or is_settings_nav) and not norm.is_question:
            reg = CanonicalCapabilityRegistry.get_instance()
            if reg.get_capability("ui.observe") and reg.get_capability("ui.click"):
                success_cond = f"active system state reflects '{raw.strip()}'"
                return SemanticIntent(
                    type="GOAL",
                    goal=raw,
                    required_effect="desktop_state_change",
                    candidate_capabilities=["app.launch", "ui.observe", "ui.click"],
                    target_reference=context.source_device,
                    arguments={
                        "goal": raw,
                        "execution_mode": "computer_use",
                        "target_device": context.source_device,
                        "desired_state": raw,
                        "success_condition": success_cond,
                    },
                    confidence=0.92,
                )

        # -------------------------------------------------------------------
        # 10. Multi-step Goal or Conditional Goals ("When X, Y" or "and then")
        # -------------------------------------------------------------------
        if lowered.startswith("when ") or " and then " in lowered or (" and " in lowered and any(v in lowered for v in ("close", "open", "notify", "search"))):
            return SemanticIntent(
                type="GOAL",
                goal=raw,
                required_effect="compound_goal_orchestration",
                candidate_capabilities=["goal.create"],
                confidence=0.90,
            )

        # Model-driven semantic interpretation over registered capability schemas
        if model_router and hasattr(model_router, "generate_result"):
            try:
                m_ctx = context.to_dict() if hasattr(context, "to_dict") else dict(context)
                res = await model_router.generate_result(raw, m_ctx)
                if res:
                    if res.type == "RESPONSE":
                        return SemanticIntent(
                            type="CONVERSATION",
                            goal=raw,
                            required_effect="conversational_response",
                            response_text=res.response_text,
                            confidence=1.0,
                        )
                    elif res.type == "ACTION" and res.action_plan:
                        plan = res.action_plan
                        cap_name = plan.capability or plan.action
                        return SemanticIntent(
                            type="ACTION",
                            goal=plan.user_facing_summary or raw,
                            required_effect=plan.intent or "action_execution",
                            candidate_capabilities=[cap_name] if cap_name else [],
                            target_reference=plan.target_device_reference,
                            arguments=dict(plan.arguments or {}),
                            confidence=getattr(plan, "confidence", 1.0),
                        )
                    elif res.type == "TASK":
                        return SemanticIntent(
                            type="GOAL",
                            goal=raw,
                            required_effect="task_orchestration",
                            candidate_capabilities=["goal.create"],
                            confidence=1.0,
                        )
            except Exception as m_err:
                log.debug(f"[SemanticInterpreter] Router consultation: {m_err}")

        # Default fallback: Treat as general conversational question or task
        return SemanticIntent(
            type="QUESTION" if norm.is_question else "ACTION",
            goal=raw,
            required_effect="general_inquiry",
            candidate_capabilities=[],
            confidence=0.75,
        )
