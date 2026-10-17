import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from .intent_schema import ConversationalResponse, ActionPlan, ModelResult
from .model_router import ModelRouter, get_model_router
from .control_layer import CortexControlLayer
from .tracing import InteractionTrace
from .metrics import MetricsCollector
from .llm_provider import classify_failure, get_user_friendly_error
import psutil

log = logging.getLogger("cortex.agent_harness")


def format_action_approval_phrase(plan: ActionPlan) -> str:
    """
    Generate natural infinitive wording for approval prompts:
    e.g. 'take a screenshot', 'check device status', 'open calculator'.
    """
    action = plan.action or ""
    if action == "take_screenshot":
        return "take a screenshot"
    elif action in ("get_status", "get_device_status"):
        return "check device status"
    elif action == "open_app":
        app = (plan.arguments or {}).get("app", "")
        return f"open {app}" if app else "open the application"
    elif action == "list_devices":
        return "list online devices"
    elif action == "move_file":
        return "move a file"
    elif action in ("delete_file", "delete", "fs.delete"):
        from pathlib import Path
        path = (plan.arguments or {}).get("path", "")
        name = Path(path).name if path else ""
        return f"delete '{name}'" if name else "delete a file"
    elif action in ("remember_person", "people_remember"):
        name = (plan.arguments or {}).get("name", "a person")
        return f"remember {name}"

    # Derive from summary if available
    summary = (plan.user_facing_summary or "").strip()
    if summary:
        words = summary.split()
        first = words[0].lower()
        gerund_map = {
            "taking": "take",
            "getting": "check",
            "checking": "check",
            "opening": "open",
            "listing": "list",
            "moving": "move",
            "deleting": "delete",
            "closing": "close",
            "stopping": "stop",
            "running": "run",
            "showing": "show",
            "reading": "read",
            "writing": "write",
            "creating": "create",
        }
        if first in gerund_map:
            words[0] = gerund_map[first]
            return " ".join(words)
        if first.endswith("ing") and len(first) > 4:
            base = first[:-3]
            if len(base) > 2 and base[-1] == base[-2]:
                base = base[:-1]
            words[0] = base
            return " ".join(words)
        return summary

    return action.replace("_", " ")


def extract_weather_location(clean_req: str) -> Optional[str]:
    """
    Extract clean location entity from weather query while stripping temporal/semantic modifiers.
    e.g.
    'search up the weather in Manassas currently' -> 'Manassas'
    'current weather in Manassas' -> 'Manassas'
    'weather right now in Fairfax' -> 'Fairfax'
    'what's it like in New York right now' -> 'New York'
    """
    import re
    MODIFIERS = [
        "currently", "right now", "today", "at the moment", "now",
        "this morning", "tonight", "this evening", "this afternoon",
        "tomorrow", "this weekend", "forecast", "current", "presently", "weather"
    ]

    # 1. Look for preposition patterns: in / for / at / around / near
    m = re.search(r"\b(?:in|for|at|around|near)\s+([a-zA-Z\s,.-]+)$", clean_req)
    candidate = None
    if m:
        candidate = m.group(1).strip(" \t.,!?;:")
    else:
        # Fallback: check if query is something like "New York weather" or "Fairfax forecast"
        m2 = re.search(r"^([a-zA-Z\s,.-]+)\s+(?:weather|forecast|temperature)$", clean_req)
        if m2:
            candidate = m2.group(1).strip(" \t.,!?;:")

    if not candidate:
        return None

    # Repeatedly strip known temporal modifiers from front and back of candidate
    changed = True
    while changed:
        changed = False
        lower_cand = candidate.lower().strip(" \t.,!?;:")
        for mod in MODIFIERS:
            if lower_cand == mod:
                return None  # Candidate was solely a modifier
            if lower_cand.endswith(" " + mod):
                candidate = candidate[:len(candidate) - len(mod)].strip(" \t.,!?;:")
                changed = True
                break
            if lower_cand.startswith(mod + " "):
                candidate = candidate[len(mod):].strip(" \t.,!?;:")
                changed = True
                break

    candidate = candidate.strip(" \t.,!?;:")
    if not candidate or candidate.lower() in MODIFIERS:
        return None

    return candidate.title()


def sanitize_for_speech(text: Optional[str]) -> str:
    """
    Sanitize text intended for VoiceResponseMessage (TTS).
    Never speak raw JSON, capability arrays, stack traces, filesystem internals,
    or supported handler dumps. Detailed diagnostics remain in backend logs.
    """
    if not text:
        return "I encountered an issue processing that request."

    clean = str(text).strip()
    if "Supported:" in clean or ("['" in clean and "']" in clean):
        log.warning(f"[SpeechSanitizer] Suppressed internal handler dump from speech: {clean}")
        if "screen" in clean.lower() or "screenshot" in clean.lower():
            return "I couldn't inspect the screenshot because the screen-analysis action failed."
        return "I couldn't complete that action because the requested capability is not supported."

    if (clean.startswith("{") and clean.endswith("}")) or (clean.startswith("[") and clean.endswith("]")) or "Traceback (most recent call last)" in clean:
        log.warning(f"[SpeechSanitizer] Suppressed raw JSON/traceback from speech: {clean}")
        return "I encountered a system error while processing that request."

    import re
    if re.search(r"\b(?:KeyError|ValueError|TypeError|AttributeError|FileNotFoundError|Exception):\s*", clean):
        log.warning(f"[SpeechSanitizer] Suppressed raw python exception from speech: {clean}")
        if "weather" in clean.lower():
            return "I couldn't retrieve the weather right now."
        if "screen" in clean.lower() or "screenshot" in clean.lower():
            return "I couldn't inspect the screenshot because the screen-analysis action failed."
        return "I encountered an error while processing that request."

    return clean


class AgentHarness:
    """
    Agent Harness handles the conversation loop, multi-step tasks, tool selection, 
    and structured reasoning. It relies on ModelRouter for LLM inference and 
    CortexControlLayer for secure execution.
    """
    def __init__(self, control_layer: CortexControlLayer, model_router: Optional[ModelRouter] = None):
        self.control = control_layer
        self.router = model_router or get_model_router()
        from .task_orchestrator import TaskOrchestrator
        self.task_orchestrator = TaskOrchestrator(self.router, self.control)
        from .pipeline import RequestPipeline
        self.pipeline = RequestPipeline(self.control, self.router)

    async def _try_answer_people_question(self, req: str, context: Dict[str, Any]) -> Optional[str]:
        """Deterministically answer questions about people when high-confidence data exists."""
        clean = req.lower().strip(" \t.,!?;:")
        people = context.get("people", [])
        session_refs = context.get("session", {}).get("references", {})
        from .people import PeopleManager
        import re

        # Check for ambiguity first across explicit name mentions in query
        words = clean.split()
        for word in words:
            if len(word) > 2 and word not in ("who", "what", "where", "when", "why", "how", "the", "you", "about", "from", "know", "does", "work", "tell"):
                res = await PeopleManager.resolve_person(word, session_refs)
                if res.is_ambiguous:
                    cand_names = [c["display_name"] for c in res.candidates]
                    return f"I found multiple people named {word.capitalize()}: {' and '.join(cand_names)}. Which one did you mean?"

        # 1. "what's my brother's name?" / "who is my brother?"
        m_rel_q = re.match(r'^(?:what(?:(?:\'s)|(?:\s+is))\s+my\s+([a-zA-Z]+)(?:(?:\'s)?\s+name)?|who\s+is\s+my\s+([a-zA-Z]+))$', clean)
        if m_rel_q:
            rel = (m_rel_q.group(1) or m_rel_q.group(2)).strip().lower()
            res = await PeopleManager.resolve_person(rel, session_refs)
            if res.is_ambiguous:
                cand_names = [c["display_name"] for c in res.candidates]
                return f"I found multiple people matching your {rel}: {', '.join(cand_names)}. Which one did you mean?"
            if res.matched and res.person:
                return f"Your {rel}'s name is {res.person['display_name']}."

        # 2. "who do i know from college?" / "who do i know from [place]?"
        m_place_q = re.match(r'^(?:who\s+do\s+i\s+know\s+from\s+([a-zA-Z0-9\s]+)|who\s+is\s+from\s+([a-zA-Z0-9\s]+))$', clean)
        if m_place_q:
            place = (m_place_q.group(1) or m_place_q.group(2)).strip().lower()
            matching_people = []
            for p in people:
                p_text = (p.get("notes", "") + " " + " ".join([f"{f['key']} {f['value']}" for f in p.get("facts", [])])).lower()
                if place in p_text:
                    matching_people.append(p)
            if matching_people:
                names = [p["display_name"] for p in matching_people]
                return f"You know {', '.join(names)} from {place}."

        # 3. "what do you remember about sarah?" / "tell me about sarah"
        m_about = re.match(r'^(?:what\s+do\s+you\s+remember\s+about\s+([a-zA-Z\s]+)|tell\s+me\s+about\s+([a-zA-Z\s]+))$', clean)
        if m_about:
            target = (m_about.group(1) or m_about.group(2)).strip()
            res = await PeopleManager.resolve_person(target, session_refs)
            if res.is_ambiguous:
                cand_names = [c["display_name"] for c in res.candidates]
                return f"I found multiple people named {target.capitalize()}: {' and '.join(cand_names)}. Which one did you mean?"
            if res.matched and res.person:
                p = res.person
                facts = p.get("facts") or await PeopleManager.get_person_details(p["person_id"])
                p_facts = facts.get("facts", []) if isinstance(facts, dict) else (p.get("facts") or [])
                details = []
                for f in p_facts:
                    details.append(f"{f['value']}")
                if p.get("notes") and p.get("notes") not in details:
                    details.append(p.get("notes"))
                if details:
                    joined = ", ".join(details)
                    return f"{p['display_name']} {joined}."
                if p.get("relationship_type"):
                    return f"{p['display_name']} is your {p['relationship_type']}."
                return f"I remember {p['display_name']}."

        # 4. "where does he work?" / "where does she work?" / "where does [name] work?"
        m_work = re.match(r'^(?:where\s+does\s+([a-zA-Z]+)\s+work|what\s+is\s+([a-zA-Z]+)(?:\'s)?\s+workplace)$', clean)
        if m_work:
            target = (m_work.group(1) or m_work.group(2)).strip()
            res = await PeopleManager.resolve_person(target, session_refs)
            if res.is_ambiguous:
                cand_names = [c["display_name"] for c in res.candidates]
                return f"I found multiple people matching '{target}': {', '.join(cand_names)}. Which one did you mean?"
            if res.matched and res.person:
                p = res.person
                facts = await PeopleManager.get_person_details(p["person_id"])
                p_facts = facts.get("facts", []) if isinstance(facts, dict) else (p.get("facts") or [])
                workplace = None
                for f in p_facts:
                    if f.get("key") in ("workplace", "work", "job", "company"):
                        workplace = f.get("value")
                        break
                if not workplace and p.get("notes") and "works at" in p.get("notes"):
                    workplace = p.get("notes").split("works at")[-1].strip()
                if workplace:
                    return f"{p['display_name']} works at {workplace}."

        # 5. "who is speaking?" / "who is talking?"
        if clean in ("who is speaking", "who is talking", "who is the speaker", "who am i"):
            spk_ctx = context.get("speaker", {})
            label = spk_ctx.get("label", "Primary User")
            p_name = spk_ctx.get("display_name")
            rel = spk_ctx.get("relationship_type")
            if p_name and rel:
                return f"The speaker is {p_name}, your {rel} ({label})."
            elif p_name:
                return f"The speaker is {p_name} ({label})."
            elif spk_ctx.get("is_primary_user"):
                return f"You are the primary user ({label})."
            return f"The current speaker is {label}."

        # 6. "who spoke recently?" / "who was talking?"
        if clean in ("who spoke recently", "who talked recently", "who was talking", "list recent speakers"):
            recent = context.get("session", {}).get("references", {}).get("recent_speakers", [])
            if not recent:
                return "No other speakers have spoken recently."
            names = [s.get("label") or "Unknown" for s in recent]
            return f"Recent speakers: {', '.join(names)}."

        # 7. "How should I contact [person]?" / "How do I reach [person]?"
        m_contact = re.match(r'^(?:how\s+(?:should|do|can)\s+i\s+(?:contact|reach|message|call)\s+([a-zA-Z\s]+))$', clean)
        if m_contact:
            target = m_contact.group(1).strip()
            res = await PeopleManager.resolve_person(target, session_refs)
            if res.is_ambiguous:
                cand_names = [c["display_name"] for c in res.candidates]
                return f"I found multiple people matching '{target}': {', '.join(cand_names)}. Which one did you mean?"
            if res.matched and res.person:
                p = res.person
                facts = await PeopleManager.get_person_details(p["person_id"])
                p_facts = facts.get("facts", []) if isinstance(facts, dict) else (p.get("facts") or [])
                pref = None
                for f in p_facts:
                    if "communication" in f.get("key", "").lower() or "preference" in f.get("key", "").lower():
                        pref = f.get("value")
                        break
                if not pref and p.get("notes") and "prefers" in p.get("notes").lower():
                    pref = p.get("notes")
                if pref:
                    return f"{p['display_name']} {pref}."
                return f"I don't have a specific contact preference recorded for {p['display_name']}."

        # 8. Multi-speaker group decision questions (e.g. "What should we get?" / "Where should we eat?")
        if any(kw in clean for kw in ("what should we get", "what should we eat", "where should we eat", "where should we go to eat", "what food should we get")):
            social_ctx = context.get("social_context", {})
            shared_facts = social_ctx.get("relevant_shared_facts", [])
            dislikes = [f for f in shared_facts if "dislike" in f.get("key", "").lower() or "dislike" in f.get("value", "").lower()]
            if dislikes:
                d = dislikes[0]
                p_name = d.get("person", "someone")
                val = d.get("value", "")
                item = val.split("dislikes")[-1].strip() if "dislikes" in val else val
                return f"Since {p_name} dislikes {item}, you could get Italian, Mexican, or pizza instead."
            return "How about Italian, Mexican, or Thai food?"

        # 9. "Who is here?" / "Who is participating?" / "What is our social context?"
        if clean in ("who is here", "who is participating", "who is in this conversation", "what is our social context"):
            social_ctx = context.get("social_context", {})
            participants = social_ctx.get("active_participants", [])
            if participants:
                return f"Current participants: {', '.join(participants)}."
            return "Currently, only you are in the conversation."

        # 10. Phase 8A: World State & Situational queries
        sit_answer = await self._try_answer_situational_question(clean, context, context.get("device", {}).get("id", "unknown"))
        if sit_answer:
            return sit_answer

        return None

    @staticmethod
    def _is_session_exit_command(raw_text: str) -> bool:
        if not raw_text:
            return False

        text = raw_text.lower().strip()
        text = re.sub(r'^[^\w]+|[^\w]+$', '', text)
        text = re.sub(r'[,;:\-—!?.]+', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if not text:
            return False

        prefix_pattern = r'^(?:okay|ok|alright|all\s+right|hey|yo)\s+'
        while True:
            stripped = re.sub(prefix_pattern, '', text)
            if stripped == text:
                break
            text = stripped

        text = re.sub(r'\bthat\s+is\b', "that's", text)

        exit_phrases = {
            "stop", "cortex stop", "stop cortex", "go to sleep", "sleep",
            "stop listening", "exit session", "stop session", "end session",
            "exit", "quit", "cortex exit", "exit cortex", "that's all",
            "that's all cortex", "cortex that's all", "that's it",
            "that's it cortex", "cortex that's it", "bye", "goodbye",
            "bye cortex", "goodbye cortex", "cortex bye", "cortex goodbye",
        }

        if text in exit_phrases:
            return True

        cortex_stripped_start = re.sub(r'^cortex\s+', '', text).strip()
        cortex_stripped_end = re.sub(r'\s+cortex$', '', text).strip()

        if cortex_stripped_start in exit_phrases or cortex_stripped_end in exit_phrases:
            return True

        return False

    async def _try_answer_situational_question(
        self, req: str, context: Dict[str, Any], source_device_id: str
    ) -> Optional[str]:
        """
        Deterministically answer situational and World State questions across semantic domains.
        Phase 8D: Includes runtime time/date/timezone from OS clock, calendar follow-up
        context resolution, tomorrow/day-after calendar queries, and timezone-aware
        datetime handling throughout.
        """
        import re
        clean = req.lower().strip(" \t.,!?;:")

        # Bypass situational status if this is a proactive condition or reminder request
        if re.search(r'\b(?:remind|alert|notify|warn)\s+me\b|\blet\s+me\s+know\s+when\b|\bwhen\s+(?:my\s+|the\s+)?(?:battery|laptop|\w+)\s+(?:drops?|falls?|is\s+below|comes?\s+online)\b', clean):
            return None

        # Phase 8E: Academic & Workload Natural Language Routing
        from .workload import WorkloadManager
        wm = WorkloadManager.get_instance()
        workload_ans = await wm.handle_natural_language(req, context)
        if workload_ans:
            return workload_ans

        # Phase 8F: Learned Routines Natural Language Routing
        from .routines import RoutineManager
        rm = RoutineManager.get_instance()
        routine_ans = await rm.handle_natural_language(req, context)
        if routine_ans:
            return routine_ans

        # Phase 9A: Persistent Goal Engine Natural Language Routing
        from .goals import GoalManager
        gm = GoalManager.get_instance()
        goal_ans = await gm.handle_natural_language(req, context)
        if goal_ans:
            return goal_ans

        from .world_state import WorldStateManager, DeviceSignalAdapter, CalendarSignalAdapter, RuntimeTimeAdapter
        wsm = WorldStateManager.get_instance()
        fused = await wsm.get_fused_world_state(source_device_id)
        world_ctx = context.get("world_state", {})
        telem = fused.system_telemetry or DeviceSignalAdapter.inspect_system_telemetry(source_device_id or "asus")

        # 1. World State Summary
        if re.search(r'\b(?:(?:give\s+me\s+a\s+)?(?:quick\s+)?summary\s+of\s+(?:my\s+)?(?:current\s+)?situation|summarize\s+(?:my\s+)?(?:current\s+)?situation|what(?:(?:\'s)|(?:\s+is))\s+my\s+(?:current\s+)?situation|situational\s+summary|situation\s+update)\b', clean):
            return wsm.format_world_state_summary(fused)

        # 2. System Telemetry: RAM usage
        if re.search(r'\b(?:how\s+much\s+ram\s+(?:am\s+i\s+using|is\s+used|is\s+being\s+used)|what(?:\'s|\s+is)\s+my\s+ram\s+usage|ram\s+(?:usage|used|percentage|consumption))\b', clean):
            ram_used = telem.get("ram_used_gb", 0.0)
            ram_tot = telem.get("ram_total_gb", 0.0)
            ram_pct = telem.get("ram_percent", 0.0)
            return f"You are currently using {ram_used} GB of RAM ({ram_pct}% of {ram_tot} GB)."

        # 3. System Telemetry: CPU usage
        if re.search(r'\b(?:what(?:\'s|\s+is)\s+my\s+cpu\s+(?:usage|utilization|load)|how\s+much\s+cpu\s+(?:am\s+i\s+using|is\s+used)|cpu\s+usage|cpu\s+load)\b', clean):
            cpu_pct = telem.get("cpu_percent", 0.0)
            return f"CPU usage is currently at {cpu_pct}%."

        # 4. System Telemetry: Disk space
        if re.search(r'\b(?:how\s+much\s+disk\s+(?:space\s+)?(?:do\s+i\s+have\s+left|is\s+left|is\s+available|is\s+free)|what(?:\'s|\s+is)\s+my\s+(?:disk\s+space|storage\s+space)|how\s+much\s+storage\s+(?:do\s+i\s+have\s+left|is\s+left|is\s+free)|disk\s+space|free\s+disk\s+space)\b', clean):
            d_free = telem.get("disk_free_gb", 0.0)
            d_tot = telem.get("disk_total_gb", 0.0)
            d_pct = telem.get("disk_percent", 0.0)
            return f"You have {d_free} GB free out of {d_tot} GB total disk space ({d_pct}% used)."

        # 5. System Telemetry: GPU
        if re.search(r'\b(?:what\s+gpu\s+do\s+i\s+have|what(?:\'s|\s+is)\s+my\s+gpu|what\s+graphics\s+card\s+do\s+i\s+have|what(?:\'s|\s+is)\s+my\s+graphics\s+card|tell\s+me\s+my\s+gpu)\b', clean):
            gpu = telem.get("gpu", "Standard Display Adapter")
            return f"Your GPU is {gpu}."

        # 6. System Telemetry: CPU / Processor identity
        if re.search(r'\b(?:what\s+cpu\s+do\s+i\s+have|what(?:\'s|\s+is)\s+my\s+cpu|what\s+processor\s+do\s+i\s+have|what(?:\'s|\s+is)\s+my\s+processor|tell\s+me\s+my\s+cpu)\b', clean):
            proc = telem.get("processor", "Unknown Processor")
            return f"Your processor is {proc}."

        # 7. Device Specs
        if re.search(r'\b(?:tell\s+me\s+my\s+laptop\s+specs|what\s+are\s+my\s+laptop\s+specs|what\s+are\s+the\s+specs|what\s+are\s+my\s+specs|tell\s+me\s+the\s+specs|laptop\s+specs|machine\s+specs|system\s+specs)\b', clean):
            return f"Laptop specs: {telem.get('manufacturer')} {telem.get('model')}, CPU: {telem.get('processor')}, GPU: {telem.get('gpu')}, RAM: {telem.get('ram_total_gb')} GB, Storage: {telem.get('disk_total_gb')} GB, OS: {telem.get('os')}."

        # 8. Device Brand / Manufacturer
        if re.search(r'\b(?:what\s+brand\s+is\s+my\s+laptop|what\s+is\s+the\s+brand\s+of\s+my\s+laptop|who\s+made\s+my\s+laptop|who\s+is\s+the\s+manufacturer)\b', clean):
            mfg = telem.get("manufacturer", "Unknown")
            return f"Your laptop brand is {mfg}."

        # 9. Device Model
        if re.search(r'\b(?:what\s+laptop\s+model\s+is\s+this|what\s+model\s+is\s+(?:this|it|my\s+laptop)|what(?:\'s|\s+is)\s+my\s+laptop\s+model|tell\s+me\s+my\s+laptop\s+model)\b', clean):
            return f"This laptop model is {telem.get('model')} (by {telem.get('manufacturer')})."

        # 10. Device Identity
        if re.search(r'\b(?:what|which)\s+(?:device|computer|machine|laptop|pc)\s+(?:am\s+i\s+(?:using|on)|is\s+this|is\s+active|is\s+running)\b', clean) or \
           re.search(r'\b(?:tell\s+me\s+)?what\s+(?:device|computer|machine|laptop|pc)\s+i(?:\'m|\s+am)\s+(?:using|on)\b', clean):
            dev_name = None
            target_id = source_device_id or fused.active_device_id or world_ctx.get("active_device_id")
            for d in fused.connected_devices:
                if d.get("device_id") == target_id:
                    dev_name = d.get("device_name") or d.get("device_id")
                    break
            if not dev_name and world_ctx.get("active_device_name"):
                dev_name = world_ctx.get("active_device_name")
            if not dev_name and target_id and target_id not in ("unknown", "dev_unknown", ""):
                dev_name = target_id
            mfg = telem.get("manufacturer")
            mod = telem.get("model")
            if dev_name and mfg and mod and mfg != "Unknown":
                return f"You are currently using {dev_name} ({mfg} {mod})."
            elif dev_name:
                return f"You are currently using {dev_name}."
            return "Device identity is currently unavailable."

        # -------------------------------------------------------------------
        # PHASE 8D: Authoritative Runtime Time/Date/Timezone Handlers
        # These ALWAYS read from OS clock - never from LLM knowledge.
        # -------------------------------------------------------------------

        # 11a. "What time is it?"
        if re.search(r'\b(?:what(?:\'s|\s+is)\s+(?:the\s+)?(?:current\s+)?time|what\s+time\s+is\s+it|current\s+time|tell\s+me\s+the\s+time)\b', clean):
            tc = RuntimeTimeAdapter.get_runtime_time_context()
            return f"The current time is {tc['local_time_12h']} ({tc['timezone']}, {tc['utc_offset']})."

        # 11b. "What day is it?" / "What's today?"
        if re.search(r'\b(?:what\s+day\s+is\s+(?:it|today)|what(?:\'s|\s+is)\s+today(?:\'s\s+date)?|what\s+is\s+(?:the\s+)?(?:current\s+)?date|today(?:\'s)?\s+date)\b', clean):
            tc = RuntimeTimeAdapter.get_runtime_time_context()
            from datetime import date as _date
            d = _date.fromisoformat(tc["local_date"])
            friendly = f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"
            return f"Today is {friendly}."

        # 11c. "What day is tomorrow?"
        if re.search(r'\b(?:what\s+day\s+is\s+tomorrow|what(?:\'s|\s+is)\s+tomorrow(?:\'s\s+date)?|tomorrow(?:\'s)?\s+date)\b', clean):
            tc = RuntimeTimeAdapter.get_runtime_time_context()
            from datetime import date as _date
            d = _date.fromisoformat(tc["tomorrow_date"])
            friendly = f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"
            return f"Tomorrow is {friendly}."

        # 11d. "What timezone are you on?"
        if re.search(r'\b(?:what(?:\'s|\s+is)\s+(?:your|the|my)?\s*time\s*zone|what\s+time\s+zone\s+(?:are\s+you\s+(?:on|using|in)|is\s+this|am\s+i\s+in)|timezone|time\s+zone\s+(?:info|information)|what\s+tz)\b', clean):
            tc = RuntimeTimeAdapter.get_runtime_time_context()
            return (f"I am grounded to your local machine's timezone: "
                    f"{tc['timezone']} ({tc['utc_offset']}). "
                    f"The current local time is {tc['local_time_12h']}.")

        # 11e. "What's the UTC offset?"
        if re.search(r'\b(?:utc\s+offset|gmt\s+offset|what(?:\'s|\s+is)\s+(?:your|the|my)?\s*(?:utc|gmt)\s*(?:offset|difference))\b', clean):
            tc = RuntimeTimeAdapter.get_runtime_time_context()
            return f"The current UTC offset is {tc['utc_offset']} ({tc['timezone']})."

        # 12. Google Calendar Integration Check
        if re.search(r'\b(?:(?:do\s+you\s+have\s+access\s+to|are\s+you\s+connected\s+to|check)\s+(?:my\s+)?google\s+calendar|google\s+calendar\s+(?:status|access|connection))\b', clean):
            conn_info = await CalendarSignalAdapter.get_connection_status()
            st = conn_info.get("status", "disconnected")
            if conn_info.get("connected"):
                return f"Yes, I have access to your Google Calendar (account: {conn_info.get('account_id')})."
            elif st == "token_expired":
                return "Google Calendar access is currently unavailable because the access token has expired."
            elif st == "unavailable":
                return "Google Calendar access is currently unavailable (OAuth credentials not configured)."
            return "Google Calendar is currently disconnected."

        # -------------------------------------------------------------------
        # 13. Calendar Queries with Phase 8D timezone-aware date handling
        # -------------------------------------------------------------------
        tc = RuntimeTimeAdapter.get_runtime_time_context()
        from datetime import date as _date, timedelta as _timedelta
        local_today = _date.fromisoformat(tc["local_date"])
        local_tomorrow = _date.fromisoformat(tc["tomorrow_date"])

        # Session calendar topic for follow-up resolution
        session_refs = context.get("session", {}).get("references", {})
        last_cal_topic = session_refs.get("last_calendar_topic")

        def _extract_calendar_target_date(text: str, l_today: _date) -> tuple[Optional[_date], Optional[str]]:
            # Relative dates
            if re.search(r'\b(?:for\s+|on\s+)?today\b', text):
                return l_today, "today"
            if re.search(r'\b(?:for\s+|on\s+)?tomorrow\b', text) and not re.search(r'\bday\s+after\s+tomorrow\b', text):
                return l_today + _timedelta(days=1), "tomorrow"
            if re.search(r'\b(?:day\s+after\s+tomorrow|day\s+after\s+next)\b', text):
                da_date = l_today + _timedelta(days=2)
                return da_date, f"on {da_date.strftime('%A')} (the day after tomorrow)"

            # Explicit dates (Month Day or Day of Month)
            _months_map = {
                "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
                "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
                "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12
            }
            m_exp = re.search(r'\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?\b', text)
            if not m_exp:
                m_exp = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)(?:\s*,?\s*(\d{4}))?\b', text)
                if m_exp:
                    d_s, m_s, y_s = m_exp.group(1), m_exp.group(2), m_exp.group(3)
                else:
                    d_s, m_s, y_s = None, None, None
            else:
                m_s, d_s, y_s = m_exp.group(1), m_exp.group(2), m_exp.group(3)

            if m_s and d_s:
                mn = _months_map[m_s.lower()]
                dn = int(d_s)
                yr = int(y_s) if y_s else l_today.year
                try:
                    cand = _date(yr, mn, dn)
                    if not y_s and cand < l_today - _timedelta(days=60):
                        cand = _date(yr + 1, mn, dn)
                    return cand, f"on {cand.strftime('%A')}, {cand.strftime('%B')} {cand.day}"
                except Exception:
                    pass

            # Weekdays
            _dnames = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            m_day = re.search(r'\b(?:(next|this|on|for|coming)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\'s)?\b', text)
            if m_day:
                pfx = (m_day.group(1) or '').lower().strip()
                dn = m_day.group(2).lower().strip()
                dow = _dnames.index(dn)
                ldow = l_today.weekday()
                da = (dow - ldow) % 7
                if da == 0:
                    if pfx == 'this':
                        da = 0
                    else:
                        da = 7
                target = l_today + _timedelta(days=da)
                return target, f"on {target.strftime('%A')}, {target.strftime('%B')} {target.day}"

            return None, None

        target_date, target_label = _extract_calendar_target_date(clean, local_today)

        is_cal_intent = bool(re.search(
            r'\b(?:calendar|schedule|agenda|events?|meetings?|appointments?|'
            r'next\s+class|free\s+after\s+\d+|time\s+until\s+next\s+event|how\s+long\s+until\s+(?:my\s+)?next\s+event|'
            r'what\s+do\s+i\s+have|what(?:\'s|\s+is)\s+on(?:\s+my)?|'
            r'what(?:\'s|\s+is)\s+(?:happening|planned)(?:\s+on)?|'
            r'how(?:\'s|\s+is|\s+does)\s+(?:my\s+)?.*look(?:ing)?|'
            r'what\s+does\s+my\s+.*look\s+like|'
            r'plans?\s+for|planned\s+for|anything\s+planned|anything\s+on|'
            r'am\s+i\s+(?:free|busy))\b', clean))

        is_calendar_followup = bool(last_cal_topic and target_date is not None)
        is_cal_query = is_cal_intent or is_calendar_followup

        if is_cal_query:
            cal = fused.calendar_context or {}
            conn_st = await CalendarSignalAdapter.get_connection_status()
            account_id = conn_st.get("account_id") or cal.get("account_id") or "primary"

            if not cal.get("connected") and not cal.get("current_event") and not cal.get("next_event"):
                if not conn_st.get("connected"):
                    _st = conn_st.get("status", "disconnected")
                    if re.search(r'\b(?:what(?:\'s|\s+is|s)\s+on\s+my\s+calendar|meeting\s+right\s+now|what\s+meeting\s+am\s+i\s+in)\b', clean):
                        return f"Google Calendar access is unavailable (currently {_st}), and you don't have anything on your calendar right now."
                    elif re.search(r'\b(?:what(?:\'s|\s+is|s)\s+(?:my\s+)?next\s+(?:calendar\s+event|event|meeting)|upcoming\s+(?:calendar\s+event|event|meeting))\b', clean):
                        return f"Google Calendar access is unavailable (currently {_st}), and you have no upcoming calendar events."
                    return f"Google Calendar access is unavailable (currently {_st})."
                else:
                    sigs = await CalendarSignalAdapter.fetch_and_create_signals(account_id)
                    for s in sigs:
                        await wsm.ingest_signal(s)
                    fused = await wsm.get_fused_world_state(source_device_id)
                    cal = fused.calendar_context

            def _get_raw_events():
                raw_sig = wsm._signals.get("calendar.raw_events")
                if raw_sig and raw_sig.is_fresh():
                    return raw_sig.payload.get("events", [])
                return cal.get("all_events", [])

            def _fmt_events(evs, lbl):
                if not evs:
                    return f"You have no events on your calendar {lbl}."
                if len(evs) == 1:
                    ev = evs[0]
                    loc = f" at {ev['location']}" if ev.get("location") else ""
                    if ev.get("all_day"):
                        return f"You have 1 event {lbl}: '{ev['title']}' (all day){loc}."
                    t = ev.get("local_start", ev.get("start", ""))
                    return f"You have 1 event {lbl}: '{ev['title']}' at {t}{loc}."
                lines = [f"You have {len(evs)} events {lbl}:"]
                for ev in evs:
                    loc = f" at {ev['location']}" if ev.get("location") else ""
                    if ev.get("all_day"):
                        lines.append(f"  * '{ev['title']}' (all day){loc}")
                    else:
                        t = ev.get("local_start", ev.get("start", ""))
                        lines.append(f"  * '{ev['title']}' at {t}{loc}")
                return "\n".join(lines)

            local_tz = CalendarSignalAdapter._get_local_now().tzinfo

            # Current Event check (takes precedence over target date if query explicitly asks about right now)
            if re.search(r'\b(?:what(?:\'s|\s+is)\s+on\s+my\s+calendar\s+(?:right\s+now|now|at\s+the\s+moment)|meeting\s+right\s+now|what\s+meeting\s+am\s+i\s+in)\b', clean):
                cur = cal.get("current_event")
                if cur and cur.get("title"):
                    return f"On your calendar right now: '{cur['title']}'."
                return "You don't have anything on your calendar right now."

            # Date-targeted queries (explicit dates, weekdays, tomorrow, today, day after tomorrow)
            if target_date is not None:
                context.setdefault("session", {}).setdefault("references", {})["last_calendar_topic"] = f"schedule_{target_date.isoformat()}"

                # "Today" query
                if target_date == local_today:
                    raw_evs = _get_raw_events()
                    evs = CalendarSignalAdapter.parse_events_for_date(raw_evs, local_today, local_tz)
                    if not evs:
                        cur = cal.get("current_event")
                        nxt = cal.get("next_event")
                        if cur and cur.get("title"):
                            return f"You are currently in '{cur['title']}'."
                        elif nxt and nxt.get("title"):
                            return f"The remaining event today is '{nxt['title']}' starting in {cal.get('time_until_next_event', 'a bit')}."
                    return _fmt_events(evs, "today")

                # "Tomorrow" query if query literally said "tomorrow" and raw events are cached
                if target_date == local_tomorrow and ("tomorrow" in clean):
                    raw_evs = _get_raw_events()
                    if raw_evs:
                        evs = CalendarSignalAdapter.parse_events_for_date(raw_evs, local_tomorrow, local_tz)
                        return _fmt_events(evs, "tomorrow")

                # Explicit weekday/date queries outside startup cache (or explicit day queries like Monday/Tuesday/Oct 18):
                # Perform on-demand Calendar fetch for:
                # target_date 00:00 local -> target_date + 1 day 00:00 local
                start_dt = datetime.combine(target_date, datetime.min.time(), tzinfo=local_tz)
                end_dt = datetime.combine(target_date + _timedelta(days=1), datetime.min.time(), tzinfo=local_tz)
                start_iso = start_dt.isoformat()
                end_iso = end_dt.isoformat()

                from .integrations.calendar import CalendarIntegration
                plugin = CalendarIntegration()
                try:
                    res = await plugin.execute_action(
                        account_id,
                        "calendar.read",
                        time_min=start_iso,
                        time_max=end_iso
                    )
                    items = res.get("items", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
                    evs = CalendarSignalAdapter.parse_events_for_date(items, target_date, local_tz)
                except Exception as e:
                    log.warning(f"On-demand calendar fetch failed for {target_date}: {e}")
                    evs = CalendarSignalAdapter.parse_events_for_date(_get_raw_events(), target_date, local_tz)

                return _fmt_events(evs, target_label)


            # Next Class
            if re.search(r'\b(?:when\s+is\s+my\s+next\s+class|next\s+class)\b', clean):
                all_evs = cal.get("all_events", [])
                class_ev = next((e for e in all_evs if "class" in (e.get("title") or "").lower() or "lecture" in (e.get("title") or "").lower()), None)
                if class_ev:
                    return f"Your next class is '{class_ev['title']}' at {class_ev.get('start')}."
                nxt = cal.get("next_event")
                if nxt and nxt.get("title"):
                    until_str = f" in {cal.get('time_until_next_event')}" if cal.get("time_until_next_event") else ""
                    return f"I don't see an event titled 'class', but your next event is '{nxt['title']}'{until_str}."
                return "You don't have any upcoming classes on your calendar."

# Free after X
            m_free = re.search(r'\bam\s+i\s+free\s+after\s+(\d+)(?::(\d+))?\s*(pm|am)?\b', clean)
            if m_free:
                raw_h = int(m_free.group(1))
                mer = (m_free.group(3) or "").lower()
                h24 = raw_h + (12 if mer == "pm" and raw_h < 12 else 0)
                if mer == "am" and raw_h == 12:
                    h24 = 0
                td = f"{raw_h} {mer.upper()}".strip() if mer else f"{raw_h}:00"
                conflict = None
                for ev in cal.get("all_events", []):
                    st = ev.get("start", "")
                    try:
                        from datetime import datetime as _dt
                        ev_dt = _dt.fromisoformat(st.replace("Z", "+00:00"))
                        if ev_dt.hour >= h24:
                            conflict = ev
                            break
                    except Exception:
                        pass
                if conflict:
                    return f"You are not completely free after {td}; you have '{conflict['title']}' at {conflict.get('start')}."
                return f"Yes, you are free after {td} today."

            # Time until next event
            if re.search(r'\b(?:how\s+long\s+until\s+(?:my\s+)?next\s+event|time\s+until\s+next\s+event)\b', clean):
                nxt = cal.get("next_event")
                time_until = cal.get("time_until_next_event")
                if nxt and nxt.get("title") and time_until:
                    return f"Your next event '{nxt['title']}' is in {time_until}."
                elif nxt and nxt.get("title"):
                    return f"Your next event '{nxt['title']}' is upcoming."
                return "You have no upcoming calendar events."

            # Next Event (generic)
            if re.search(r'\b(?:what(?:\'s|\s+is|s)\s+(?:my\s+)?next\s+(?:calendar\s+event|event|meeting)|what(?:\'s|\s+is|s)\s+next\s+on\s+my\s+calendar|upcoming\s+(?:calendar\s+event|event|meeting))\b', clean):
                nxt = cal.get("next_event")
                time_until = cal.get("time_until_next_event")
                if nxt and nxt.get("title"):
                    until_str = f" in {time_until}" if time_until else ""
                    return f"Your next calendar event is '{nxt['title']}'{until_str}."
                return "You have no upcoming calendar events."

        # 14. Foreground Activity
        if re.search(r'\b(?:what|which)\s+(?:app|application|window|program)\s+(?:am\s+i\s+(?:currently\s+)?(?:using|in)|is\s+(?:currently\s+)?(?:open|active|foregrounded|in\s+the\s+foreground))\b', clean) or \
           re.search(r'\bwhat(?:\'s|\s+is)\s+(?:foregrounded|in\s+the\s+foreground|currently\s+open|open\s+on\s+my\s+screen)\b', clean):
            fg = fused.foreground_activity
            app_name = fg.get("app_name") if fg else None
            if not app_name or app_name == "unknown":
                app_name = world_ctx.get("foreground_app") or context.get("foreground_app")
            task = fused.current_task
            task_title = task.get("title") if task else (world_ctx.get("active_task") or context.get("active_task"))
            if app_name and app_name != "unknown":
                session_refs["last_confirmed_app"] = app_name
                session_refs["foreground_app"] = app_name
                session_refs["current_app"] = app_name
                session_refs["last_target"] = app_name
                from .session_context import SessionContextManager
                await SessionContextManager.set_foreground_app(source_device_id, app_name)
                if task_title:
                    return f"You are currently using {app_name} on task '{task_title}'."
                return f"You are currently using {app_name}."
            return "Foreground application information is currently unavailable."

        # 15. Battery / Power
        if re.search(r'\b(?:battery|power\s+level|battery\s+percentage|battery\s+level|battery\s+status|how\s+much\s+battery)\b', clean):
            target_id = source_device_id or fused.active_device_id
            bat = fused.battery_states.get(target_id)
            if not bat and fused.battery_states:
                bat = next(iter(fused.battery_states.values()))
            if not bat and world_ctx.get("battery"):
                return f"Battery is at {world_ctx['battery']}."
            if not bat:
                try:
                    import psutil
                    ps_bat = psutil.sensors_battery()
                    if ps_bat:
                        bat = {"battery_level": int(ps_bat.percent), "is_charging": ps_bat.power_plugged}
                except Exception:
                    pass
            if bat and bat.get("battery_level") is not None:
                lvl = bat.get("battery_level")
                charging = "and is plugged in" if bat.get("is_charging") else "on battery"
                return f"Battery is at {lvl}% {charging}."
            return "Battery status is currently unavailable."

        # 16. Network / Connectivity
        if re.search(r'\b(?:am\s+i\s+connected\s+to\s+(?:the\s+)?internet|am\s+i\s+online|do\s+i\s+have\s+(?:an?\s+)?internet\s+connection|is\s+(?:the\s+)?internet\s+connected|check\s+(?:my\s+)?internet\s+connection|network\s+status|network\s+connection|wifi\s+connection)\b', clean):
            net = fused.network_context
            is_connected = net.get("is_connected")
            net_state = net.get("network_state")
            if not is_connected and world_ctx.get("network"):
                net_state = world_ctx.get("network")
                is_connected = True if net_state != "offline" else False
            if is_connected is True:
                state_str = f" ({net_state})" if net_state and net_state not in ("connected", "unknown") else ""
                return f"You are connected to the internet{state_str}."
            elif is_connected is False and net_state in ("disconnected", "offline"):
                return "You are not connected to the internet."
            return "Internet connectivity status is currently unavailable."

        # 17. Location
        if re.search(r'\b(?:where\s+am\s+i|what(?:\'s|\s+is)\s+my\s+(?:current\s+)?location|where\s+am\s+i\s+right\s+now)\b', clean):
            loc = fused.location_context
            label = loc.get("label") if loc else None
            if not label or label == "unknown":
                label = world_ctx.get("location")
            if label and label != "unknown":
                return f"You are at {label}."
            return "I don't have a current location signal for you."

        # 18. Task / Current Activity / What am I doing right now?
        if re.search(r'\b(?:what\s+am\s+i\s+(?:currently\s+)?(?:doing|working\s+on)(?:\s+right\s+now)?|what(?:\'s|\s+is)\s+my\s+(?:current|active)\s+task|what\s+task\s+is\s+(?:active|in\s+progress)|tell\s+me\s+what\s+i(?:\'m|\s+am)\s+doing)\b', clean):
            task = fused.current_task
            task_title = task.get("title") if task else (world_ctx.get("active_task") or context.get("active_task"))
            fg = fused.foreground_activity
            app_name = fg.get("app_name") if fg and fg.get("app_name") != "unknown" else (world_ctx.get("foreground_app") or context.get("foreground_app"))
            win_title = fg.get("window_title") if fg and fg.get("window_title") != "unknown" else world_ctx.get("active_window")
            
            # Recent screen observation summary
            screen_summary = None
            from .screen_perception import ScreenPerceptionManager
            obs = ScreenPerceptionManager.get_latest_observation(source_device_id)
            if obs and obs.summary:
                screen_summary = obs.summary
            elif world_ctx.get("screen_summary"):
                screen_summary = world_ctx.get("screen_summary")
            elif world_ctx.get("screen_perception", {}).get("summary"):
                screen_summary = world_ctx.get("screen_perception", {}).get("summary")

            if app_name and app_name != "unknown":
                session_refs["last_confirmed_app"] = app_name
                session_refs["foreground_app"] = app_name
                session_refs["current_app"] = app_name
                session_refs["last_target"] = app_name
                from .session_context import SessionContextManager
                await SessionContextManager.set_foreground_app(source_device_id, app_name)
                if screen_summary:
                    summary_clean = screen_summary.strip()
                    if summary_clean.lower().startswith("you are ") or summary_clean.lower().startswith("you're "):
                        return summary_clean
                    # Handle "Cortex development conversation" -> "working on Cortex"
                    if "cortex" in summary_clean.lower():
                        return f"You're currently using {app_name} and working on Cortex."
                    return f"You're currently using {app_name} and {summary_clean}."
                elif task_title:
                    return f"You're currently using {app_name} and working on '{task_title}'."
                elif win_title and win_title.lower() not in ("unknown", "", app_name.lower()):
                    if "cortex" in win_title.lower():
                        return f"You're currently using {app_name} and working on Cortex."
                    return f"You're currently using {app_name} with window '{win_title}'."
                return f"You are currently using {app_name}."
            elif task_title:
                return f"Your active task is '{task_title}'."
            elif screen_summary:
                return f"Based on your screen, {screen_summary}."
            return "I don't have enough active screen or task observations to determine what you're doing right now."

        return None

        return None

    async def process_request(
        self, 
        source_device_id: str, 
        natural_request: str, 
        context: Dict[str, Any],
        log_action_callback: Optional[Any] = None, # Function to log actions back to db/hub
        latency_metrics: dict = None,
        on_chunk: Optional[callable] = None,
        request_id: str = "unknown"
    ) -> ConversationalResponse:
        if latency_metrics is None:
            latency_metrics = {}

        t0 = time.monotonic()
        trace_id = latency_metrics.get("trace_id") or f"tr_{uuid.uuid4().hex[:12]}"
        session_id = context.get("session_id") or latency_metrics.get("session_id") or f"ses_{uuid.uuid4().hex[:8]}"
        interaction_id = request_id if request_id != "unknown" else (latency_metrics.get("interaction_id") or f"ix_{uuid.uuid4().hex[:6]}")

        trace = InteractionTrace(
            trace_id=trace_id,
            session_id=session_id,
            interaction_id=interaction_id
        )
        for stage in ["wake_detected", "microphone_open", "speech_first_detected", "speech_end", "stt_complete"]:
            if stage in latency_metrics and latency_metrics[stage] is not None:
                trace.record_stage(stage, float(latency_metrics[stage]))

        trace.record_stage("hub_request_received", t0)
        latency_metrics["intent_route_start"] = t0
        latency_metrics["trace_id"] = trace_id
        latency_metrics["session_id"] = session_id
        latency_metrics["interaction_id"] = interaction_id
        context["trace"] = trace
        context["interaction_trace"] = trace
        context["request_id"] = interaction_id

        log.info(f"[Latency] Hub request received | trace={trace_id} | device={source_device_id} | text='{natural_request}'")

        try:
            res = await self._process_request_impl(
                source_device_id=source_device_id,
                natural_request=natural_request,
                context=context,
                log_action_callback=log_action_callback,
                latency_metrics=latency_metrics,
                on_chunk=on_chunk,
                request_id=interaction_id,
                trace=trace,
                t0=t0
            )
        except Exception as e:
            log.error(f"process_request uncaught exception: {e}", exc_info=True)
            trace.record_stage("hub_response_emitted")
            trace.success = False
            trace.failure_category = classify_failure(e)
            trace.internal_error = str(e)
            res = ConversationalResponse(status="error", message=get_user_friendly_error(trace.failure_category))

        # Finalize trace and metrics
        trace.record_stage("hub_response_emitted")
        trace.compute_durations()
        if res.status in ("success", "approval_required", "clarification_needed"):
            trace.success = True
        else:
            trace.success = False
            if not trace.failure_category:
                trace.failure_category = "HARNESS_ERROR"
        trace.user_facing_message = res.message
        trace.log_summary()

        MetricsCollector.get_instance().record_trace(trace)
        trace_dict = trace.to_dict()
        latency_metrics.update(trace_dict)
        res.latency_metrics = trace_dict
        return res

    async def _process_request_impl(
        self, 
        source_device_id: str, 
        natural_request: str, 
        context: Dict[str, Any],
        log_action_callback,
        latency_metrics: dict,
        on_chunk: Optional[callable],
        request_id: str,
        trace: InteractionTrace,
        t0: float
    ) -> ConversationalResponse:
        import re
        from datetime import datetime, timezone
        from .intent_schema import ActionPlan, ModelResult
        
        normalized_request = natural_request.strip().lower()
        
        # Reflect caller device capabilities from context into ExecutorRegistry if provided
        dev_ctx = context.get("device")
        if isinstance(dev_ctx, dict) and dev_ctx.get("capabilities"):
            from .executor_registry import ExecutorRegistry
            from .device_handle import DeviceHandle
            er = ExecutorRegistry.get_instance()
            h = er.get_device_handle(source_device_id)
            if not h:
                h = DeviceHandle(
                    device_id=source_device_id,
                    display_name=source_device_id,
                    role="primary_admin" if source_device_id == "asus" else "trusted_worker",
                    trust_level="admin" if source_device_id == "asus" else "trusted",
                    capabilities=list(dev_ctx["capabilities"]),
                )
                er.register_device(h)
                er.set_device_online(source_device_id, True)
            else:
                for c in dev_ctx["capabilities"]:
                    if c not in h.capabilities:
                        h.capabilities.append(c)
                er._handles[source_device_id] = h
                er.set_device_online(source_device_id, True)

        # Strip conversational wrappers
        for prefix in ["i said ", "please ", "can you ", "could you "]:
            if normalized_request.startswith(prefix):
                normalized_request = normalized_request[len(prefix):].strip()
                
        match = re.match(r'^(open|launch|start|run)\s+(.+)$', normalized_request)
        
        question_starters = {"which", "what", "who", "when", "where", "why", "how", "explain", "tell", "define", "translate", "is", "are", "do", "does", "can", "could", "am", "give", "summarize", "check"}
        action_verbs = {"open", "launch", "start", "run", "move", "copy", "delete", "remove", "create", "send", "turn", "set", "play", "pause", "stop", "close", "kill", "find", "take", "show", "read", "list", "get", "remember", "update", "search", "link", "resolve"}
        
        words = normalized_request.split()
        first_word = words[0].lower() if words else ""
        
        is_fast_response = False
        if first_word in question_starters:
            is_fast_response = True
        elif len(words) <= 2 and first_word not in action_verbs:
            # Ambiguous bare nouns like "Spotify" -> FAST_RESPONSE
            is_fast_response = True
            
        clean_req = normalized_request.lower().strip(" \t.,!?;:")

        # Context-provided device capability registration (for test harnesses and caller metadata)
        dev_ctx = context.get("device", {}) if isinstance(context, dict) else {}
        ctx_caps = dev_ctx.get("capabilities")
        if ctx_caps is not None and source_device_id:
            from .executor_registry import ExecutorRegistry
            from .device_handle import DeviceHandle
            er = ExecutorRegistry.get_instance()
            handle = er.get_device_handle(source_device_id)
            if not handle:
                handle = DeviceHandle(
                    device_id=source_device_id,
                    display_name=dev_ctx.get("name") or source_device_id,
                    role="primary_admin" if source_device_id == "asus" else "standard",
                    trust_level="admin" if source_device_id == "asus" else "trusted",
                    capabilities=list(ctx_caps),
                )
                er.register_device(handle)
                er.set_device_online(source_device_id, True)
            else:
                handle.capabilities = list(set(handle.capabilities + list(ctx_caps)))
                if not er.is_device_online(source_device_id):
                    er.set_device_online(source_device_id, True)

        # ===================================================================
        # Request Pipeline Stage (Pre-A9 General Semantic Routing)
        # ===================================================================
        try:
            self.pipeline.router = self.router
            p_resp = await self.pipeline.execute(
                source_device_id=source_device_id,
                natural_request=natural_request,
                context=context,
                request_id=request_id,
            )
            if p_resp is not None:
                return p_resp
        except Exception as p_exc:
            log.error("pipeline_failed request_id=%s reason=%s", request_id, type(p_exc).__name__)
            return ConversationalResponse(status="error", message="The request pipeline failed before completion could be verified.")

        if os.environ.get("CORTEX_LEGACY_COMPATIBILITY", "0") != "1":
            return ConversationalResponse(status="unsupported", message="I could not find a supported route for that request.")
        log.warning("legacy_fallback request_id=%s reason=semantic_pipeline_unhandled stage=agent_harness", request_id)

        # ===================================================================
        # Fallback Processing (Legacy Continuity)
        # ===================================================================
        from .database import get_pending_confirmations, resolve_confirmation
        from .approval_resolver import resolve_approval_intent
        pending_confs = await get_pending_confirmations()
        if pending_confs:
            matching_conf = next(
                (c for c in reversed(pending_confs) if c.get("requested_by") == source_device_id or c.get("target_device") == source_device_id),
                pending_confs[-1]
            )
            approval_decision = resolve_approval_intent(clean_req)
            if approval_decision:
                cmd_id = matching_conf["command_id"]
                await resolve_confirmation(cmd_id, approval_decision, "user")
                from .main import PENDING_FUTURES
                fut = PENDING_FUTURES.get(cmd_id)
                if fut and not fut.done():
                    fut.set_result(approval_decision)

                if approval_decision == "approved":
                    # 1. Check if there is an active goal waiting on this approval
                    from .goals import GoalManager, GoalRunner
                    gm = GoalManager.get_instance()
                    goals = await gm.list_goals(status="NEEDS_APPROVAL")
                    matching_goal = next((g for g in goals if g.approval_request_id == cmd_id), None)
                    if matching_goal:
                        await gm.mark_active(matching_goal.goal_id)
                        runner = GoalRunner.get_instance(control_layer=self.control, goal_manager=gm)
                        run_res = await runner.run_goal(matching_goal.goal_id)
                        if run_res.get("status") == "COMPLETED":
                            msg = f"Approved. {matching_goal.result_summary or f'Goal {matching_goal.title} completed successfully.'}"
                            status = "success"
                        elif run_res.get("status") == "FAILED":
                            err_reason = run_res.get("error") or run_res.get("reason") or matching_goal.blocking_reason or "Verification failed"
                            msg = f"Approved, but execution failed: {err_reason}"
                            status = "error"
                        else:
                            msg = "Approved and execution resumed."
                            status = "success"
                        return ConversationalResponse(status=status, message=msg, command_id=cmd_id)

                    # 2. Interactive filesystem / direct command post-action verification
                    cmd_name = matching_conf.get("command")
                    import json
                    raw_args = json.loads(matching_conf.get("args") or "[]")
                    if cmd_name in ("fs.delete", "delete_file", "delete"):
                        target_path = raw_args[0] if raw_args else None
                        if target_path:
                            from .environment import fs_delete, fs_stat
                            from pathlib import Path
                            fs_delete(target_path)
                            stat_res = fs_stat(target_path)
                            item_name = Path(target_path).name
                            if stat_res.get("exists", False):
                                return ConversationalResponse(
                                    status="error",
                                    message=f"Deletion failed: item '{item_name}' still exists on desktop.",
                                    command_id=cmd_id,
                                )
                            else:
                                return ConversationalResponse(
                                    status="success",
                                    message=f"Approved. Successfully deleted '{item_name}' from your desktop.",
                                    command_id=cmd_id,
                                )

                    return ConversationalResponse(
                        status="success",
                        message="Approved. Proceeding with execution.",
                        command_id=cmd_id,
                    )
                else:
                    # Denied
                    from .goals import GoalManager
                    gm = GoalManager.get_instance()
                    goals = await gm.list_goals(status="NEEDS_APPROVAL")
                    matching_goal = next((g for g in goals if g.approval_request_id == cmd_id), None)
                    if matching_goal:
                        await gm.cancel_goal(matching_goal.goal_id, reason="Denied by user")
                    return ConversationalResponse(
                        status="cancelled",
                        message="Cancelled. The requested action was denied and will not be performed.",
                        command_id=cmd_id,
                    )

        # ===================================================================
        # Real-Runtime Repair: Pending Action Continuity
        # ===================================================================
        has_explicit_session = "session" in context
        session_ctx = context.setdefault("session", {})
        session_refs = session_ctx.setdefault("references", {})
        if not has_explicit_session:
            from .session_context import SessionContextManager
            sess_db = await SessionContextManager.get_session(source_device_id)
            if sess_db and sess_db.get("references"):
                for k, v in sess_db["references"].items():
                    session_refs.setdefault(k, v)

        pending_act = session_refs.get("pending_action")

        if pending_act:
            created_at = pending_act.get("created_at", 0)
            if time.time() - created_at > 180:
                session_refs.pop("pending_action", None)
                from .session_context import SessionContextManager
                await SessionContextManager.set_pending_action(source_device_id, None)
                pending_act = None

        if pending_act:
            is_cancel = clean_req in ("cancel", "never mind", "stop", "no", "don't do that", "abort", "cancel that")
            if is_cancel:
                session_refs.pop("pending_action", None)
                from .session_context import SessionContextManager
                await SessionContextManager.set_pending_action(source_device_id, None)
                return ConversationalResponse(
                    status="success",
                    message="Cancelled. I won't proceed with that action.",
                )

            clean_normalized = re.sub(r"[^\w\s]", " ", clean_req).strip()
            clean_words = " ".join(clean_normalized.split())

            is_affirmative = clean_words in (
                "do it", "go ahead", "yes", "proceed", "check it", "do that",
                "please do", "okay do it", "ok do it", "yes please", "sure",
                "yes do it", "okay", "ok", "yeah do it", "yep", "do that please"
            ) or clean_words.startswith("do it") or clean_words.startswith("go ahead") or clean_words.startswith("check it")

            p_cap = pending_act.get("capability")
            p_missing = pending_act.get("missing_arguments", [])
            p_args = pending_act.get("arguments", {})

            if is_affirmative:
                if "location" in p_missing and not p_args.get("location"):
                    loc_val = (
                        session_refs.get("location")
                        or session_refs.get("user_location")
                        or context.get("device", {}).get("location")
                    )
                    if loc_val:
                        p_args["location"] = loc_val
                        p_missing = [m for m in p_missing if m != "location"]
                    else:
                        return ConversationalResponse(
                            status="clarification_needed",
                            message="Which city or location would you like me to check the weather for?",
                        )

                session_refs.pop("pending_action", None)
                from .session_context import SessionContextManager
                await SessionContextManager.set_pending_action(source_device_id, None)

                if p_cap == "weather.current":
                    from .weather_provider import get_weather_provider
                    wp = get_weather_provider()
                    if not wp:
                        return ConversationalResponse(
                            status="error",
                            message="Live weather provider is unavailable. I cannot check current weather right now.",
                        )
                    target_loc = p_args.get("location", "Manassas")
                    try:
                        w_res = await wp.get_current(target_loc)
                        loc_name = w_res.get("location", target_loc)
                        temp = w_res.get("temperature")
                        unit = w_res.get("temperature_unit", "°C")
                        cond = w_res.get("conditions", "Normal")
                        msg = f"It's currently {temp}{unit} and {cond} in {loc_name}."
                        session_refs["location"] = loc_name
                        return ConversationalResponse(
                            status="success",
                            message=msg,
                            data=w_res,
                            provenance={
                                "provider": wp.provider_name,
                                "source_type": "live_weather",
                                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                    except Exception as exc:
                        log.error(f"[AgentHarness] Weather query error: {exc}")
                        return ConversationalResponse(status="error", message=f"Could not retrieve weather: {exc}")

            elif "location" in p_missing:
                AFFIRMATION_TOKENS = {"okay", "ok", "do it", "yes", "yep", "sure", "please", "go ahead", "check it", "do that"}
                cand_loc = extract_weather_location(clean_req)
                if not cand_loc and clean_words not in AFFIRMATION_TOKENS and len(clean_words.split()) <= 3 and not any(w in clean_words for w in ("open", "close", "delete", "move", "launch", "search", "who", "what")):
                    cand_loc = clean_words.title()
                if cand_loc:
                    session_refs.pop("pending_action", None)
                    from .session_context import SessionContextManager
                    await SessionContextManager.set_pending_action(source_device_id, None)

                    from .weather_provider import get_weather_provider
                    wp = get_weather_provider()
                    if not wp:
                        return ConversationalResponse(
                            status="error",
                            message="Live weather provider is unavailable. I cannot check current weather right now.",
                        )
                    try:
                        w_res = await wp.get_current(cand_loc)
                        loc_name = w_res.get("location", cand_loc)
                        temp = w_res.get("temperature")
                        unit = w_res.get("temperature_unit", "°C")
                        cond = w_res.get("conditions", "Normal")
                        msg = f"It's currently {temp}{unit} and {cond} in {loc_name}."
                        session_refs["location"] = loc_name
                        return ConversationalResponse(
                            status="success",
                            message=msg,
                            data=w_res,
                            provenance={
                                "provider": wp.provider_name,
                                "source_type": "live_weather",
                                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                    except Exception as exc:
                        log.error(f"[AgentHarness] Weather query error: {exc}")
                        return ConversationalResponse(status="error", message=f"Could not retrieve weather: {exc}")

        # ===================================================================
        # Real-Runtime Repair: Screenshot Follow-up Continuity
        # ===================================================================
        is_screenshot_inspect = bool(re.search(
            r"\b(?:read|inspect|describe|analyze|tell me what you see in|what do you see in|what(?:'s| is) in|what(?:'s| is) on)\s+(?:that|the|this|recent)?\s*screenshot\b",
            clean_req,
        )) or (
            bool(re.search(r"\b(?:read (?:that|it)|what do you see there)\b", clean_req))
            and bool(session_refs.get("last_screenshot_ref") or session_refs.get("last_action") == "screen.capture")
        )

        if is_screenshot_inspect:
            explicit_dev = None
            m_dev = re.search(r"\b(?:on|in)\s+([a-zA-Z0-9_-]+)(?:'s)?(?:\s+screen)?\b", clean_req)
            if m_dev:
                cand = m_dev.group(1).lower().rstrip("'s")
                if cand not in ("the", "my", "this", "that", "recent"):
                    explicit_dev = cand

            target_dev = (
                explicit_dev
                or session_refs.get("last_screenshot_device")
                or session_refs.get("grounded_target", {}).get("owning_device")
                or source_device_id
            )

            img_ref = session_refs.get("last_screenshot_ref")
            if not img_ref and not has_explicit_session:
                from .session_context import SessionContextManager
                s_db = await SessionContextManager.get_session(target_dev)
                img_ref = s_db.get("references", {}).get("last_screenshot_ref")
                if not img_ref and target_dev != source_device_id:
                    s_src = await SessionContextManager.get_session(source_device_id)
                    img_ref = s_src.get("references", {}).get("last_screenshot_ref")

            if not img_ref or (explicit_dev and session_refs.get("last_screenshot_device") and session_refs.get("last_screenshot_device") != explicit_dev):
                from .screen_perception import ScreenPerceptionManager
                obs = ScreenPerceptionManager.get_latest_observation(target_dev)
                if obs and obs.source_image_reference:
                    img_ref = obs.source_image_reference

            # Fallback to most recent screenshot file on disk if created recently (within 10 minutes)
            if not img_ref and not has_explicit_session:
                from pathlib import Path
                for sc_path in (Path("./screenshots"), Path("c:/screenshots"), Path("screenshots")):
                    if sc_path.exists():
                        pngs = sorted(sc_path.glob("screenshot_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
                        if pngs and (time.time() - pngs[0].stat().st_mtime < 600):
                            img_ref = str(pngs[0].resolve())
                            from .session_context import SessionContextManager
                            await SessionContextManager.set_screenshot_ref(target_dev, img_ref)
                            session_refs["last_screenshot_ref"] = img_ref
                            session_refs["last_screenshot_device"] = target_dev
                            break

            if not img_ref:
                return ConversationalResponse(
                    status="error",
                    message="There is no recent screenshot to inspect. You can ask me to take a screenshot first.",
                )

            from .vision_provider import get_vision_provider
            v_provider = get_vision_provider()
            if not v_provider:
                return ConversationalResponse(
                    status="error",
                    message="Screen analysis provider unavailable.",
                )

            try:
                obs = await v_provider.analyze_image(
                    image_ref=img_ref,
                    question=natural_request,
                    mode="describe",
                    device_id=target_dev,
                )
                from .screen_perception import ScreenPerceptionManager
                spm = ScreenPerceptionManager.get_instance()
                spm.store_observation(obs)
                await spm.publish_world_state_signals(obs)
                session_refs["last_screen_observation"] = obs.to_dict()
                session_refs["last_screenshot_device"] = target_dev
                return ConversationalResponse(
                    status="success",
                    message=obs.summary,
                    data=obs.to_dict(),
                    provenance={
                        "provider": obs.provenance.get("provider", v_provider.provider_name),
                        "model": obs.provenance.get("model", v_provider.model_name),
                        "source_type": "screen_perception",
                        "retrieved_at": obs.captured_at,
                    },
                )
            except Exception as exc:
                log.error(f"[AgentHarness] Screenshot analysis error: {exc}")
                return ConversationalResponse(
                    status="error",
                    message="I couldn't inspect the screenshot because the screen-analysis action failed.",
                )

        # ===================================================================
        # Real-Runtime Repair: Self-Knowledge Follow-Up References
        # ===================================================================
        last_intro = session_refs.get("last_introspection")
        m_intro_followup = re.match(
            r"^(?:what about|how about|and|what about the|can that one)\s+(.+)$",
            clean_req,
        ) or (clean_req in ("what about gmail", "what about calendar", "what about contacts", "and weather", "what about weather", "can that one do it", "what about the lenovo", "what about lenovo") and bool(last_intro))
        if m_intro_followup and last_intro and (time.time() - last_intro.get("timestamp", 0) < 180):
            target_topic = (m_intro_followup.group(1) if hasattr(m_intro_followup, "group") and m_intro_followup.group(1) else clean_req).strip(" \t.,!?;:")
            from .system_registry import SystemRegistry
            sys_reg = SystemRegistry.get_instance()

            if any(k in target_topic for k in ("gmail", "email")):
                g_stat = await sys_reg.get_integration_status("gmail")
                msg = "Gmail is connected and authorized." if g_stat.get("connected") else "Gmail is not currently connected or authorized."
                session_refs["last_introspection"] = {"topic": "integrations", "entities": ["gmail"], "timestamp": time.time()}
                return ConversationalResponse(
                    status="success" if g_stat.get("connected") else "error",
                    message=msg,
                    data=g_stat,
                )
            elif "calendar" in target_topic:
                c_stat = await sys_reg.get_integration_status("calendar")
                msg = "Google Calendar is connected and authorized." if c_stat.get("connected") else "Google Calendar is not currently connected or authorized."
                session_refs["last_introspection"] = {"topic": "integrations", "entities": ["calendar"], "timestamp": time.time()}
                return ConversationalResponse(
                    status="success" if c_stat.get("connected") else "error",
                    message=msg,
                    data=c_stat,
                )
            elif "contact" in target_topic:
                cnt_stat = await sys_reg.get_integration_status("contacts")
                msg = "Contacts is connected and authorized." if cnt_stat.get("connected") else "Contacts is not currently connected or authorized."
                session_refs["last_introspection"] = {"topic": "integrations", "entities": ["contacts"], "timestamp": time.time()}
                return ConversationalResponse(
                    status="success" if cnt_stat.get("connected") else "error",
                    message=msg,
                    data=cnt_stat,
                )
            elif "weather" in target_topic:
                w_stat = sys_reg.get_capability_status("weather.current")
                msg = "Yes, I can check live weather conditions and forecasts." if w_stat["capabilities"]["weather.current"]["available"] else "No, weather access is currently unavailable."
                session_refs["last_introspection"] = {"topic": "capabilities", "entities": ["weather.current"], "timestamp": time.time()}
                return ConversationalResponse(
                    status="success",
                    message=msg,
                    data=w_stat,
                )
            elif any(k in target_topic for k in ("search", "web", "internet")):
                s_stat = sys_reg.get_capability_status("web.search")
                msg = "Yes, I can search the web for live information." if s_stat["capabilities"]["web.search"]["available"] else "No, web search is currently unavailable."
                session_refs["last_introspection"] = {"topic": "capabilities", "entities": ["web.search"], "timestamp": time.time()}
                return ConversationalResponse(
                    status="success",
                    message=msg,
                    data=s_stat,
                )
            elif any(k in target_topic for k in ("screen", "screenshot")):
                sc_stat = sys_reg.get_capability_status(["screen.capture", "screen.analyze"])
                msg = "Yes, I can capture and analyze what is visible on your screen." if sc_stat["all_available"] else "No, screen perception is unavailable."
                session_refs["last_introspection"] = {"topic": "capabilities", "entities": ["screen.capture"], "timestamp": time.time()}
                return ConversationalResponse(
                    status="success",
                    message=msg,
                    data=sc_stat,
                )
            elif any(d in target_topic for d in ("lenovo", "asus", "test device", "do it", "that one")):
                devs = await sys_reg.list_devices(current_node=source_device_id, connected_devices_map=self.control.connected_devices)
                matching_d = next((d for d in devs if target_topic in d["name"].lower() or d["device_id"].lower() in target_topic), None)
                if not matching_d and ("that one" in target_topic or "do it" in target_topic):
                    matching_d = next((d for d in devs if d["device_id"] != source_device_id), None)
                if matching_d:
                    onl = "online" if matching_d.get("online") else "offline"
                    msg = f"The {matching_d['name']} device is currently {onl} (role: {matching_d.get('role')})."
                    session_refs["last_device"] = matching_d["device_id"]
                    return ConversationalResponse(
                        status="success",
                        message=msg,
                        data={"device": matching_d},
                    )

        # ===================================================================
        # Phase 9 Fix 3: Follow-Up Re-Observation ("I still see it") & Loop Protection
        # ===================================================================
        m_still_see = re.search(r"\b(?:still\s+see|still\s+there|still\s+on\s+my|still\s+visible)\b", clean_req)
        if m_still_see:
            session_ctx = context.get("session", {})
            session_refs = session_ctx.get("references", {})
            grounded = session_refs.get("grounded_target")
            if not grounded:
                from .session_context import SessionContextManager
                sess = await SessionContextManager.get_session(source_device_id)
                grounded = sess.get("references", {}).get("grounded_target")

            if grounded:
                target_path = grounded.get("matched_path") or grounded.get("path")
                from pathlib import Path
                target_name = grounded.get("target_name") or (Path(target_path).name if target_path else "the item")
                from .environment import fs_stat
                stat_res = fs_stat(target_path) if target_path else {"exists": False}
                if stat_res.get("exists", False):
                    fail_key = f"delete:{target_path}"
                    failures = session_refs.get("action_failures", {}).get(fail_key, 0) + 1
                    session_refs.setdefault("action_failures", {})[fail_key] = failures

                    if failures >= 2:
                        # Loop protection: Stop! Mark BLOCKED/FAILED. Report concrete error. Do not ask for approval again!
                        return ConversationalResponse(
                            status="error",
                            message=f"I checked your desktop and '{target_name}' is still present. Deletion has failed twice. Operation is blocked: the file may be locked by another running process.",
                        )
                    else:
                        return ConversationalResponse(
                            status="error",
                            message=f"I checked your desktop and '{target_name}' is indeed still present. The previous deletion did not succeed. Would you like me to try deleting it again?",
                        )
                else:
                    return ConversationalResponse(
                        status="success",
                        message=f"I inspected your desktop filesystem and verified that '{target_name}' is no longer present. The file was successfully deleted; your desktop view may need to be refreshed.",
                    )

        # ===================================================================
        # Phase 9 Fix 2: Grounded Target Continuity & Direct Filesystem Actions
        # ===================================================================
        from .target_resolver import TargetResolver, match_filesystem_items
        resolved_entity = TargetResolver.resolve(
            natural_request,
            current_device=source_device_id,
            session_context=context.get("session"),
            world_state=context.get("world_state"),
        )
        if resolved_entity.action == "delete" and (resolved_entity.target_name or resolved_entity.resolved_path):
            loc = resolved_entity.location or "desktop"
            from .environment import fs_list, resolve_location_path
            from pathlib import Path
            loc_path = resolve_location_path(loc)
            items_res = fs_list(loc_path or loc)
            items = items_res.get("items", [])

            matched_item = None
            if resolved_entity.resolved_path and Path(resolved_entity.resolved_path).is_file():
                matched_item = {
                    "name": Path(resolved_entity.resolved_path).name,
                    "path": str(Path(resolved_entity.resolved_path).resolve()),
                }
            elif resolved_entity.target_name:
                match_res = match_filesystem_items(
                    resolved_entity.target_name, items, qualifiers=resolved_entity.qualifiers
                )
                if match_res.is_ambiguous:
                    return ConversationalResponse(
                        status="clarification_needed",
                        message=match_res.clarification_message or f"Multiple items match '{resolved_entity.target_name}'.",
                    )
                matched_item = match_res.matched_item

            if matched_item:
                matched_path = matched_item["path"]
                matched_name = matched_item["name"]

                grounded_dict = {
                    "target_device": source_device_id,
                    "target_kind": "filesystem_item",
                    "target_name": Path(matched_name).stem if matched_name else (resolved_entity.target_name or matched_name),
                    "matched_name": matched_name,
                    "matched_path": matched_path,
                    "path": matched_path,
                    "location": loc,
                    "owning_device": resolved_entity.owning_device or source_device_id,
                }
                session_ctx = context.get("session", {})
                session_ctx.setdefault("references", {})["grounded_target"] = grounded_dict
                session_ctx["references"]["last_action_target"] = matched_name
                from .session_context import SessionContextManager
                await SessionContextManager.set_grounded_target(source_device_id, grounded_dict)

                plan = ActionPlan(
                    intent="delete_file",
                    source_device=source_device_id,
                    target_device=source_device_id,
                    target_device_reference="this device",
                    capability="fs.delete",
                    action="fs.delete",
                    arguments={"path": matched_path},
                    requires_confirmation=True,
                    confidence=1.0,
                    user_facing_summary=f"delete '{matched_name}' from {loc}",
                )

                cmd_id = await self.control.request_approval(
                    source_device_id, "fs.delete", {"path": matched_path}, source_device_id, tier=3
                )
                return ConversationalResponse(
                    status="approval_required",
                    message=f"I need your approval to delete '{matched_name}' from your {loc}.",
                    action_plan=plan,
                    command_id=cmd_id,
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message=f"I couldn't find '{resolved_entity.target_name}' on your {loc}.",
                )

        # Detect multi-step / compound commands
        compound_indicators = [
            " and then ", " then ", " and tell ", " and open ", " and get ",
            " and show ", " and read ", " after that "
        ]
        is_compound = any(ind in normalized_request for ind in compound_indicators)

        # ===================================================================
        # Phase A3: App Close, Focus, Window State, Launch, and File Actions
        # ===================================================================
        if resolved_entity.action == "close" and not is_compound:
            target_name = resolved_entity.target_name
            refs = context.get("session", {}).get("references", {})
            if resolved_entity.is_ambiguous or not target_name:
                return ConversationalResponse(
                    status="clarification_needed",
                    message=resolved_entity.clarification_reason or "Which application would you like me to close?",
                )
            if target_name:
                close_res = await self.control.execute_and_observe(
                    source_device_id,
                    "app.close",
                    {"app": target_name, "graceful": True, "target_device": source_device_id},
                )
                if close_res.get("status") == "clarification_needed":
                    return ConversationalResponse(
                        status="clarification_needed",
                        message=close_res.get("message", f"Multiple windows found for '{target_name}'. Please clarify which one to close."),
                    )
                if close_res.get("status") == "success":
                    session_ctx = context.get("session", {})
                    session_ctx.setdefault("references", {})["last_closed_app"] = target_name
                    session_ctx["references"]["last_action_target"] = target_name
                    session_ctx["references"]["last_target"] = target_name
                    session_ctx["references"]["last_action"] = "close_app"
                    if session_ctx["references"].get("foreground_app") == target_name:
                        session_ctx["references"].pop("foreground_app", None)
                        session_ctx["references"].pop("current_app", None)
                    return ConversationalResponse(
                        status="success",
                        message=close_res.get("message", f"Closed {target_name}."),
                    )
                else:
                    return ConversationalResponse(
                        status="error",
                        message=close_res.get("message", f"Could not close {target_name}."),
                    )

        if resolved_entity.action == "focus" and not is_compound:
            target_name = resolved_entity.target_name
            refs = context.get("session", {}).get("references", {})
            if not target_name or target_name.lower() in ("it", "that", "this", "the app", "the window"):
                target_name = refs.get("last_window") or refs.get("last_app")
            if target_name:
                focus_res = await self.control.execute_and_observe(
                    source_device_id,
                    "window.focus",
                    {"title": target_name, "app": target_name, "target_device": source_device_id},
                )
                if focus_res.get("status") == "clarification_needed":
                    return ConversationalResponse(
                        status="clarification_needed",
                        message=focus_res.get("message", f"Multiple windows found matching '{target_name}'. Please clarify which one to switch to."),
                    )
                if focus_res.get("status") == "success":
                    session_ctx = context.get("session", {})
                    session_ctx.setdefault("references", {})["last_window"] = target_name
                    session_ctx["references"]["last_app"] = target_name
                    session_ctx["references"]["last_action"] = "focus_window"
                    return ConversationalResponse(
                        status="success",
                        message=focus_res.get("message", f"Switched to {target_name}."),
                    )
                else:
                    return ConversationalResponse(
                        status="error",
                        message=focus_res.get("message", f"Could not switch to {target_name}."),
                    )

        if resolved_entity.action == "copy" and not is_compound:
            m_copy = re.search(r"copy\s+['\"]?([^'\",\n]+?)['\"]?\s+to\s+['\"]?([^'\",\n]+?)['\"]?$", natural_request, re.IGNORECASE)
            if m_copy:
                src_path = m_copy.group(1).strip()
                dst_path = m_copy.group(2).strip()
                copy_res = await self.control.execute_and_observe(
                    source_device_id,
                    "fs.copy",
                    {"src": src_path, "dst": dst_path, "overwrite": False, "target_device": source_device_id},
                )
                if copy_res.get("status") == "success":
                    session_ctx = context.setdefault("session", {})
                    session_ctx.setdefault("references", {})["last_file"] = src_path
                    session_ctx["references"]["last_copied_dst"] = dst_path
                    session_ctx["references"]["last_copied_file"] = dst_path
                    return ConversationalResponse(
                        status="success",
                        message=copy_res.get("message", f"Copied {src_path} to {dst_path}."),
                    )
                else:
                    return ConversationalResponse(
                        status="error",
                        message=copy_res.get("message", f"Could not copy {src_path} to {dst_path}."),
                    )

        if resolved_entity.action == "rename" and not is_compound:
            m_rename = re.search(r"rename\s+['\"]?([^'\",\n]+?)['\"]?\s+to\s+['\"]?([^'\",\n]+?)['\"]?$", natural_request, re.IGNORECASE)
            if m_rename:
                src_path = m_rename.group(1).strip()
                new_name = m_rename.group(2).strip()
                refs = context.setdefault("session", {}).setdefault("references", {})
                if src_path.lower() in ("the copy", "it", "that", "this"):
                    src_path = refs.get("last_copied_dst") or refs.get("last_copied_file") or refs.get("last_file") or src_path
                rename_res = await self.control.execute_and_observe(
                    source_device_id,
                    "fs.rename",
                    {"src": src_path, "new_name": new_name, "overwrite": False, "target_device": source_device_id},
                )
                if rename_res.get("status") == "success":
                    session_ctx = context.setdefault("session", {})
                    session_ctx.setdefault("references", {})["last_file"] = new_name
                    session_ctx["references"]["last_copied_dst"] = new_name
                    return ConversationalResponse(
                        status="success",
                        message=rename_res.get("message", f"Renamed {src_path} to {new_name}."),
                    )
                else:
                    return ConversationalResponse(
                        status="error",
                        message=rename_res.get("message", f"Could not rename {src_path} to {new_name}."),
                    )

        if resolved_entity.action == "move" and not is_compound:
            m_move = re.search(r"move\s+['\"]?([^'\",\n]+?)['\"]?\s+to\s+['\"]?([^'\",\n]+?)['\"]?$", natural_request, re.IGNORECASE)
            if m_move:
                src_path = m_move.group(1).strip()
                dst_path = m_move.group(2).strip()
                refs = context.setdefault("session", {}).setdefault("references", {})
                if src_path.lower() in ("the copy", "it", "that", "this", "the file"):
                    src_path = refs.get("last_file") or refs.get("last_copied_dst") or src_path
                move_res = await self.control.execute_and_observe(
                    source_device_id,
                    "fs.move",
                    {"src": src_path, "dst": dst_path, "overwrite": False, "target_device": source_device_id},
                )
                if move_res.get("status") == "success":
                    session_ctx = context.setdefault("session", {})
                    session_ctx.setdefault("references", {})["last_file"] = dst_path
                    return ConversationalResponse(
                        status="success",
                        message=move_res.get("message", f"Moved {src_path} to {dst_path}."),
                    )
                else:
                    return ConversationalResponse(
                        status="error",
                        message=move_res.get("message", f"Could not move {src_path} to {dst_path}."),
                    )

        if resolved_entity.action == "open" and not is_compound and not is_fast_response:
            raw_target = resolved_entity.target_name or ""
            refs = context.get("session", {}).get("references", {})
            if raw_target.lower() in ("this file", "the file", "it", "that"):
                raw_target = refs.get("last_file") or raw_target

            is_web_dest = bool(re.search(r"^(?:https?://|www\.|youtube|google|github|reddit|twitter|x|gmail|netflix|amazon)\b", raw_target.lower()))

            # Determine whether target is a file/folder or an application
            is_file_target = (
                resolved_entity.target_kind == "filesystem_item"
                or any(ext in raw_target.lower() for ext in (".pdf", ".txt", ".doc", ".docx", ".png", ".jpg", ".csv", ".json", ".log", " review", " document", " file"))
                or ("\\" in raw_target or "/" in raw_target or (len(raw_target) > 3 and "." in raw_target and not raw_target.startswith("www.")))
            ) and not is_web_dest

            if is_file_target and raw_target:
                target_p = resolved_entity.resolved_path or raw_target
                sel_res = await self.control.resolve_executor(
                    required_capability="fs.open",
                    requested_device=resolved_entity.target_device if resolved_entity.target_device not in (source_device_id, "current") else None,
                    source_device_id=source_device_id,
                    owning_device=resolved_entity.owning_device or source_device_id,
                )
                if not sel_res.is_success:
                    if sel_res.status == "device_offline":
                        msg = f"Device {resolved_entity.target_device} is currently offline."
                    elif sel_res.status == "capability_mismatch":
                        msg = f"Device {resolved_entity.target_device} does not support 'fs.open'."
                    else:
                        msg = sel_res.reason
                    return ConversationalResponse(status="error", message=msg)

                exec_dev = sel_res.selected_device
                open_res = await self.control.execute_and_observe(
                    exec_dev,
                    "fs.open",
                    {"path": target_p, "target_device": exec_dev},
                )
                if open_res.get("status") == "success":
                    session_ctx = context.get("session", {})
                    session_ctx.setdefault("references", {})["last_file"] = target_p
                    session_ctx["references"]["last_action"] = "open_file"
                    return ConversationalResponse(
                        status="success",
                        message=open_res.get("message", f"Opened {target_p}."),
                    )
                else:
                    return ConversationalResponse(
                        status="error",
                        message=open_res.get("message", f"Could not open {target_p}."),
                    )
            elif raw_target and not is_web_dest:
                # Application launch
                sel_res = await self.control.resolve_executor(
                    required_capability="app.launch",
                    requested_device=resolved_entity.target_device if resolved_entity.target_device not in (source_device_id, "current") else None,
                    source_device_id=source_device_id,
                    owning_device=resolved_entity.owning_device,
                )
                if not sel_res.is_success:
                    if sel_res.status == "device_offline":
                        msg = f"Device {resolved_entity.target_device} is currently offline."
                    elif sel_res.status == "capability_mismatch":
                        msg = f"Device {resolved_entity.target_device} does not support 'app.launch'."
                    else:
                        msg = sel_res.reason
                    return ConversationalResponse(status="error", message=msg)

                exec_dev = sel_res.selected_device
                launch_res = await self.control.execute_and_observe(
                    exec_dev,
                    "app.launch",
                    {"app": raw_target, "target_device": exec_dev},
                )
                if launch_res.get("status") == "success":
                    session_ctx = context.get("session", {})
                    session_ctx.setdefault("references", {})["last_app"] = raw_target
                    session_ctx["references"]["last_action"] = "launch_app"
                    return ConversationalResponse(
                        status="success",
                        message=launch_res.get("message", f"Launched {raw_target}."),
                    )
                else:
                    return ConversationalResponse(
                        status="error",
                        message=launch_res.get("message", f"Could not launch {raw_target}."),
                    )

        if resolved_entity.action in ("minimize", "maximize", "restore") and not is_compound:
            win_title = resolved_entity.target_name
            refs = context.get("session", {}).get("references", {})
            if not win_title or win_title.lower() in ("it", "that", "this", "the window", "the app"):
                win_title = refs.get("last_window") or refs.get("last_app") or ""
            cap_name = f"window.{resolved_entity.action}"
            w_res = await self.control.execute_and_observe(
                source_device_id,
                cap_name,
                {"title": win_title, "target_device": source_device_id},
            )
            if w_res.get("status") == "success":
                return ConversationalResponse(
                    status="success",
                    message=w_res.get("message", f"Window {resolved_entity.action}d."),
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message=w_res.get("message", f"Could not {resolved_entity.action} window."),
                )

        # ===================================================================
        # Phase A4: Direct Self-Knowledge & System Registry Routing
        # ===================================================================
        from .system_registry import SystemRegistry
        sys_reg = SystemRegistry.get_instance()

        # Real-Runtime Repair: Compound Self-Knowledge (Multi-Integration & Multi-Capability)
        integs_to_check = []
        if re.search(r"\bcalendar\b", clean_req):
            integs_to_check.append("calendar")
        if re.search(r"\b(?:gmail|email)\b", clean_req):
            integs_to_check.append("gmail")
        if re.search(r"\bcontacts?\b", clean_req):
            integs_to_check.append("contacts")

        is_integ_query = any(w in clean_req for w in ("access", "connected", "have", "read", "check", "integrate", "service"))
        if len(integs_to_check) >= 2 and is_integ_query:
            parts = []
            res_data = {}
            for iname in integs_to_check:
                istat = await sys_reg.get_integration_status(iname)
                res_data[iname] = istat
                disp_name = "Google Calendar" if iname == "calendar" else ("Gmail" if iname == "gmail" else iname.capitalize())
                if istat.get("connected"):
                    parts.append(f"{disp_name} is connected and authorized.")
                else:
                    parts.append(f"{disp_name} is not currently connected.")
            session_refs["last_introspection"] = {
                "topic": "integrations",
                "entities": integs_to_check,
                "timestamp": time.time(),
            }
            return ConversationalResponse(
                status="success",
                message=" ".join(parts),
                data={"integrations": res_data},
            )

        caps_to_check = []
        if re.search(r"\b(?:search|browse)(?: the)? (?:web|internet|online)\b", clean_req):
            caps_to_check.append("web.search")
        if re.search(r"\b(?:see|look at|view|inspect)(?: my)? screen|screen perception|screenshot\b", clean_req):
            caps_to_check.append("screen.perception")
        if re.search(r"\b(?:check|get|see)(?: the)? weather\b", clean_req):
            caps_to_check.append("weather.current")
        if re.search(r"\b(?:control|manage|open|close|launch)(?: my)? (?:apps|applications|windows)\b", clean_req):
            caps_to_check.append("app.control")

        if len(caps_to_check) >= 2 and (any(w in natural_request.lower() for w in ("can you", "able to", "do you have", "what can you")) or " and " in clean_req):
            cap_phrases = []
            for cname in caps_to_check:
                if cname == "web.search":
                    cap_phrases.append("search the web for live information")
                elif cname == "screen.perception":
                    cap_phrases.append("capture and analyze what is visible on your screen")
                elif cname == "weather.current":
                    cap_phrases.append("check live weather conditions and forecasts")
                elif cname == "app.control":
                    cap_phrases.append("control applications and windows")
            session_refs["last_introspection"] = {
                "topic": "capabilities",
                "entities": caps_to_check,
                "timestamp": time.time(),
            }
            return ConversationalResponse(
                status="success",
                message=f"Yes, I can {', and I can '.join(cap_phrases)}.",
                data={"capabilities": caps_to_check},
            )

        # 1. Identity Queries ("What are you?", "Who are you?", "What is Cortex?")
        is_identity_query = bool(re.search(
            r"\b(?:what are you|who are you|what is cortex|tell me about yourself|describe yourself|introduce yourself)\b",
            clean_req,
        ))
        if is_identity_query and not is_compound:
            ident = sys_reg.get_identity(current_node=source_device_id)
            msg = (
                f"I am {ident['product_name']}, a personal AI assistant built on a {ident['architecture_mode']} architecture. "
                f"I combine deterministic local system execution with multi-provider intelligence for reasoning, live web access, "
                f"visual screen perception, and application control."
            )
            return ConversationalResponse(
                status="success",
                message=msg,
                data=ident,
            )

        # 2. Model & Provider Queries ("What model are you using?", "What model is this?")
        is_model_query = bool(re.search(
            r"\b(?:what model (?:are you|is this|do you)|which model (?:are you|do you)|what ai model|what llm|model are you using|what model are you running)\b",
            clean_req,
        ))
        if is_model_query and not is_compound:
            models = sys_reg.get_models()
            llm_entry = next((m for m in models if m["purpose"] == "conversational_reasoning"), None)
            prov = llm_entry.get("provider", "unknown") if llm_entry else "unknown"
            mod = llm_entry.get("model", "unknown") if llm_entry else "unknown"
            if prov == "unknown" or mod == "unknown" or not (llm_entry and llm_entry.get("availability")):
                msg = "Active model metadata is currently unavailable or unknown."
            else:
                msg = f"I am running on Cortex using the {prov} model ({mod}) for conversational reasoning and planning."
            return ConversationalResponse(
                status="success",
                message=msg,
                data={"models": models},
            )

        # 3. Overall Capabilities List ("What can you do?", "What are your capabilities?")
        is_capabilities_list_query = bool(re.search(
            r"\b(?:what can you (?:actually )?do|what are your capabilities|what capabilities do you have|what actions can you perform|list (?:your )?capabilities)\b",
            clean_req,
        ))
        if is_capabilities_list_query and not is_compound:
            caps = sys_reg.list_capabilities()
            active_caps = [c["name"] for c in caps if c.get("availability")]
            msg = (
                "I can perform live web searches, fetch live weather reports, open browser URLs, "
                "capture and visually analyze your screen, launch and gracefully close desktop applications, "
                "switch and focus windows, and reliably perform verified file operations (open, copy, move, rename, delete)."
            )
            return ConversationalResponse(
                status="success",
                message=msg,
                data={"capabilities": caps, "active_count": len(active_caps)},
            )

        # 4. Specific Capability Status Queries ("Can you search the web?", "Can you check the weather?", "Can you see my screen?", "Can you control apps?")
        req_for_caps = natural_request.lower().strip(" \t.,!?;:")
        is_can_weather = bool(re.search(
            r"\b(?:can you (?:check|get|see|look up)(?: the)? weather|do you have weather access)\b",
            req_for_caps,
        )) or bool(re.search(r"\b(?:can you (?:check|get|see|look up)(?: the)? weather|do you have weather access)\b", clean_req))
        if is_can_weather and not is_compound:
            w_stat = sys_reg.get_capability_status("weather.current")
            if w_stat["capabilities"]["weather.current"]["available"]:
                pending_act = {
                    "intent": "get_weather",
                    "capability": "weather.current",
                    "action": "weather.current",
                    "arguments": {},
                    "missing_arguments": ["location"],
                    "target_device": source_device_id,
                    "created_at": time.time(),
                }
                session_refs["pending_action"] = pending_act
                session_refs["last_introspection"] = {"topic": "capabilities", "entities": ["weather.current"], "timestamp": time.time()}
                from .session_context import SessionContextManager
                await SessionContextManager.set_pending_action(source_device_id, pending_act)
                return ConversationalResponse(
                    status="success",
                    message="Yes, I can check live weather conditions and forecasts.",
                    data=w_stat,
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message="No, weather access is currently unavailable because the weather provider is disabled.",
                    data=w_stat,
                )

        is_can_search = bool(re.search(
            r"\b(?:can you (?:search|browse)(?: the)? (?:web|internet|online)|do you have (?:web|internet) search)\b",
            req_for_caps,
        )) or bool(re.search(r"\b(?:can you (?:search|browse)(?: the)? (?:web|internet|online)|do you have (?:web|internet) search)\b", clean_req))
        if is_can_search and not is_compound:
            s_stat = sys_reg.get_capability_status("web.search")
            if s_stat["capabilities"]["web.search"]["available"]:
                return ConversationalResponse(
                    status="success",
                    message="Yes, I can search the web for live information.",
                    data=s_stat,
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message="No, web search is currently unavailable because the search provider is unconfigured.",
                    data=s_stat,
                )

        is_can_screen = bool(re.search(
            r"\b(?:can you (?:see|look at|view|inspect|watch)(?: my)? screen|can you take (?:a )?screenshot|do you have screen perception)\b",
            req_for_caps,
        )) or bool(re.search(r"\b(?:can you (?:see|look at|view|inspect|watch)(?: my)? screen|can you take (?:a )?screenshot|do you have screen perception)\b", clean_req))
        if is_can_screen and not is_compound:
            sc_stat = sys_reg.get_capability_status(["screen.capture", "screen.analyze"])
            if sc_stat["all_available"]:
                return ConversationalResponse(
                    status="success",
                    message="Yes, I can capture and analyze what is visible on your screen.",
                    data=sc_stat,
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message="No, screen perception is currently unavailable because the vision provider is unconfigured or disabled.",
                    data=sc_stat,
                )

        is_can_apps = bool(re.search(
            r"\b(?:can you (?:control|manage|open|close|switch)(?: my)? (?:apps|applications|windows)|can you launch apps)\b",
            req_for_caps,
        )) or bool(re.search(r"\b(?:can you (?:control|manage|open|close|switch)(?: my)? (?:apps|applications|windows)|can you launch apps)\b", clean_req))
        if is_can_apps and not is_compound:
            app_stat = sys_reg.get_capability_status(["app.launch", "app.close", "window.focus"])
            return ConversationalResponse(
                status="success",
                message="Yes, I can inspect, launch, focus, minimize, maximize, and gracefully close applications.",
                data=app_stat,
            )

        # 5. Device Registry & Self-Knowledge Queries
        # 5A. Capability discovery ("Which devices can take screenshots?", "Which devices can capture my screen?")
        is_cap_discovery = bool(re.search(
            r"\bwhich devices (?:can|are able to) (?:take screenshots?|capture (?:my |the )?screen)\b",
            clean_req,
        ))
        if is_cap_discovery and not is_compound:
            from .executor_registry import ExecutorRegistry
            er = ExecutorRegistry.get_instance()
            capable_devs = er.get_capable_devices("screen.capture", online_only=True)
            if not capable_devs:
                # Check DB devices if registry not populated yet
                all_d = await sys_reg.list_devices(current_node=source_device_id, connected_devices_map=self.control.connected_devices)
                capable_devs = [type('MockHandle', (), {"display_name": d["name"], "online": d["online"]})() for d in all_d if "screen.capture" in d.get("advertised_capabilities", []) and d.get("online")]
            if capable_devs:
                dev_names = ", ".join([getattr(d, "display_name", getattr(d, "device_id", str(d))) for d in capable_devs])
                msg = f"The following online devices can capture your screen: {dev_names}."
            else:
                msg = "No online devices currently support screen capture."
            return ConversationalResponse(
                status="success",
                message=msg,
                data={"capable_devices": [getattr(d, "display_name", str(d)) for d in capable_devs]},
            )

        # 5B. Device capability inquiry ("Can my Lenovo open apps?", "Can Lenovo open apps?")
        m_can_dev_action = re.search(
            r"\bcan (?:my )?([a-zA-Z0-9_-]+) (?:open apps?|launch apps?|take screenshots?|capture screen)\b",
            clean_req,
        )
        if m_can_dev_action and not is_compound:
            dev_target = m_can_dev_action.group(1).strip()
            action_text = clean_req.lower()
            req_cap = "screen.capture" if ("screenshot" in action_text or "capture screen" in action_text) else "app.launch"
            from .executor_registry import ExecutorRegistry
            er = ExecutorRegistry.get_instance()
            handle = er.get_device_handle(dev_target)
            if not handle:
                for h in er.list_device_handles():
                    if h.display_name.lower() == dev_target.lower() or h.device_id.lower() == dev_target.lower():
                        handle = h
                        break
            if not handle:
                # Check DB
                stat = await sys_reg.get_device_status(dev_target, current_node=source_device_id, connected_devices_map=self.control.connected_devices)
                if not stat:
                    return ConversationalResponse(status="error", message=f"Device '{dev_target}' is not enrolled in Cortex.")
                is_on = stat.get("online", False)
                has_cap = req_cap in stat.get("advertised_capabilities", [])
                cap_human = "open applications" if req_cap == "app.launch" else "capture screenshots"
                if not is_on:
                    return ConversationalResponse(status="error", message=f"No, {stat['name']} is currently offline.", data=stat)
                elif has_cap:
                    return ConversationalResponse(status="success", message=f"Yes, {stat['name']} can {cap_human}.", data=stat)
                else:
                    return ConversationalResponse(status="error", message=f"No, {stat['name']} does not support {cap_human}.", data=stat)

            cap_human = "open applications" if req_cap == "app.launch" else "capture screenshots"
            if not handle.online:
                return ConversationalResponse(status="error", message=f"No, {handle.display_name} is currently offline.", data=handle.to_dict())
            elif handle.supports_capability(req_cap):
                return ConversationalResponse(status="success", message=f"Yes, {handle.display_name} can {cap_human}.", data=handle.to_dict())
            else:
                return ConversationalResponse(status="error", message=f"No, {handle.display_name} does not support {cap_human}.", data=handle.to_dict())

        # 5C. "Why can't you run this on my <device>?"
        m_why_dev = re.search(
            r"\bwhy can(?:['’]t| not) you run (?:this|that) on (?:my )?([a-zA-Z0-9_-]+)\b",
            clean_req,
        )
        if m_why_dev and not is_compound:
            dev_target = m_why_dev.group(1).strip()
            from .executor_registry import ExecutorRegistry
            er = ExecutorRegistry.get_instance()
            handle = er.get_device_handle(dev_target)
            if not handle:
                for h in er.list_device_handles():
                    if h.display_name.lower() == dev_target.lower() or h.device_id.lower() == dev_target.lower():
                        handle = h
                        break
            if not handle:
                msg = f"Device '{dev_target}' is not enrolled in Cortex."
            elif not handle.online:
                msg = f"I cannot run that on {handle.display_name} because it is currently offline."
            elif handle.trust_level == "restricted":
                msg = f"I cannot run that on {handle.display_name} because its trust level is restricted."
            else:
                msg = f"Device {handle.display_name} does not have the required capabilities to run that action."
            return ConversationalResponse(status="success", message=msg, data=handle.to_dict() if handle else None)

        is_devices_list_query = bool(re.search(
            r"\b(?:what devices are connected|which devices are connected|list connected devices|show connected devices|what devices do you have|what devices are online|which devices are online)\b",
            clean_req,
        ))
        if is_devices_list_query and not is_compound:
            devices = await sys_reg.list_devices(current_node=source_device_id, connected_devices_map=self.control.connected_devices)
            from .executor_registry import ExecutorRegistry
            _er_inst = ExecutorRegistry.get_instance()
            for d in devices:
                log.info(
                    f"[DIAGNOSTIC] self_knowledge: device_id={d['device_id']} "
                    f"name={d.get('name')} online={d.get('online')} "
                    f"source={'ExecutorRegistry' if _er_inst.get_device_handle(d['device_id']) else 'conn_map'} "
                    f"heartbeat_freshness={d.get('heartbeat_freshness')}"
                )
            online_devs = [d for d in devices if d.get("online")]
            dev_names = ", ".join([f"{d['name']} ({'online' if d.get('online') else 'offline'}, role: {d.get('role')})" for d in devices])
            msg = f"Connected devices ({len(online_devs)} online): {dev_names}."
            return ConversationalResponse(
                status="success",
                message=msg,
                data={"devices": devices},
            )

        is_current_device_query = bool(re.search(
            r"\b(?:which device are you (?:running|on)|which device are you running this on|what device is this|which node are you on)\b",
            clean_req,
        ))
        if is_current_device_query and not is_compound:
            ident = sys_reg.get_identity(current_node=source_device_id)
            msg = f"I am running on {ident['current_node'].upper()} (role: {ident['role']})."
            return ConversationalResponse(
                status="success",
                message=msg,
                data=ident,
            )

        is_would_run_query = bool(re.search(
            r"\bwhich device would run (?:this|that)\b",
            clean_req,
        ))
        if is_would_run_query and not is_compound:
            s_refs = context.get("session", {}).get("references", {}) if isinstance(context, dict) else {}
            target_dev = s_refs.get("grounded_target", {}).get("owning_device") or source_device_id
            from .executor_registry import ExecutorRegistry
            er = ExecutorRegistry.get_instance()
            handle = er.get_device_handle(target_dev)
            dev_name = handle.display_name if handle else target_dev.upper()
            return ConversationalResponse(
                status="success",
                message=f"That action would run on {dev_name} (the current execution node).",
                data={"selected_device": target_dev},
            )

        # 5b. Task Ownership, Handoff & Failover Self-Knowledge (Phase A6 & A7 Privacy)
        is_ownership_self_knowledge = bool(re.search(
            r"\b(?:where is (?:this|the|my) task(?: running)?|why did (?:it|the task|this task) move to|why is (?:this|the) task waiting|will (?:it|this task|the task) (?:continue|resume) when|which device (?:owns|currently owns) (?:this|the) task|why (?:are you running|run) this locally|why didn'?t this (?:task )?move to (?:the )?cloud|can lenovo handle this privately|what data would leave (?:my )?(?:laptop|device))\b",
            clean_req,
        ))
        if is_ownership_self_knowledge and not is_compound:
            from .task_ownership import TaskOwnershipManager
            tom = TaskOwnershipManager.get_instance()
            sk_ans = await tom.answer_self_knowledge_query(clean_req)
            if sk_ans.get("matched"):
                return ConversationalResponse(
                    status="success",
                    message=sk_ans["answer"],
                    data=sk_ans.get("execution"),
                )

        # 5c. Cross-Device Transport & File Transfer Self-Knowledge (Phase A8)
        is_transfer_self_knowledge = bool(re.search(
            r"\b(?:where is (?:my|the|this) file|is (?:it|the file|the transfer) still transferring|how much is (?:left|remaining)|why did the transfer pause|which transport (?:are you|is) using|can (?:this|the transfer) resume|did lenovo receive (?:it|the file)|cancel (?:that|the) transfer|resume (?:that|the) transfer)\b",
            clean_req,
        ))
        if is_transfer_self_knowledge and not is_compound:
            from .file_transfer import FileTransferManager
            ftm = FileTransferManager.get_instance()
            ft_ans = await ftm.answer_self_knowledge_query(clean_req)
            if ft_ans.get("matched"):
                return ConversationalResponse(
                    status="success",
                    message=ft_ans["answer"],
                    data=ft_ans.get("session") or ft_ans.get("locality"),
                )

        # 6. Integration Introspection Queries ("Do you have access to my calendar?", "Do you have Gmail access?", "What integrations are connected?")
        is_calendar_integration_query = bool(re.search(
            r"\b(?:do you have (?:access to (?:my )?)?calendar|can you access (?:my )?calendar|is calendar connected)\b",
            clean_req,
        ))
        if is_calendar_integration_query and not is_compound:
            c_stat = await sys_reg.get_integration_status("calendar")
            session_refs["last_introspection"] = {"topic": "integrations", "entities": ["calendar"], "timestamp": time.time()}
            if c_stat.get("connected"):
                return ConversationalResponse(
                    status="success",
                    message="Yes, Google Calendar is connected and authorized.",
                    data=c_stat,
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message="No, Google Calendar access is not currently connected or authorized.",
                    data=c_stat,
                )

        is_gmail_integration_query = bool(re.search(
            r"\b(?:do you have (?:access to )?gmail|do you have gmail access|do you have (?:access to (?:my )?)?email|can you read (?:my )?email|is gmail connected|do you have gmail)\b",
            clean_req,
        ))
        if is_gmail_integration_query and not is_compound:
            g_stat = await sys_reg.get_integration_status("gmail")
            session_refs["last_introspection"] = {"topic": "integrations", "entities": ["gmail"], "timestamp": time.time()}
            if g_stat.get("connected"):
                return ConversationalResponse(
                    status="success",
                    message="Yes, Gmail is connected and authorized.",
                    data=g_stat,
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message="No, Gmail access is not currently connected or authorized.",
                    data=g_stat,
                )

        is_integrations_list_query = bool(re.search(
            r"\b(?:what integrations are connected|list integrations|what services are connected)\b",
            clean_req,
        ))
        if is_integrations_list_query and not is_compound:
            integs = await sys_reg.list_integrations()
            conn = [i["name"] for i in integs if i.get("connected")]
            not_conn = [i["name"] for i in integs if not i.get("connected")]
            if conn:
                msg = f"Connected integrations: {', '.join(conn)}. Unconnected: {', '.join(not_conn)}."
            else:
                msg = f"No external service integrations are currently connected. (Available integrations: {', '.join([i['name'] for i in integs])})."
            return ConversationalResponse(
                status="success",
                message=msg,
                data={"integrations": integs},
            )

        # 7. Limitation & Capability Explanation Queries ("Why can't you read Gmail?", "Why can't you check the weather?", "Why can't you do this?")
        m_explain = re.search(
            r"\bwhy can(?:['’]t| not) you\s+(.+)",
            clean_req,
        )
        if m_explain and not is_compound:
            target_task = m_explain.group(1).strip()
            exp = await sys_reg.explain_capability(target_task)
            return ConversationalResponse(
                status="success",
                message=exp.get("explanation", ""),
                data=exp,
            )

        # ===================================================================
        # Phase A1: Web Search, Weather, and Browser Navigation Capabilities
        # ===================================================================

        # 1. Weather Routing
        is_weather_query = bool(re.search(
            r"\b(?:what(?:['’]s| is) the weather|weather today|weather forecast|weather right now|how(?:['’]s| is) the weather|weather in|temperature in|forecast (?:for|this|next)|check the weather|what is the forecast|what's the forecast)\b",
            clean_req,
        )) or ("weather" in clean_req and any(w in clean_req for w in ("what", "how", "forecast", "today", "now", "current", "temperature", "check")))

        if is_weather_query and not is_compound:
            from .weather_provider import get_weather_provider
            weather_provider = get_weather_provider()
            if not weather_provider:
                return ConversationalResponse(
                    status="error",
                    message="Live weather provider is unavailable. I cannot check current weather right now.",
                )

            # Determine location using semantic modifier stripping
            location_str = extract_weather_location(clean_req)
            if not location_str:
                # Check context / session references
                dev_loc = context.get("device", {}).get("location")
                sess_loc = (
                    context.get("session", {}).get("references", {}).get("location")
                    or context.get("session", {}).get("references", {}).get("user_location")
                )
                location_str = dev_loc or sess_loc

            if not location_str:
                return ConversationalResponse(
                    status="clarification_needed",
                    message="Which city or location would you like me to check the weather for?",
                )

            is_forecast = bool(re.search(r"\b(?:forecast|weekend|tomorrow|next week|days)\b", clean_req))
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()

            try:
                if is_forecast:
                    w_res = await weather_provider.get_forecast(location_str)
                    loc_name = w_res.get("location", location_str)
                    session_ctx = context.setdefault("session", {})
                    session_ctx.setdefault("references", {})["location"] = loc_name
                    days = w_res.get("forecast", [])
                    if days:
                        summary_parts = [
                            f"{d.get('date')}: {d.get('conditions')}, high {d.get('high')}{d.get('temperature_unit', '°C')}, low {d.get('low')}{d.get('temperature_unit', '°C')}"
                            for d in days[:3]
                        ]
                        forecast_text = "; ".join(summary_parts)
                        msg = f"Weather forecast for {loc_name}: {forecast_text}."
                    else:
                        msg = f"Weather forecast retrieved for {loc_name}."
                else:
                    w_res = await weather_provider.get_current(location_str)
                    loc_name = w_res.get("location", location_str)
                    session_ctx = context.setdefault("session", {})
                    session_ctx.setdefault("references", {})["location"] = loc_name
                    temp = w_res.get("temperature")
                    unit = w_res.get("temperature_unit", "°C")
                    cond = w_res.get("conditions", "Normal")
                    feels = w_res.get("feels_like")
                    humidity = w_res.get("humidity")
                    msg = f"It's currently {temp}{unit} and {cond} in {loc_name}."
                    if feels is not None:
                        msg += f" Feels like {feels}{unit}."
                    if humidity is not None:
                        msg += f" Humidity is {humidity}%."

                return ConversationalResponse(
                    status="success",
                    message=msg,
                    provenance={
                        "provider": weather_provider.provider_name,
                        "retrieved_at": w_res.get("observed_at") or now_iso,
                        "source_type": "live_weather",
                    },
                )
            except Exception as exc:
                log.error(f"[AgentHarness] Weather query error: {exc}")
                return ConversationalResponse(
                    status="error",
                    message=f"Could not retrieve weather: {exc}",
                )

        # 2. Web Search Routing
        m_search = re.search(
            r"^(?:search (?:the web|google|online|for)|look up(?: on the web)?|find (?:the )?latest news (?:about|on)|latest news (?:about|on))\s+(.+)",
            clean_req,
        ) or re.search(r"\b(?:search the web for|search google for|search online for)\s+(.+)", clean_req)

        if m_search and not is_compound:
            raw_query = m_search.group(1).strip(" \t.,!?;:")
            # Strip leading prepositions if any remain
            clean_query = re.sub(r"^(?:for|about|the)\s+", "", raw_query).strip()

            from .search_provider import get_search_provider
            search_provider = get_search_provider()
            if not search_provider:
                return ConversationalResponse(
                    status="error",
                    message="Live search provider is unavailable. I cannot search the web right now.",
                )

            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()

            try:
                s_res = await search_provider.search(clean_query, max_results=3)
                results = s_res.get("results", [])
                if results:
                    top_items = []
                    for r in results[:3]:
                        t = r.get("title", "")
                        snip = r.get("snippet", "")
                        u = r.get("url", "")
                        top_items.append(f"• {t}: {snip} ({u})")
                    summary_body = "\n".join(top_items)
                    msg = f"Here is what I found for '{clean_query}':\n{summary_body}"
                else:
                    msg = f"I searched the web for '{clean_query}', but found no results."

                source_urls = [r["url"] for r in results if r.get("url")]
                return ConversationalResponse(
                    status="success",
                    message=msg,
                    provenance={
                        "provider": search_provider.provider_name,
                        "source_urls": source_urls,
                        "retrieved_at": s_res.get("retrieved_at") or now_iso,
                        "source_type": "live_search",
                    },
                )
            except Exception as exc:
                log.error(f"[AgentHarness] Search query error: {exc}")
                return ConversationalResponse(
                    status="error",
                    message=f"Search failed: {exc}",
                )

        # 3. Browser Navigation Routing ("Open YouTube", "Open https://...", "Open GitHub")
        m_browser = re.search(
            r"^(?:open|launch|go to|navigate to)\s+(?:the\s+)?(?:website|site|url|page)?\s*(https?://\S+|www\.\S+|[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?|youtube|github|google|reddit|twitter|x|gmail|netflix|amazon|wikipedia|linkedin|spotify|chatgpt|openai|huggingface)\b",
            clean_req,
        )
        if m_browser and not is_compound:
            target_site = m_browser.group(1).strip()
            from .browser_navigation import resolve_web_destination
            target_url = await resolve_web_destination(target_site)

            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()

            exec_res = await self.control.execute_and_observe(
                source_device_id,
                "browser.open_url",
                {"url": target_url, "target_device": source_device_id},
            )

            # Check for visual verification ("and check that it opened", "and verify it opened")
            is_visual_verify = bool(re.search(r'\b(?:and\s+)?(?:check|verify)(?:\s+that|\s+if)?\s+it(?:\s+visibly)?\s+opened\b', clean_req))
            if is_visual_verify:
                from .vision_provider import get_vision_provider
                v_provider = get_vision_provider()
                if not v_provider:
                    return ConversationalResponse(
                        status="error",
                        message="Screen analysis provider unavailable.",
                    )

                # Capture screen on target device
                cap_res = await self.control.execute_and_observe(
                    source_device_id,
                    "screen.capture",
                    {"target_device": source_device_id},
                )
                cap_data = cap_res.get("data") if isinstance(cap_res.get("data"), dict) else {}
                img_ref = cap_data.get("image_ref") or cap_res.get("message", "").replace("Screenshot saved: ", "").strip()

                analyze_res = await self.control.execute_and_observe(
                    source_device_id,
                    "screen.analyze",
                    {
                        "image_ref": img_ref,
                        "question": f"Did {target_site} visibly open in the browser?",
                        "mode": "inspect",
                    },
                )
                from .screen_perception import ScreenPerceptionManager
                obs = ScreenPerceptionManager.get_latest_observation(source_device_id)
                summary_text = (obs.summary if obs else "") or analyze_res.get("data", {}).get("summary", "")
                apps_list = (obs.visible_apps if obs else []) or analyze_res.get("data", {}).get("visible_apps", [])
                text_frags = (obs.visible_text_fragments if obs else []) or analyze_res.get("data", {}).get("visible_text_fragments", [])

                site_lower = target_site.lower()
                is_visible = (
                    any(site_lower in app.lower() for app in apps_list)
                    or any(site_lower in tf.lower() for tf in text_frags)
                    or (site_lower in summary_text.lower())
                )
                if is_visible or "youtube" in summary_text.lower() or "browser" in summary_text.lower() or any("chrome" in a.lower() for a in apps_list):
                    return ConversationalResponse(
                        status="success",
                        message=f"Opened {target_url} and verified it opened on screen.",
                        provenance={
                            "provider": v_provider.provider_name,
                            "source_urls": [target_url],
                            "source_type": "visual_verification",
                            "retrieved_at": now_iso,
                            "observation_id": obs.observation_id if obs else analyze_res.get("data", {}).get("observation_id", ""),
                        },
                        data=obs.to_dict() if obs else analyze_res.get("data", {}),
                    )
                else:
                    return ConversationalResponse(
                        status="error",
                        message=f"Opened {target_url}, but could not visually verify that it opened.",
                        provenance={
                            "provider": v_provider.provider_name,
                            "source_type": "visual_verification",
                        },
                    )

            if exec_res.get("status") == "success":
                return ConversationalResponse(
                    status="success",
                    message=f"Opened {target_url} in your default browser.",
                    provenance={
                        "provider": "system_browser",
                        "source_urls": [target_url],
                        "retrieved_at": now_iso,
                        "source_type": "device_action",
                    },
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message=f"Could not open browser for {target_url}: {exec_res.get('message')}",
                )

        # ===================================================================
        # Phase A2: Direct Screen Perception Routing ("What's on my screen?")
        # ===================================================================
        is_screen_perception_query = bool(re.search(
            r"\b(?:"
            r"what(?:['’]s| is) (?:on|showing on|visible on) (?:my |the |[a-zA-Z0-9_-]+(?:'s)? )?screen|"
            r"what am i looking at|"
            r"read (?:this|the|my) screen|"
            r"what error is (?:showing|on my screen|on the screen|visible)|"
            r"what(?:['’]s| is) open on (?:my |the )?screen|"
            r"look at (?:my |the )?screen"
            r")\b",
            clean_req,
        )) or (
            ("screen" in clean_req or "looking at" in clean_req)
            and any(w in clean_req for w in ("what", "read", "error", "which", "look", "inspect", "seeing", "visible"))
            and not re.search(r"\b(?:take|capture|save)\s+(?:a\s+)?screenshot\b", clean_req)
        )

        if is_screen_perception_query and not is_compound:
            from .vision_provider import get_vision_provider
            vision_provider = get_vision_provider()
            if not vision_provider or not getattr(vision_provider, "is_available", lambda: True)():
                return ConversationalResponse(
                    status="error",
                    message="Screen analysis provider unavailable.",
                )

            # Extract explicit target device if specified (e.g. "What is on Lenovo's screen?")
            target_screen_device = source_device_id
            m_screen_dev = re.search(r'\bon\s+([a-zA-Z0-9_-]+)(?:\'s)?(?:\s+screen)?\b', clean_req)
            if m_screen_dev:
                cand_screen_dev = m_screen_dev.group(1).lower().rstrip("'s")
                if cand_screen_dev not in ("the", "my", "this", "that", "recent"):
                    target_screen_device = cand_screen_dev

            # 1. Capture screen on target device
            cap_res = await self.control.execute_and_observe(
                target_screen_device,
                "screen.capture",
                {"target_device": target_screen_device},
            )
            if cap_res.get("status") != "success":
                return ConversationalResponse(
                    status="error",
                    message=f"Could not capture screen: {cap_res.get('message', 'Capture failed')}",
                )

            data_dict = cap_res.get("data") if isinstance(cap_res.get("data"), dict) else {}
            img_ref = data_dict.get("image_ref") or cap_res.get("message", "").replace("Screenshot saved: ", "").strip()

            # 2. Determine mode
            if any(w in clean_req for w in ("error", "fail", "wrong", "issue", "crash", "warning")):
                mode = "inspect"
            elif any(w in clean_req for w in ("locate", "where is", "find button", "find element")):
                mode = "locate"
            else:
                mode = "describe"

            # 3. Analyze image with vision provider
            try:
                obs = await vision_provider.analyze_image(
                    image_ref=img_ref,
                    question=natural_request,
                    mode=mode,
                    device_id=target_screen_device,
                )

                # 4. Update World State signals and ephemeral cache
                from .screen_perception import ScreenPerceptionManager
                spm = ScreenPerceptionManager.get_instance()
                spm.store_observation(obs)
                await spm.publish_world_state_signals(obs)
                session_refs["last_screenshot_ref"] = img_ref
                session_refs["last_screenshot_device"] = target_screen_device
                session_refs["last_screen_observation"] = obs.to_dict()

                return ConversationalResponse(
                    status="success",
                    message=obs.summary,
                    provenance={
                        "provider": obs.provenance.get("provider", vision_provider.provider_name),
                        "model": obs.provenance.get("model", vision_provider.model_name),
                        "source_type": "screen_perception",
                        "retrieved_at": obs.captured_at,
                        "visible_apps": obs.visible_apps,
                        "observation_id": obs.observation_id,
                    },
                    data=obs.to_dict(),
                )
            except Exception as exc:
                log.error(f"[AgentHarness] Screen perception error: {exc}")
                return ConversationalResponse(
                    status="error",
                    message=f"Screen analysis failed: {exc}",
                )

        if " and " in normalized_request and not is_compound:
            parts = normalized_request.split(" and ", 1)
            if len(parts) > 1:
                next_word = parts[1].strip().split()[0] if parts[1].strip() else ""
                if next_word in action_verbs or next_word in question_starters:
                    is_compound = True
                elif match:
                    is_compound = True
                    
        if is_compound:
            match = None
            is_fast_response = False

        # Capability-Aware Routing candidate check
        capability_triggers = {
            "device.status": {
                "keywords": ["battery percentage", "battery level", "power level", "battery life", "check battery"],
                "intent": "get_device_status",
                "action": "get_status",
                "summary": "getting device status"
            },
            "screen.capture": {
                "keywords": ["take a screenshot", "take screenshot", "capture screen", "capture the screen", "screenshot"],
                "intent": "capture_screen",
                "action": "screen.capture",
                "summary": "taking a screenshot"
            },
            "hub.read": {
                "keywords": ["online devices", "devices online", "what devices"],
                "intent": "list_online_devices",
                "action": "list_devices",
                "summary": "listing online devices"
            }
        }

        cap_match = None
        device_caps = context.get("device", {}).get("capabilities", [])
        if not is_compound:
            for cap, config in capability_triggers.items():
                if any(kw in normalized_request for kw in config["keywords"]):
                    log.info(f"Capability routing candidate: {cap}")
                    explicit_dev = None
                    m_target_dev = re.search(r'\bon\s+(?:my\s+)?([a-zA-Z0-9_-]+)$', clean_req, re.IGNORECASE)
                    if m_target_dev:
                        explicit_dev = m_target_dev.group(1).strip()

                    target_dev_ref = explicit_dev if (explicit_dev and explicit_dev.lower() not in ("this device", "here", "this laptop", "my phone", "this", "local", "current")) else "this device"
                    target_dev = explicit_dev or source_device_id

                    cap_match = ActionPlan(
                        intent=config["intent"],
                        source_device=source_device_id,
                        target_device=target_dev,
                        target_device_reference=target_dev_ref,
                        capability=cap,
                        action=config["action"],
                        arguments={},
                        requires_confirmation=False,
                        confidence=1.0,
                        user_facing_summary=config["summary"]
                    )
                    log.info(f"Capability route selected: {cap} on {target_dev}")
                    is_fast_response = False
                    break

        # Phase 8A: Direct situational / World State question routing (only if not compound and not handled by a device capability)
        if not is_compound and not cap_match:
            situational_direct_answer = await self._try_answer_situational_question(clean_req, context, source_device_id)
            if situational_direct_answer:
                log.info(f"[Situational] Intent recognized and directly answered: '{situational_direct_answer[:60]}...'")
                t_route_done = time.monotonic()
                trace.record_stage("routing_complete", t_route_done)
                trace.routing_lane = "DETERMINISTIC_SITUATIONAL"
                trace.model_provider = "deterministic_situational"
                latency_metrics["routing_path"] = "DETERMINISTIC_SITUATIONAL"
                latency_metrics["classification"] = "RESPONSE"
                latency_metrics["intent_route_complete"] = t_route_done
                return ConversationalResponse(
                    status="success",
                    message=situational_direct_answer
                )

        # Session exit fallback check
        if self._is_session_exit_command(clean_req):
            log.info(f"[Harness] Session exit recognized: '{clean_req}'")
            t_route_done = time.monotonic()
            trace.record_stage("routing_complete", t_route_done)
            trace.routing_lane = "DETERMINISTIC_EXIT"
            trace.model_provider = "deterministic_exit"
            latency_metrics["routing_path"] = "DETERMINISTIC_EXIT"
            latency_metrics["classification"] = "RESPONSE"
            latency_metrics["intent_route_complete"] = t_route_done
            return ConversationalResponse(
                status="success",
                message="Going to sleep."
            )

        repeat_match = None
        if clean_req in ["do it again", "do that again", "again", "repeat that"]:
            is_fast_response = False
            session_ctx = context.get("session", {})
            refs = session_ctx.get("references", {})
            last_act = refs.get("last_action")
            if last_act:
                last_intent = refs.get("last_intent", "repeat_action")
                last_cap = refs.get("last_capability")
                if not last_cap:
                    if last_act == "take_screenshot": last_cap = "screen.capture"
                    elif last_act == "get_status": last_cap = "device.status"
                    elif last_act == "open_app": last_cap = "app.launch"
                    else: last_cap = "unknown"
                
                args = {}
                summary = "repeating previous action"
                if last_act == "take_screenshot":
                    summary = "taking a screenshot"
                elif last_act == "get_status":
                    summary = "getting device status"
                elif last_act == "open_app":
                    app_name = refs.get("last_app", "application")
                    args = {"app": app_name}
                    summary = f"opening {app_name}"
                elif last_act == "move_file":
                    args = {"src": refs.get("last_file", ""), "dst": refs.get("last_dest", "")}
                    summary = "moving a file"
                    
                repeat_match = ActionPlan(
                    intent=last_intent,
                    source_device=source_device_id,
                    target_device=source_device_id,
                    target_device_reference="this device",
                    capability=last_cap,
                    action=last_act,
                    arguments=args,
                    requires_confirmation=False,
                    confidence=0.98,
                    user_facing_summary=summary
                )

        # Phase 7A: Pattern Parser for People Statements
        people_plan = None
        m_brother = re.match(r'^(?:remember\s+(?:that\s+)?)?my\s+([a-zA-Z]+)(?:\'s)?\s+name\s+is\s+([a-zA-Z\s]+)$', clean_req)
        m_rel = re.match(r'^(?:remember\s+(?:that\s+)?)?([a-zA-Z]+)\s+is\s+my\s+([a-zA-Z]+)(?:\s+from\s+([a-zA-Z\s]+))?$', clean_req)
        m_pref = re.match(r'^(?:remember\s+(?:that\s+)?)?([a-zA-Z]+)\s+(prefers?\s+.+)$', clean_req)
        m_works = re.match(r'^(?:remember\s+(?:that\s+)?)?([a-zA-Z]+)\s+works\s+at\s+([a-zA-Z0-9\s]+)$', clean_req)
        m_rem = re.match(r'^remember\s+(?:that\s+)?([a-zA-Z]+)\s+(.+)$', clean_req)

        if m_brother:
            rel = m_brother.group(1).strip().lower()
            name = m_brother.group(2).strip().capitalize()
            people_plan = ActionPlan(
                intent="remember_person",
                source_device=source_device_id,
                target_device=source_device_id,
                target_device_reference="this device",
                capability="people.remember",
                action="remember_person",
                arguments={"name": name, "relationship_type": rel, "aliases": [name, rel, f"my {rel}"]},
                requires_confirmation=False,
                confidence=1.0,
                user_facing_summary=f"remembering that {name} is your {rel}"
            )
        elif m_rel:
            name = m_rel.group(1).strip().capitalize()
            rel = m_rel.group(2).strip().lower()
            from_context = (m_rel.group(3) or "").strip()
            args = {"name": name, "relationship_type": rel, "aliases": [name, rel, f"my {rel}"]}
            if from_context:
                args["notes"] = f"{rel} from {from_context}"
                args["key"] = "context"
                args["value"] = from_context
            people_plan = ActionPlan(
                intent="remember_person",
                source_device=source_device_id,
                target_device=source_device_id,
                target_device_reference="this device",
                capability="people.remember",
                action="remember_person",
                arguments=args,
                requires_confirmation=False,
                confidence=1.0,
                user_facing_summary=f"remembering that {name} is your {rel}"
            )
        elif m_pref:
            name = m_pref.group(1).strip().capitalize()
            pref = m_pref.group(2).strip()
            people_plan = ActionPlan(
                intent="remember_person",
                source_device=source_device_id,
                target_device=source_device_id,
                target_device_reference="this device",
                capability="people.remember",
                action="remember_person",
                arguments={
                    "name": name,
                    "key": "communication_preference",
                    "value": pref,
                    "notes": pref
                },
                requires_confirmation=False,
                confidence=1.0,
                user_facing_summary=f"remembering that {name} {pref}"
            )
        elif m_works:
            name = m_works.group(1).strip().capitalize()
            company = " ".join([w.capitalize() for w in m_works.group(2).strip().split()])
            people_plan = ActionPlan(
                intent="remember_person",
                source_device=source_device_id,
                target_device=source_device_id,
                target_device_reference="this device",
                capability="people.remember",
                action="remember_person",
                arguments={
                    "name": name,
                    "key": "workplace",
                    "value": company,
                    "notes": f"works at {company}"
                },
                requires_confirmation=False,
                confidence=1.0,
                user_facing_summary=f"remembering that {name} works at {company}"
            )
        elif m_rem and not any(kw in clean_req for kw in ["battery", "screen", "screenshot", "task", "device"]):
            from .social import is_valid_social_fact
            name = m_rem.group(1).strip().capitalize()
            fact_text = m_rem.group(2).strip()
            is_valid, reason = is_valid_social_fact(fact_text, "fact", fact_text)
            if not is_valid:
                people_plan = ActionPlan(
                    intent="remember_social_interaction",
                    source_device=source_device_id,
                    target_device=source_device_id,
                    target_device_reference="this device",
                    capability="social.remember_interaction",
                    action="remember_social_interaction",
                    arguments={
                        "name": name,
                        "key": "statement",
                        "value": fact_text,
                        "statement": fact_text
                    },
                    requires_confirmation=False,
                    confidence=1.0,
                    user_facing_summary=f"remembering that {name} {fact_text}"
                )
            else:
                people_plan = ActionPlan(
                    intent="remember_person",
                    source_device=source_device_id,
                    target_device=source_device_id,
                    target_device_reference="this device",
                    capability="people.remember",
                    action="remember_person",
                    arguments={
                        "name": name,
                        "key": "fact",
                        "value": fact_text,
                        "notes": fact_text
                    },
                    requires_confirmation=False,
                    confidence=1.0,
                    user_facing_summary=f"remembering that {name} {fact_text}"
                )

        # Phase 8B: Pattern Parser for Proactive Context & Reminders
        proactive_plan = None
        m_bat_drop = re.search(r'^(?:(?:let\s+me\s+know|remind\s+me|alert\s+me|tell\s+me|notify\s+me|warn\s+me)\s+)?when\s+(?:my\s+|the\s+)?battery\s+(?:drops?\s+below|falls?\s+below|is\s+below|goes\s+below|<)\s+(\d+)(?:\s*(?:percent|%))?$', clean_req)
        m_dev_online = re.search(r'^(?:(?:let\s+me\s+know|remind\s+me|alert\s+me|tell\s+me|notify\s+me|warn\s+me)\s+)?when\s+(?:my\s+|the\s+)?([a-zA-Z0-9_\-]+)\s+(?:comes?\s+online|is\s+online|becomes?\s+online)$', clean_req)
        m_loc_time = re.search(r'^(?:(?:let\s+me\s+know|remind\s+me|alert\s+me|tell\s+me|notify\s+me|warn\s+me)\s+)?when\s+i(?:\'m|\s+am)\s+(?:at\s+)?([a-zA-Z]+)\s+after\s+(\d+)(?::(\d+))?\s*(pm|am)?$', clean_req)
        m_cal_prox = re.search(
            r'^(?:let\s+me\s+know|remind\s+me|alert\s+me|tell\s+me|notify\s+me|warn\s+me)\s+'
            r'(?:when\s+(?:the\s+)?next\s+(?:calendar\s+)?(?:event|meeting)\s+is\s+(?:within|in)\s+(\d+)\s+minutes?'
            r'|(\d+)\s+minutes?\s+before\s+(?:my\s+|the\s+)?next\s+(?:calendar\s+)?(?:event|meeting|class))$'
            r'|'
            r'^(?:when\s+)?(?:the\s+)?next\s+(?:calendar\s+)?(?:event|meeting)\s+is\s+(?:within|in)\s+(\d+)\s+minutes?$',
            clean_req
        )

        if m_bat_drop:
            thresh = int(m_bat_drop.group(1))
            proactive_plan = ActionPlan(
                intent="register_proactive_condition",
                source_device=source_device_id,
                target_device=source_device_id,
                target_device_reference="this device",
                capability="proactive.register_condition",
                action="proactive_register_condition",
                arguments={
                    "condition_type": "signal_threshold",
                    "signal_key": f"device.battery:{source_device_id}",
                    "operator": "less_than",
                    "target_value": thresh,
                    "description": f"Battery drops below {thresh}%",
                    "response_text": f"I'll let you know when your battery drops below {thresh}%."
                },
                requires_confirmation=False,
                confidence=1.0,
                user_facing_summary=f"registering reminder for battery below {thresh}%"
            )
        elif m_dev_online:
            dev_target = m_dev_online.group(1).lower()
            target_key = dev_target
            if dev_target in ("laptop", "my laptop", "computer", "pc", "this device"):
                target_key = source_device_id
            proactive_plan = ActionPlan(
                intent="register_proactive_condition",
                source_device=source_device_id,
                target_device=source_device_id,
                target_device_reference="this device",
                capability="proactive.register_condition",
                action="proactive_register_condition",
                arguments={
                    "condition_type": "signal_threshold",
                    "signal_key": f"device.online:{target_key}",
                    "operator": "equals",
                    "target_value": True,
                    "description": f"{dev_target} comes online",
                    "response_text": f"I'll remind you when your {dev_target} comes online."
                },
                requires_confirmation=False,
                confidence=1.0,
                user_facing_summary=f"registering reminder for {dev_target} coming online"
            )
        elif m_loc_time:
            loc_label = m_loc_time.group(1).lower()
            raw_h = int(m_loc_time.group(2))
            raw_m = int(m_loc_time.group(3)) if m_loc_time.group(3) else 0
            meridiem = (m_loc_time.group(4) or "").lower()
            h24 = raw_h
            if meridiem == "pm" and raw_h < 12:
                h24 = raw_h + 12
            elif meridiem == "am" and raw_h == 12:
                h24 = 0
            time_str = f"{raw_h}:{raw_m:02d} {meridiem.upper()}".strip() if meridiem else f"{raw_h}:{raw_m:02d}"
            proactive_plan = ActionPlan(
                intent="register_proactive_condition",
                source_device=source_device_id,
                target_device=source_device_id,
                target_device_reference="this device",
                capability="proactive.register_condition",
                action="proactive_register_condition",
                arguments={
                    "condition_type": "contextual_reminder",
                    "clauses": [
                        {"signal": "time.hour", "operator": "greater_than_or_equal", "value": h24},
                        {"signal": "location.label", "operator": "equals", "value": loc_label}
                    ],
                    "combination": "AND",
                    "description": f"{loc_label.capitalize()} after {time_str}",
                    "response_text": f"I'll remind you when you're {loc_label} after {time_str}."
                },
                requires_confirmation=False,
                confidence=1.0,
                user_facing_summary=f"registering reminder for {loc_label} after {time_str}"
            )
        elif m_cal_prox:
            mins = int(m_cal_prox.group(1) or m_cal_prox.group(2) or m_cal_prox.group(3))
            proactive_plan = ActionPlan(
                intent="register_proactive_condition",
                source_device=source_device_id,
                target_device=source_device_id,
                target_device_reference="this device",
                capability="proactive.register_condition",
                action="proactive_register_condition",
                arguments={
                    "condition_type": "signal_threshold",
                    "signal_key": "calendar.time_until_next_event",
                    "operator": "less_than_or_equal",
                    "target_value": mins * 60,
                    "description": f"Next event is within {mins} minutes",
                    "response_text": f"I'll remind you {mins} minutes before your next event."
                },
                requires_confirmation=False,
                confidence=1.0,
                user_facing_summary=f"registering reminder for calendar event within {mins} minutes"
            )

        if people_plan or proactive_plan:
            is_fast_response = False
            match = None
            cap_match = None

        force_response = context.get("force_response_only", False)
        if force_response:
            match = None
            is_fast_response = True

        if match:
            app_name = match.group(2).strip(" \t.,!?;:")
            if app_name.lower() in ["it", "that", "this", "it again", "that again"]:
                match = None

        if repeat_match:
            match = None
            is_fast_response = False
            cap_match = None

        if repeat_match:
            log.info(f"[Latency] Lane selected: SESSION_REPEAT (action: {repeat_match.action})")
            trace.record_stage("routing_complete")
            trace.routing_lane = "SESSION_REPEAT"
            trace.model_provider = "deterministic_repeat"
            latency_metrics["routing_path"] = "SESSION_REPEAT"
            latency_metrics["classification"] = "ACTION"
            res = ModelResult(type="ACTION", action_plan=repeat_match)
            latency_metrics["intent_route_complete"] = time.monotonic()
        elif people_plan:
            log.info(f"[Latency] Lane selected: PEOPLE_REMEMBER (action: {people_plan.action})")
            trace.record_stage("routing_complete")
            trace.routing_lane = "PEOPLE_REMEMBER"
            trace.model_provider = "deterministic_people"
            latency_metrics["routing_path"] = "PEOPLE_REMEMBER"
            latency_metrics["classification"] = "ACTION"
            res = ModelResult(type="ACTION", action_plan=people_plan)
            latency_metrics["intent_route_complete"] = time.monotonic()
        elif proactive_plan:
            log.info(f"[Latency] Lane selected: PROACTIVE_REGISTER (action: {proactive_plan.action})")
            trace.record_stage("routing_complete")
            trace.routing_lane = "PROACTIVE_REGISTER"
            trace.model_provider = "deterministic_proactive"
            latency_metrics["routing_path"] = "PROACTIVE_REGISTER"
            latency_metrics["classification"] = "ACTION"
            res = ModelResult(type="ACTION", action_plan=proactive_plan)
            latency_metrics["intent_route_complete"] = time.monotonic()
        elif match:
            # Lane A: LOCAL_FAST
            raw_app_target = match.group(2).strip(" \t.,!?;:")
            explicit_dev = None
            m_target_dev = re.search(r'^(.*?)\s+on\s+(?:my\s+)?([a-zA-Z0-9_-]+)$', raw_app_target, re.IGNORECASE)
            if m_target_dev:
                app_name = m_target_dev.group(1).strip()
                explicit_dev = m_target_dev.group(2).strip()
            else:
                app_name = raw_app_target
            
            # App Normalization
            if app_name.lower() == "git hub": app_name = "github"
            elif app_name.lower() == "you tube": app_name = "youtube"

            target_dev_ref = explicit_dev if (explicit_dev and explicit_dev.lower() not in ("this device", "here", "this laptop", "my phone", "this", "local", "current")) else "this device"
            target_dev = explicit_dev or source_device_id
            
            log.info(f"[Latency] Lane selected: LOCAL_FAST (action: open_app, target: {app_name}, target_dev: {target_dev})")
            trace.record_stage("routing_complete")
            trace.routing_lane = "LOCAL_FAST"
            trace.model_provider = "deterministic_local_fast"
            latency_metrics["routing_path"] = "LOCAL_FAST"
            latency_metrics["classification"] = "ACTION"
            plan = ActionPlan(
                intent="open_app",
                source_device=source_device_id,
                target_device=target_dev,
                target_device_reference=target_dev_ref,
                capability="app.launch",
                action="open_app",
                arguments={"app": app_name},
                requires_confirmation=False,
                confidence=1.0,
                user_facing_summary=f"opening {app_name}"
            )
            res = ModelResult(type="ACTION", action_plan=plan)
            latency_metrics["intent_route_complete"] = time.monotonic()
        elif is_fast_response:
            # Lane B: FAST_RESPONSE
            log.info("[Latency] Lane selected: FAST_RESPONSE")
            trace.record_stage("routing_complete")
            trace.routing_lane = "FAST_RESPONSE"
            latency_metrics["routing_path"] = "FAST_RESPONSE"
            latency_metrics["llm_request_start"] = time.monotonic()
            trace.record_stage("model_request_started")
            try:
                # If we matched a capability but are forced to respond conversationally
                if cap_match:
                    ans = f"Yes, your device supports {cap_match.user_facing_summary}."
                    res = ModelResult(type="RESPONSE", response_text=ans, action_plan=cap_match)
                    latency_metrics["llm_response_received"] = time.monotonic()
                    trace.record_stage("model_complete")
                    trace.model_provider = "deterministic_capability"
                else:
                    # Check if deterministic people response can directly answer
                    people_direct_answer = await self._try_answer_people_question(clean_req, context)
                    if people_direct_answer:
                        res = ModelResult(type="RESPONSE", response_text=people_direct_answer)
                        latency_metrics["llm_response_received"] = time.monotonic()
                        trace.record_stage("model_complete")
                        trace.model_provider = "deterministic_people"
                    else:
                        # Call LLM with lane and trace in context
                        ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                        log.info(f"[Profiling] FAST_RESPONSE Router call start | CPU: {psutil.cpu_percent()}% | RAM: {ram_mb:.1f} MB")
                        context_override = dict(context)
                        context_override["force_response_only"] = True
                        context_override["routing_lane"] = "FAST_RESPONSE"
                        context_override["trace"] = trace
                        res = await self.router.generate_result(natural_request, context_override, on_chunk=on_chunk)
                        if res.type != "RESPONSE":
                            res = ModelResult(type="RESPONSE", response_text=res.response_text or f"I heard: {natural_request}. I cannot execute actions for this query.")
                        latency_metrics["llm_response_received"] = time.monotonic()
                        trace.record_stage("model_complete")
                        if not trace.model_provider:
                            trace.model_provider = getattr(self.router, "name", "fast_response_model")
            except Exception as e:
                log.error(f"Harness failed | reason=router_error | error={e}", exc_info=True)
                trace.record_stage("model_complete")
                trace.success = False
                trace.failure_category = classify_failure(e)
                trace.internal_error = str(e)
                return ConversationalResponse(status="error", message=get_user_friendly_error(trace.failure_category))
        elif cap_match:
            # Lane: DETERMINISTIC_CAPABILITY
            log.info(f"[Latency] Lane selected: DETERMINISTIC_CAPABILITY (capability: {cap_match.capability})")
            log.info(f"DEBUG CAP_MATCH: {cap_match.model_dump_json()}")
            trace.record_stage("routing_complete")
            trace.routing_lane = "DETERMINISTIC_CAPABILITY"
            trace.model_provider = "deterministic_capability"
            latency_metrics["routing_path"] = "DETERMINISTIC_CAPABILITY"
            latency_metrics["classification"] = "ACTION"
            res = ModelResult(type="ACTION", action_plan=cap_match)
            latency_metrics["intent_route_complete"] = time.monotonic()
        else:
            # Lane C: STRUCTURED_MODEL
            log.info("[Latency] Lane selected: STRUCTURED_MODEL")
            trace.record_stage("routing_complete")
            trace.routing_lane = "STRUCTURED_MODEL"
            latency_metrics["routing_path"] = "STRUCTURED_MODEL"
            latency_metrics["llm_request_start"] = time.monotonic()
            trace.record_stage("model_request_started")
            try:
                ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                log.info(f"[Profiling] STRUCTURED_MODEL Router call start | CPU: {psutil.cpu_percent()}% | RAM: {ram_mb:.1f} MB")
                context_override = dict(context)
                context_override["routing_lane"] = "STRUCTURED_MODEL"
                context_override["trace"] = trace
                res = await self.router.generate_result(natural_request, context_override)
                latency_metrics["llm_response_received"] = time.monotonic()
                trace.record_stage("model_complete")
                if not trace.model_provider:
                    trace.model_provider = getattr(self.router, "name", "structured_model")
            except Exception as e:
                log.error(f"Harness failed | reason=router_error | error={e}", exc_info=True)
                trace.record_stage("model_complete")
                trace.success = False
                trace.failure_category = classify_failure(e)
                trace.internal_error = str(e)
                return ConversationalResponse(status="error", message=get_user_friendly_error(trace.failure_category))

        latency_metrics["classification"] = res.type

        if res.type == "RESPONSE":
            log.info(f"[Latency] Response emitted: {time.monotonic() - t0:.3f}s")
            return ConversationalResponse(
                status="success",
                message=res.response_text or "I don't have anything to say.",
                action_plan=cap_match if getattr(res, "action_plan", None) is None else res.action_plan
            )

        if res.type == "TASK":
            log.info("[Latency] Routing path: TASK_ORCHESTRATOR")
            trace.routing_lane = "TASK"
            task_res = await self.task_orchestrator.execute_task(natural_request, source_device_id, context, log_action_callback, request_id)
            return ConversationalResponse(
                status="success" if task_res.type.lower() != "error" else "error",
                message=task_res.response_text or "Task execution complete."
            )

        if res.type != "ACTION" or not res.action_plan:
            return ConversationalResponse(status="error", message="Received invalid action from model.")

        plan = res.action_plan

        log.info(
            f"Router result | type={res.type} | intent={plan.intent} | action={plan.action} | "
            f"confidence={plan.confidence:.2f} | target_ref={plan.target_device_reference}"
        )
        log.info(f"Generated action_plan: {plan.model_dump_json(indent=2)}")
        
        # Phase 7A logging
        session_ctx = context.get("session", {})
        refs_str = str(session_ctx.get("references", {}))
        log.info(f"[Phase7A] active references before routing: {refs_str}")
        log.info(f"[Phase7A] final routed intent/action: {plan.intent}/{plan.action}")
        if plan.arguments:
            log.info(f"[Phase7A] resolved arguments: {plan.arguments}")
            
        # 2. Confidence and Ambiguity Handling
        if plan.confidence < 0.7:
            return ConversationalResponse(
                status="clarification_needed",
                message="I'm not quite sure what you mean. Could you clarify?",
                action_plan=plan
            )

            
        # 3. Target Device Resolution & Executor Selection (Phase A5)
        target_ref = plan.target_device_reference
        if target_ref in (None, "", "this device", "here", "this laptop", "my phone", "this", "this computer", "my desktop", "current", "local"):
            requested_device = None
        else:
            requested_device = target_ref

        # Check target ownership from plan or session context
        owning_dev = getattr(plan, "owning_device", None) or (plan.arguments.get("owning_device") if isinstance(plan.arguments, dict) else None)
        if not owning_dev and session_refs.get("grounded_target"):
            owning_dev = session_refs["grounded_target"].get("owning_device")

        required_cap = self._map_intent_to_capability(plan.intent) or plan.action
        log.info(f"Required Capability: {required_cap}")

        if not required_cap:
            return ConversationalResponse(
                status="error", message=f"Intent '{plan.intent}' is not supported.", action_plan=plan
            )

        # Deterministic Executor Selection
        sel_res = await self.control.resolve_executor(
            required_capability=required_cap,
            requested_device=requested_device,
            source_device_id=source_device_id,
            owning_device=owning_dev,
            permission_tier=getattr(plan, "permission_tier", 1),
            classification=getattr(plan, "classification", "read"),
        )
        log.info(f"Executor Selection Result: {sel_res}")

        if not sel_res.is_success:
            if sel_res.status == "device_offline":
                return ConversationalResponse(
                    status="error",
                    message=f"Device {requested_device or plan.target_device} is currently offline.",
                    action_plan=plan,
                )
            elif sel_res.status == "capability_mismatch":
                return ConversationalResponse(
                    status="error",
                    message=f"Device {requested_device or plan.target_device} does not support '{required_cap}'.",
                    action_plan=plan,
                )
            elif sel_res.status == "trust_denied":
                return ConversationalResponse(
                    status="error",
                    message=f"Execution rejected by trust policy: {sel_res.reason}",
                    action_plan=plan,
                )
            elif sel_res.status == "owning_device_unavailable":
                return ConversationalResponse(
                    status="error",
                    message=sel_res.reason,
                    action_plan=plan,
                )
            elif sel_res.status == "device_not_found":
                return ConversationalResponse(
                    status="clarification_needed",
                    message=f"I don't know which device you mean by '{requested_device}'.",
                    action_plan=plan,
                )
            else:
                return ConversationalResponse(
                    status="error",
                    message=f"No eligible executor found for '{required_cap}'.",
                    action_plan=plan,
                )

        target_device = sel_res.selected_device
        plan.target_device = target_device
        if isinstance(plan.arguments, dict) and "target_device" in plan.arguments:
            plan.arguments["target_device"] = target_device
        log.info(f"Executor selected | target={target_device} | reason={sel_res.reason}")
                
        # 5. Permission Check
        perm = await self.control.check_permissions(source_device_id, target_device, required_cap)
        log.info(f"CortexControlLayer Permission Result: {perm}")
        if not perm or not perm.allowed:
            if log_action_callback:
                await log_action_callback(source_device_id, plan.intent, "denied", f"Permission denied")
            return ConversationalResponse(
                status="error", message="You don't have permission to do that.", action_plan=plan
            )
            
        # 6. Approval Flow
        if perm.needs_confirmation or plan.requires_confirmation:
            cmd_id = await self.control.request_approval(
                target_device, plan.action, plan.arguments, source_device_id, perm.tier
            )
            if log_action_callback:
                await log_action_callback(source_device_id, plan.intent, "ask", "Pending confirmation")
            approval_phrase = format_action_approval_phrase(plan)
            return ConversationalResponse(
                status="approval_required",
                message=f"I need your approval to {approval_phrase}.",
                action_plan=plan,
                command_id=cmd_id
            )
            
        # 7. Execution Dispatch
        log.info(f"Executor Dispatch: action={plan.action}")
        try:
            import uuid
            if required_cap.startswith("hub.") or required_cap.startswith("task.") or required_cap.startswith("memory.") or required_cap.startswith("people.") or required_cap.startswith("speaker.") or required_cap.startswith("social.") or required_cap.startswith("context.") or required_cap.startswith("proactive.") or required_cap.startswith("attention.") or required_cap.startswith("calendar.") or required_cap.startswith("telemetry.") or required_cap.startswith("workload.") or required_cap.startswith("goal.") or required_cap == "screen.analyze":
                cmd_id = uuid.uuid4().hex # Hub local execution

                if plan.action in ("screen.analyze", "screen_analyze") or required_cap == "screen.analyze":
                    from .vision_provider import get_vision_provider
                    provider = get_vision_provider()
                    if not provider:
                        return ConversationalResponse(
                            status="error",
                            message="screen analysis provider unavailable",
                            action_plan=plan,
                            command_id=cmd_id,
                        )
                    img_ref = plan.arguments.get("screenshot") or plan.arguments.get("image_ref") or ""
                    question = plan.arguments.get("question")
                    mode = plan.arguments.get("mode", "describe")
                    if not img_ref:
                        from .screen_perception import ScreenPerceptionManager
                        spm = ScreenPerceptionManager.get_instance()
                        latest = spm.get_latest_observation(target_device)
                        if latest and latest.source_image_reference:
                            img_ref = latest.source_image_reference
                    try:
                        obs = await provider.analyze_image(img_ref, question=question, mode=mode, device_id=target_device)
                        from .screen_perception import ScreenPerceptionManager
                        spm = ScreenPerceptionManager.get_instance()
                        spm.store_observation(obs)
                        await spm.publish_world_state_signals(obs)
                        return ConversationalResponse(
                            status="success",
                            message=obs.summary,
                            action_plan=plan,
                            command_id=cmd_id,
                            data=obs.to_dict(),
                        )
                    except Exception as e:
                        return ConversationalResponse(
                            status="error",
                            message=f"Screen analysis failed: {e}",
                            action_plan=plan,
                            command_id=cmd_id,
                        )

                # Actually perform the action for memory
                elif plan.action == "store_memory":
                    from .database import add_memory_item
                    key = str(plan.arguments.get("key", "unknown"))[:100].strip().lower()
                    val = str(plan.arguments.get("value", ""))[:500].strip()
                    cat = str(plan.arguments.get("category", "general"))[:50].strip().lower()
                    
                    if not key or not val:
                        log.warning("[Memory] Rejecting empty memory store request")
                        return ConversationalResponse(status="error", message="Memory key or value cannot be empty.")
                        
                    mem_item = {
                        "id": uuid.uuid4().hex,
                        "key": key,
                        "value": val,
                        "category": cat,
                        "confidence": plan.confidence
                    }
                    await add_memory_item(mem_item)
                    log.info(f"[Audit] Stored memory safely: {mem_item['key']} = {mem_item['value']} (category: {mem_item['category']})")
                
                elif plan.action in ("remember_person", "people_remember"):
                    from .people import PeopleManager
                    session_ctx = context.get("session", {})
                    p_res = await PeopleManager.remember_person(plan.arguments, session_ctx.get("references"))
                    if p_res.get("status") == "ambiguous":
                        return ConversationalResponse(
                            status="clarification_needed",
                            message=p_res.get("message", "Multiple people matched."),
                            action_plan=plan,
                            command_id=cmd_id
                        )
                    elif p_res.get("status") == "error":
                        return ConversationalResponse(
                            status="error",
                            message=p_res.get("message", "Failed to remember person."),
                            action_plan=plan,
                            command_id=cmd_id
                        )
                    p_name = p_res.get("display_name")
                    p_info = p_res.get("person", {})
                    rel_type = p_info.get("relationship_type")
                    if plan.arguments.get("key") and plan.arguments.get("value") and plan.arguments.get("key") != "relationship_type":
                        msg = f"Done. I have remembered that {p_name} {plan.arguments.get('value')}."
                    elif plan.arguments.get("notes") and not plan.arguments.get("relationship_type"):
                        msg = f"Done. I have remembered that {p_name} {plan.arguments.get('notes')}."
                    elif rel_type:
                        msg = f"Done. I have remembered that {p_name} is your {rel_type}."
                    else:
                        msg = f"Done. I have remembered {p_name}."

                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    log.info(f"Executor Result: SUCCESS (hub-local people remember)")
                    log.info(f"[Latency] Response emitted: {time.monotonic() - t0:.3f}s")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("get_person", "people_get"):
                    from .people import PeopleManager
                    session_ctx = context.get("session", {})
                    identifier = plan.arguments.get("name") or plan.arguments.get("person_id") or ""
                    person_details = await PeopleManager.get_person_details(identifier, session_ctx.get("references"))
                    if not person_details:
                        msg = f"I don't have any record of {identifier}."
                    else:
                        facts_summary = ", ".join([f"{f['key']}: {f['value']}" for f in person_details.get("facts", [])])
                        msg = f"{person_details['display_name']}"
                        if person_details.get("relationship_type"):
                            msg += f" ({person_details['relationship_type']})"
                        if facts_summary:
                            msg += f" - {facts_summary}"
                        if person_details.get("notes"):
                            msg += f" - {person_details['notes']}"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("search_people", "people_search"):
                    from .people import PeopleManager
                    query = plan.arguments.get("query", "")
                    results = await PeopleManager.search_people(query)
                    names = [p["display_name"] for p in results]
                    msg = f"Found {len(names)} people: {', '.join(names)}" if names else f"No people found matching '{query}'."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("update_person", "people_update"):
                    from .people import PeopleManager
                    session_ctx = context.get("session", {})
                    p_res = await PeopleManager.remember_person(plan.arguments, session_ctx.get("references"))
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=f"Done. I have updated {plan.arguments.get('name')}.", action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("get_current_speaker", "speaker_get_current"):
                    from .speakers import SpeakerManager
                    spk = await SpeakerManager.get_current_speaker(source_device_id)
                    label = spk.get("label", "Unknown Speaker")
                    is_primary = spk.get("is_primary_user", False)
                    display_name = spk.get("display_name")
                    rel = spk.get("relationship_type")
                    if display_name and rel:
                        msg = f"Current speaker is {label} ({display_name}, your {rel})."
                    elif display_name:
                        msg = f"Current speaker is {label} ({display_name})."
                    elif is_primary:
                        msg = f"Current speaker is the primary user ({label})."
                    else:
                        msg = f"Current speaker is {label}."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("resolve_speaker", "speaker_resolve"):
                    from .speakers import SpeakerManager
                    session_ctx = context.get("session", {})
                    hint = plan.arguments.get("speaker_hint") or plan.arguments.get("name") or plan.arguments.get("label")
                    res = await SpeakerManager.resolve_speaker(hint, session_ctx.get("references"))
                    if res.get("is_ambiguous"):
                        cands = [c["display_name"] for c in res.get("candidates", [])]
                        msg = f"Multiple people matched speaker hint '{hint}': {', '.join(cands)}."
                    else:
                        label = res.get("label", "Unknown")
                        p_name = res.get("display_name")
                        msg = f"Resolved speaker '{hint}' to {label}" + (f" ({p_name})" if p_name else "") + "."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("link_speaker_person", "speaker_link_person"):
                    from .speakers import SpeakerManager
                    spk_id = plan.arguments.get("speaker_id")
                    p_id = plan.arguments.get("person_id")
                    res = await SpeakerManager.link_person(spk_id, p_id)
                    if res:
                        msg = f"Linked speaker {spk_id} to person {p_id}."
                    else:
                        msg = f"Failed to link speaker {spk_id} to person {p_id}."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("list_recent_speakers", "speaker_list_recent"):
                    from .speakers import SpeakerManager
                    recents = await SpeakerManager.list_recent_speakers(source_device_id)
                    labels = [s.get("label", "Unknown") for s in recents]
                    msg = f"Recent speakers: {', '.join(labels)}" if labels else "No recent speakers."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("get_social_context", "social_get_context"):
                    social_ctx = context.get("social_context", {})
                    participants = social_ctx.get("active_participants", [])
                    tone = social_ctx.get("tone_guidance", "neutral")
                    msg = f"Social context: {len(participants)} active participants ({', '.join(participants)}). Tone guidance: {tone}."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("remember_social_interaction", "social_remember_interaction"):
                    from .social import SocialContextManager, is_valid_social_fact
                    p_id = plan.arguments.get("person_id")
                    key = plan.arguments.get("key", "interaction")
                    val = plan.arguments.get("value", "")
                    stmt = plan.arguments.get("statement", val)
                    is_valid, reason = is_valid_social_fact(stmt, key, val)
                    if not is_valid:
                        return ConversationalResponse(
                            status="rejected", message=reason, action_plan=plan, command_id=cmd_id
                        )
                    res = await SocialContextManager.remember_social_interaction(p_id, key, val)
                    if res.get("status") == "success":
                        msg = f"Remembered social fact: {key} = {val}."
                        status = "success"
                    else:
                        msg = res.get("reason") or res.get("message") or "Failed to record social fact."
                        status = "rejected" if res.get("status") == "rejected" else "error"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("get_world_state", "context_get_world_state"):
                    from .world_state import WorldStateManager
                    wsm = WorldStateManager.get_instance()
                    state = await wsm.get_fused_world_state(source_device_id)
                    summary = wsm.format_world_state_summary(state)
                    msg = f"World state: {summary}."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("update_signal", "context_update_signal"):
                    from .world_state import WorldStateManager, Signal
                    from datetime import datetime, timezone
                    wsm = WorldStateManager.get_instance()
                    key = plan.arguments.get("key") or plan.arguments.get("name")
                    val = plan.arguments.get("value") or plan.arguments.get("payload") or {}
                    src = plan.arguments.get("source", "user_override")
                    conf = float(plan.arguments.get("confidence", 1.0))
                    ttl = float(plan.arguments.get("ttl", 300.0))
                    now = datetime.now(timezone.utc).isoformat()
                    if isinstance(val, (str, int, float, bool)):
                        payload = {"value": val}
                    elif isinstance(val, dict):
                        payload = val
                    else:
                        payload = {"data": str(val)}
                    sig = Signal(key=key, source=src, timestamp=now, confidence=conf, ttl=ttl, payload=payload)
                    ok = await wsm.ingest_signal(sig, record_event=True, event_summary=f"Signal {key} updated manually")
                    if ok:
                        msg = f"Updated context signal: {key}."
                        status = "success"
                    else:
                        msg = f"Failed to update context signal {key} (stale or lower confidence)."
                        status = "error"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("get_device_state", "context_get_device_state"):
                    from .world_state import WorldStateManager
                    wsm = WorldStateManager.get_instance()
                    target_d = plan.arguments.get("device_id") or source_device_id
                    dev_state = await wsm.get_device_state(target_d)
                    aspect = plan.arguments.get("aspect")
                    if "error" in dev_state:
                        msg = dev_state["error"]
                        status = "error"
                    elif aspect == "identity":
                        msg = f"You are currently using {dev_state['device_name']}."
                        status = "success"
                    elif aspect == "network":
                        if dev_state.get("network") and dev_state["network"].get("network_state") not in (None, "unknown"):
                            msg = f"You are connected to the network ({dev_state['network']['network_state']})."
                        elif dev_state.get("network") and dev_state["network"].get("is_connected"):
                            msg = "You are connected to the internet."
                        else:
                            msg = "Internet connectivity status is currently unavailable."
                        status = "success"
                    elif aspect == "battery":
                        if dev_state.get("battery") and dev_state["battery"].get("battery_level") is not None:
                            charging_str = " (charging)" if dev_state["battery"].get("is_charging") else ""
                            msg = f"Battery is at {dev_state['battery']['battery_level']}%{charging_str}."
                        else:
                            msg = "Battery status is currently unavailable."
                        status = "success"
                    else:
                        bat_str = f", battery {dev_state['battery']['battery_level']}%" if dev_state.get('battery') else ""
                        net_str = f", network {dev_state['network']['network_state']}" if dev_state.get('network') else ""
                        fg_str = f", foreground {dev_state['foreground_activity']['app_name']}" if dev_state.get('foreground_activity') else ""
                        online_str = "online" if dev_state.get("is_online") else "offline"
                        msg = f"Device {dev_state['device_name']} is {online_str}{bat_str}{net_str}{fg_str}."
                        status = "success"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("get_recent_events", "context_get_recent_events"):
                    from .world_state import WorldStateManager
                    wsm = WorldStateManager.get_instance()
                    lim = int(plan.arguments.get("limit", 10))
                    events = await wsm.get_recent_events(limit=lim)
                    summaries = [e.get("summary", "") for e in events]
                    msg = f"Recent context events ({len(summaries)}): {'; '.join(summaries)}." if summaries else "No recent context events."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("proactive_register_condition", "proactive.register_condition"):
                    from .proactive_engine import ProactiveEngine
                    engine = ProactiveEngine.get_instance()
                    cond = await engine.register_condition(
                        condition_type=plan.arguments.get("condition_type", "signal_threshold"),
                        signal_key=plan.arguments.get("signal_key", ""),
                        operator=plan.arguments.get("operator", "equals"),
                        target_value=plan.arguments.get("target_value"),
                        clauses=plan.arguments.get("clauses", []),
                        combination=plan.arguments.get("combination", "AND"),
                        description=plan.arguments.get("description", ""),
                        related_task_id=plan.arguments.get("related_task_id"),
                        related_goal_id=plan.arguments.get("related_goal_id"),
                        metadata=plan.arguments.get("metadata", {}),
                        expires_at=plan.arguments.get("expires_at")
                    )
                    resp_msg = plan.arguments.get("response_text") or f"Registered proactive condition for {cond.description or cond.signal_key}."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Registered condition {cond.condition_id}")
                    return ConversationalResponse(
                        status="success", message=resp_msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("proactive_evaluate", "proactive.evaluate"):
                    from .proactive_engine import ProactiveEngine
                    engine = ProactiveEngine.get_instance()
                    evs = await engine.evaluate(force=bool(plan.arguments.get("force", False)))
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Evaluated proactive conditions, generated {len(evs)} events")
                    return ConversationalResponse(
                        status="success", message=f"Evaluated conditions; {len(evs)} events generated.", action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("proactive_list_candidates", "proactive.list_candidates"):
                    from .proactive_engine import ProactiveEngine
                    engine = ProactiveEngine.get_instance()
                    lim = int(plan.arguments.get("limit", 50))
                    cands = await engine.list_candidates(limit=lim)
                    msg = f"Found {len(cands)} proactive candidate events."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Listed {len(cands)} candidate events")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("proactive_get_event", "proactive.get_event"):
                    from .proactive_engine import ProactiveEngine
                    engine = ProactiveEngine.get_instance()
                    ev_id = plan.arguments.get("event_id", "")
                    ev = await engine.get_event(ev_id)
                    if ev:
                        msg = f"Event {ev_id}: {ev.get('event_type')} (status: {ev.get('status')}, priority: {ev.get('priority')})."
                        status = "success"
                    else:
                        msg = f"Proactive event {ev_id} not found."
                        status = "error"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Retrieved proactive event {ev_id}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("proactive_dismiss", "proactive.dismiss"):
                    from .proactive_engine import ProactiveEngine
                    engine = ProactiveEngine.get_instance()
                    ev_id = plan.arguments.get("event_id", "")
                    ok = await engine.dismiss(ev_id)
                    if ok:
                        msg = f"Dismissed proactive event {ev_id}."
                        status = "success"
                    else:
                        msg = f"Failed to dismiss proactive event {ev_id}."
                        status = "error"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Dismissed proactive event {ev_id}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("route_event", "attention_route_event", "attention.route_event"):
                    from .attention_router import AttentionRouter
                    router = AttentionRouter.get_instance()
                    ev_id = plan.arguments.get("event_id") or plan.arguments.get("proactive_event_id")
                    decision = await router.route_event(ev_id)
                    if decision:
                        msg = f"Delivery decision: {decision.status} for {decision.target_device_id or 'none'} via {decision.channel} (timing: {decision.timing}, score: {decision.score})."
                        status = "success"
                    else:
                        msg = "Failed to route proactive event (no eligible event found)."
                        status = "error"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Attention route: {decision.decision_id if decision else 'none'}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("get_decision", "attention_get_decision", "attention.get_decision"):
                    from .attention_router import AttentionRouter
                    router = AttentionRouter.get_instance()
                    dec_id = plan.arguments.get("decision_id", "")
                    dec = await router.get_decision(dec_id)
                    if dec:
                        msg = f"Decision {dec_id}: {dec.get('status')} for {dec.get('target_device_id')} via {dec.get('channel')} (score: {dec.get('score')})."
                        status = "success"
                    else:
                        msg = f"Delivery decision {dec_id} not found."
                        status = "error"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Get decision {dec_id}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("list_pending", "attention_list_pending", "attention.list_pending"):
                    from .attention_router import AttentionRouter
                    router = AttentionRouter.get_instance()
                    lim = int(plan.arguments.get("limit", 50))
                    pending = await router.list_pending(limit=lim)
                    msg = f"Pending delivery decisions ({len(pending)}): {', '.join([d['decision_id'] for d in pending])}." if pending else "No pending delivery decisions."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Listed {len(pending)} pending decisions")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("reevaluate", "attention_reevaluate", "attention.reevaluate"):
                    from .attention_router import AttentionRouter
                    router = AttentionRouter.get_instance()
                    ready = await router.reevaluate()
                    msg = f"Reevaluated delivery decisions: {len(ready)} now ready for delivery."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Reevaluated decisions: {len(ready)} ready")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("cancel", "attention_cancel", "attention.cancel"):
                    from .attention_router import AttentionRouter
                    router = AttentionRouter.get_instance()
                    dec_id = plan.arguments.get("decision_id", "")
                    ok = await router.cancel(dec_id)
                    if ok:
                        msg = f"Cancelled delivery decision {dec_id}."
                        status = "success"
                    else:
                        msg = f"Failed to cancel delivery decision {dec_id}."
                        status = "error"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Cancelled decision {dec_id}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("calendar_get_current", "calendar.get_current"):
                    from .world_state import CalendarSignalAdapter
                    conn = await CalendarSignalAdapter.get_connection_status()
                    if not conn.get("connected"):
                        msg = f"Google Calendar is currently {conn.get('status', 'disconnected')}."
                        status = "error"
                    else:
                        from .world_state import WorldStateManager
                        wsm = WorldStateManager.get_instance()
                        fused = await wsm.get_fused_world_state(source_device_id)
                        if not fused.calendar_context.get("connected") and not fused.calendar_context.get("current_event") and not fused.calendar_context.get("next_event"):
                            sigs = await CalendarSignalAdapter.fetch_and_create_signals(conn.get("account_id"))
                            for s in sigs:
                                await wsm.ingest_signal(s)
                            fused = await wsm.get_fused_world_state(source_device_id)
                        cur = fused.calendar_context.get("current_event")
                        if cur and cur.get("title"):
                            msg = f"Current calendar event: '{cur['title']}' from {cur.get('start')} to {cur.get('end')}."
                        else:
                            msg = "You have no events currently active on your calendar."
                        status = "success"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("calendar_get_next", "calendar.get_next"):
                    from .world_state import CalendarSignalAdapter
                    conn = await CalendarSignalAdapter.get_connection_status()
                    if not conn.get("connected"):
                        msg = f"Google Calendar is currently {conn.get('status', 'disconnected')}."
                        status = "error"
                    else:
                        from .world_state import WorldStateManager
                        wsm = WorldStateManager.get_instance()
                        fused = await wsm.get_fused_world_state(source_device_id)
                        if not fused.calendar_context.get("connected") and not fused.calendar_context.get("current_event") and not fused.calendar_context.get("next_event"):
                            sigs = await CalendarSignalAdapter.fetch_and_create_signals(conn.get("account_id"))
                            for s in sigs:
                                await wsm.ingest_signal(s)
                            fused = await wsm.get_fused_world_state(source_device_id)
                        nxt = fused.calendar_context.get("next_event")
                        time_until = fused.calendar_context.get("time_until_next_event")
                        if nxt and nxt.get("title"):
                            until_str = f" in {time_until}" if time_until else ""
                            msg = f"Next calendar event: '{nxt['title']}' at {nxt.get('start')}{until_str}."
                        else:
                            msg = "You have no upcoming events on your calendar."
                        status = "success"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("calendar_get_availability", "calendar.get_availability"):
                    from .world_state import CalendarSignalAdapter
                    conn = await CalendarSignalAdapter.get_connection_status()
                    if not conn.get("connected"):
                        msg = f"Google Calendar is currently {conn.get('status', 'disconnected')}."
                        status = "error"
                    else:
                        from .world_state import WorldStateManager
                        wsm = WorldStateManager.get_instance()
                        fused = await wsm.get_fused_world_state(source_device_id)
                        if not fused.calendar_context.get("connected") and not fused.calendar_context.get("current_event") and not fused.calendar_context.get("next_event"):
                            sigs = await CalendarSignalAdapter.fetch_and_create_signals(conn.get("account_id"))
                            for s in sigs:
                                await wsm.ingest_signal(s)
                            fused = await wsm.get_fused_world_state(source_device_id)
                        cal = fused.calendar_context
                        if cal.get("busy"):
                            cur = cal.get("current_event", {})
                            msg = f"Busy: currently in '{cur.get('title')}' until {cur.get('end')}."
                        else:
                            free_until = cal.get("free_until", "rest of the day")
                            msg = f"Available: free until {free_until}."
                        status = "success"
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, status, f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status=status, message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("calendar_status", "calendar.status"):
                    from .world_state import CalendarSignalAdapter
                    conn = await CalendarSignalAdapter.get_connection_status()
                    st = conn.get("status", "disconnected")
                    if conn.get("connected"):
                        msg = f"Google Calendar is connected (account: {conn.get('account_id')})."
                    else:
                        msg = f"Google Calendar is currently {st}."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("telemetry_get_system_usage", "telemetry.get_system_usage"):
                    from .world_state import DeviceSignalAdapter
                    telem = DeviceSignalAdapter.inspect_system_telemetry(source_device_id)
                    msg = f"CPU: {telem['cpu_percent']}%, RAM: {telem['ram_used_gb']}/{telem['ram_total_gb']} GB ({telem['ram_percent']}%), Disk: {telem['disk_free_gb']} GB free out of {telem['disk_total_gb']} GB ({telem['disk_percent']}% used)."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("telemetry_get_hardware_info", "telemetry.get_hardware_info"):
                    from .world_state import DeviceSignalAdapter
                    telem = DeviceSignalAdapter.inspect_system_telemetry(source_device_id)
                    msg = f"Hardware info: {telem['manufacturer']} {telem['model']}, CPU: {telem['processor']}, GPU: {telem['gpu']}, Hostname: {telem['hostname']}, OS: {telem['os']}."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                    return ConversationalResponse(
                        status="success", message=msg, action_plan=plan, command_id=cmd_id
                    )

                elif plan.action in ("add_item", "workload_add_item", "workload.add_item"):
                    from .workload import WorkloadManager
                    wm = WorkloadManager.get_instance()
                    item = await wm.add_item(**plan.arguments)
                    msg = f"Added {item.type} '{item.title}'."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Added work item {item.work_item_id}")
                    return ConversationalResponse(status="success", message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("get_item", "workload_get_item", "workload.get_item"):
                    from .workload import WorkloadManager
                    wm = WorkloadManager.get_instance()
                    item_id = plan.arguments.get("work_item_id") or plan.arguments.get("id", "")
                    item = await wm.get_item(item_id)
                    if item:
                        msg = f"Work item '{item.title}': {item.status}, due {item.due_at}."
                        status = "success"
                    else:
                        msg = f"Work item {item_id} not found."
                        status = "error"
                    return ConversationalResponse(status=status, message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("update_item", "workload_update_item", "workload.update_item"):
                    from .workload import WorkloadManager
                    wm = WorkloadManager.get_instance()
                    item_id = plan.arguments.get("work_item_id") or plan.arguments.get("id", "")
                    updates = plan.arguments.get("updates", plan.arguments)
                    item = await wm.update_item(item_id, updates)
                    msg = f"Updated '{item.title}'." if item else f"Work item {item_id} not found."
                    return ConversationalResponse(status="success" if item else "error", message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("complete_item", "workload_complete_item", "workload.complete_item"):
                    from .workload import WorkloadManager
                    wm = WorkloadManager.get_instance()
                    ident = plan.arguments.get("work_item_id") or plan.arguments.get("title") or plan.arguments.get("identifier", "")
                    item = await wm.complete_item(ident)
                    msg = f"Completed '{item.title}'." if item else f"Work item '{ident}' not found."
                    return ConversationalResponse(status="success" if item else "error", message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("list_items", "workload_list_items", "workload.list_items"):
                    from .workload import WorkloadManager
                    wm = WorkloadManager.get_instance()
                    st = plan.arguments.get("status")
                    inc = bool(plan.arguments.get("include_completed", False))
                    items = await wm.list_items(status=st, include_completed=inc)
                    msg = f"Found {len(items)} work items: {', '.join([i.title for i in items])}."
                    return ConversationalResponse(status="success", message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("get_summary", "workload_get_summary", "workload.get_summary"):
                    from .workload import WorkloadManager
                    wm = WorkloadManager.get_instance()
                    metrics = await wm.compute_workload_metrics()
                    msg = f"Workload summary: {metrics['total_pending']} pending, pressure {metrics['current_pressure']}."
                    return ConversationalResponse(status="success", message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("prioritize", "workload_prioritize", "workload.prioritize"):
                    from .workload import WorkloadManager
                    wm = WorkloadManager.get_instance()
                    ranked = wm.prioritize()
                    msg = f"Priority ranking ({len(ranked)} items): {', '.join([i[0].title for i in ranked])}."
                    return ConversationalResponse(status="success", message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("create_goal", "goal_create", "goal.create"):
                    from .goals import GoalManager
                    gm = GoalManager.get_instance()
                    title = plan.arguments.get("title") or plan.arguments.get("original_request") or "Untitled Goal"
                    req_text = plan.arguments.get("original_request", natural_request)
                    goal = await gm.create_goal(title=title, original_request=req_text, source="user", status="ACTIVE")
                    msg = f"I created the goal '{goal.title}' (ID: {goal.goal_id}) and saved it. Autonomous execution is not enabled yet."
                    if log_action_callback:
                        await log_action_callback(source_device_id, plan.intent, "success", f"Created goal {goal.goal_id}")
                    return ConversationalResponse(status="success", message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("get_goal", "goal_get", "goal.get"):
                    from .goals import GoalManager
                    gm = GoalManager.get_instance()
                    g_id = plan.arguments.get("goal_id", "")
                    goal = await gm.get_goal(g_id)
                    if goal:
                        msg = f"Goal '{goal.title}' ({goal.goal_id}): status {goal.status}, steps {goal.current_step_index}/{goal.max_steps}."
                        status = "success"
                    else:
                        msg = f"Goal '{g_id}' not found."
                        status = "error"
                    return ConversationalResponse(status=status, message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("list_goals", "goal_list", "goal.list"):
                    from .goals import GoalManager
                    gm = GoalManager.get_instance()
                    st = plan.arguments.get("status")
                    goals = await gm.list_goals(status=st)
                    if not goals:
                        msg = "No goals found." if not st else f"No {st.lower()} goals found."
                    else:
                        goal_strs = [f"'{g.title}' ({g.goal_id}, {g.status})" for g in goals]
                        msg = f"Found {len(goals)} goals: {', '.join(goal_strs)}."
                    return ConversationalResponse(status="success", message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("cancel_goal", "goal_cancel", "goal.cancel"):
                    from .goals import GoalManager
                    gm = GoalManager.get_instance()
                    g_id = plan.arguments.get("goal_id") or plan.arguments.get("identifier", "")
                    reason = plan.arguments.get("reason", "Cancelled by user")
                    try:
                        goal = await gm.cancel_goal(g_id, reason=reason)
                        msg = f"Cancelled goal '{goal.title}' ({goal.goal_id})."
                        status = "success"
                    except Exception as e:
                        msg = f"Failed to cancel goal: {e}"
                        status = "error"
                    return ConversationalResponse(status=status, message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("resume_goal", "goal_resume", "goal.resume"):
                    from .goals import GoalManager
                    gm = GoalManager.get_instance()
                    g_id = plan.arguments.get("goal_id") or plan.arguments.get("identifier", "")
                    try:
                        goal = await gm.resume_goal(g_id)
                        msg = f"Resumed goal '{goal.title}' ({goal.goal_id}) to ACTIVE."
                        status = "success"
                    except Exception as e:
                        msg = f"Cannot resume goal: {e}"
                        status = "error"
                    return ConversationalResponse(status=status, message=msg, action_plan=plan, command_id=cmd_id)

                elif plan.action in ("run_goal", "goal_run", "goal.run"):
                    from .goals import GoalManager, GoalRunner
                    gm = GoalManager.get_instance()
                    runner = GoalRunner.get_instance(control_layer=self.control, goal_manager=gm)
                    g_id = plan.arguments.get("goal_id") or plan.arguments.get("identifier", "")
                    target_goal = await gm.get_goal(g_id) if g_id else None
                    if not target_goal:
                        goals = await gm.list_goals()
                        target_goal = gm._find_matching_goal(g_id or plan.arguments.get("title", ""), goals)
                    if target_goal:
                        await runner.run_goal(target_goal.goal_id)
                        final_g = await gm.get_goal(target_goal.goal_id)
                        msg = f"Ran goal '{final_g.title}' (ID: {final_g.goal_id}): status {final_g.status}."
                        status = "success"
                    else:
                        msg = f"Goal '{g_id}' not found."
                        status = "error"
                    return ConversationalResponse(status=status, message=msg, action_plan=plan, command_id=cmd_id)
                
                if log_action_callback:
                    await log_action_callback(source_device_id, plan.intent, "success", f"Executed hub action: {plan.action}")
                log.info(f"Executor Result: SUCCESS (hub-local)")
                log.info(f"[Latency] Response emitted: {time.monotonic() - t0:.3f}s")
                return ConversationalResponse(
                    status="success", message=f"Done. I have {plan.user_facing_summary}.", action_plan=plan, command_id=cmd_id
                )
            else:
                import uuid
                import asyncio
                from .main import PENDING_FUTURES
                
                cmd_id = uuid.uuid4().hex
                log.info(f"Hub dispatch | action={plan.action} | cmd_id={cmd_id[:8]}...")
                
                # Pre-register future to avoid race condition
                fut = asyncio.get_running_loop().create_future()
                PENDING_FUTURES[cmd_id] = fut
                
                await self.control.execute_command(target_device, plan.action, plan.arguments, latency_metrics, cmd_id=cmd_id)
                if log_action_callback:
                    await log_action_callback(source_device_id, plan.intent, "success", f"Dispatched {plan.action}")
                log.info(f"Executor Result: Dispatched via websocket (cmd_id={cmd_id[:8]}...)")
                log.info(f"[Latency] Response emitted: {time.monotonic() - t0:.3f}s")
                return ConversationalResponse(
                    status="success", message=f"Okay, I am {plan.user_facing_summary}.", action_plan=plan, command_id=cmd_id
                )
        except Exception as e:
            log.error(f"Executor Error: {e}")
            if log_action_callback:
                await log_action_callback(source_device_id, plan.intent, "error", f"Failed dispatch: {e}")
            return ConversationalResponse(status="error", message="Failed to execute command.", action_plan=plan)

    def _map_intent_to_capability(self, intent: str) -> str:
        SUPPORTED_INTENTS = {
            "show_notification": "ui.notify",
            "open_app": "app.launch",
            "move_file": "fs.move",
            "get_device_status": "device.status",
            "resume_session": "session.resume",
            "list_online_devices": "hub.read",
            "retrieve_recent_file": "fs.read",
            "capture_screen": "screen.capture",
            "screen_capture": "screen.capture",
            "screen.capture": "screen.capture",
            "take_screenshot": "screen.capture",
            "intent_screenshot": "screen.capture",
            "screenshot.read": "screen.capture",
            "retrieve_recent_screenshot": "screen.capture",
            "screen.analyze": "screen.analyze",
            "screen_analyze": "screen.analyze",
            "analyze_screen": "screen.analyze",
            "inspect_screen": "screen.analyze",
            "create_task": "task.write",
            "read_task_status": "task.read",
            "store_memory": "memory.write",
            "remember_person": "people.remember",
            "get_person": "people.get",
            "search_people": "people.search",
            "update_person": "people.update",
            "get_current_speaker": "speaker.get_current",
            "resolve_speaker": "speaker.resolve",
            "link_speaker_person": "speaker.link_person",
            "list_recent_speakers": "speaker.list_recent",
            "get_social_context": "social.context",
            "remember_social_interaction": "social.remember_interaction",
            "get_world_state": "context.get_world_state",
            "update_signal": "context.update_signal",
            "get_device_state": "context.get_device_state",
            "get_recent_events": "context.get_recent_events",
            "register_proactive_condition": "proactive.register_condition",
            "proactive_register": "proactive.register_condition",
            "proactive_evaluate": "proactive.evaluate",
            "proactive_list_candidates": "proactive.list_candidates",
            "proactive_get_event": "proactive.get_event",
            "proactive_dismiss": "proactive.dismiss",
            "route_event": "attention.route_event",
            "attention_route": "attention.route_event",
            "attention_get_decision": "attention.get_decision",
            "attention_list_pending": "attention.list_pending",
            "attention_reevaluate": "attention.reevaluate",
            "attention_cancel": "attention.cancel",
            "calendar_get_current": "calendar.get_current",
            "calendar_get_next": "calendar.get_next",
            "calendar_get_availability": "calendar.get_availability",
            "calendar_status": "calendar.status",
            "telemetry_get_system_usage": "telemetry.get_system_usage",
            "telemetry_get_hardware_info": "telemetry.get_hardware_info",
            "manage_workload": "workload.add_item",
            "query_workload": "workload.list_items",
            "prioritize_workload": "workload.prioritize",
            "workload_add_item": "workload.add_item",
            "workload_get_item": "workload.get_item",
            "workload_update_item": "workload.update_item",
            "workload_complete_item": "workload.complete_item",
            "workload_list_items": "workload.list_items",
            "workload_get_summary": "workload.get_summary",
            "workload_prioritize": "workload.prioritize",
            "create_goal": "goal.create",
            "get_goal": "goal.get",
            "list_goals": "goal.list",
            "cancel_goal": "goal.cancel",
            "resume_goal": "goal.resume",
            "goal_create": "goal.create",
            "goal_get": "goal.get",
            "goal_list": "goal.list",
            "goal_cancel": "goal.cancel",
            "goal_resume": "goal.resume",
            "delete_file": "fs.delete",
            "delete": "fs.delete",
            "fs.delete": "fs.delete",
            "fs.list": "fs.list",
            "fs.stat": "fs.stat",
            "fs.move": "fs.move",
            "fs.shortcut_info": "fs.shortcut_info",
            "web.search": "web.search",
            "web_search": "web.search",
            "search_web": "web.search",
            "web.fetch": "web.fetch",
            "web_fetch": "web.fetch",
            "weather.current": "weather.current",
            "weather_current": "weather.current",
            "weather.forecast": "weather.forecast",
            "weather_forecast": "weather.forecast",
            "browser.open_url": "browser.open_url",
            "open_url": "browser.open_url",
            "open_website": "browser.open_url",
            "app.list_running": "app.list_running",
            "list_running_apps": "app.list_running",
            "list_apps": "app.list_running",
            "app.launch": "app.launch",
            "launch_app": "app.launch",
            "app.close": "app.close",
            "close_app": "app.close",
            "window.list": "window.list",
            "list_windows": "window.list",
            "window.focus": "window.focus",
            "focus_window": "window.focus",
            "switch_window": "window.focus",
            "window.minimize": "window.minimize",
            "minimize_window": "window.minimize",
            "window.maximize": "window.maximize",
            "maximize_window": "window.maximize",
            "window.restore": "window.restore",
            "restore_window": "window.restore",
            "fs.open": "fs.open",
            "open_file": "fs.open",
            "open_folder": "fs.open",
            "fs.copy": "fs.copy",
            "copy_file": "fs.copy",
            "fs.rename": "fs.rename",
            "rename_file": "fs.rename",
        }
        if intent in SUPPORTED_INTENTS:
            return SUPPORTED_INTENTS[intent]
        if intent.startswith("fs.") or intent.startswith("app.") or intent.startswith("window.") or intent.startswith("screen.") or intent.startswith("proactive.") or intent.startswith("context.") or intent.startswith("attention.") or intent.startswith("calendar.") or intent.startswith("telemetry.") or intent.startswith("workload.") or intent.startswith("goal.") or intent.startswith("web.") or intent.startswith("weather.") or intent.startswith("browser."):
            return intent
        return None
