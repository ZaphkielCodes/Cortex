from typing import Dict, Any, List, Optional
import asyncio
import time
from .database import get_session_context, save_session_context

import logging

log = logging.getLogger("cortex.session")

class SessionContextManager:
    """
    Maintains bounded recent-context window per device.
    """
    MAX_TURNS = 10
    _memory_cache: Dict[str, dict] = {}
    _update_locks: dict = {}

    @staticmethod
    async def get_session(device_id: str) -> dict:
        if device_id in SessionContextManager._memory_cache:
            session = SessionContextManager._memory_cache[device_id]
            log.info(f"[Phase7A] session_id={device_id} | context came from: memory | turns={len(session['turns'])} | refs={session['references']}")
            return session
            
        session = await get_session_context(device_id)
        SessionContextManager._memory_cache[device_id] = session
        log.info(f"[Phase7A] session_id={device_id} | context came from: SQLite reload | turns={len(session['turns'])} | refs={session['references']}")
        return session

    @staticmethod
    async def set_grounded_target(device_id: str, grounded_target: dict) -> None:
        """Persist grounded filesystem target into session references."""
        session = await SessionContextManager.get_session(device_id)
        session["references"]["grounded_target"] = grounded_target
        if grounded_target.get("matched_path"):
            session["references"]["last_grounded_path"] = grounded_target["matched_path"]
        if grounded_target.get("target_name"):
            session["references"]["last_action_target"] = grounded_target["target_name"]
        await save_session_context(
            device_id,
            session.get("turns", []),
            session.get("references", {}),
            session.get("active_task"),
        )

    @staticmethod
    async def set_pending_action(device_id: str, pending_action: Optional[dict]) -> None:
        """Persist or clear pending conversational action in session references."""
        session = await SessionContextManager.get_session(device_id)
        if pending_action:
            session["references"]["pending_action"] = pending_action
        else:
            session["references"].pop("pending_action", None)
        await save_session_context(
            device_id,
            session.get("turns", []),
            session.get("references", {}),
            session.get("active_task"),
        )

    @staticmethod
    async def set_screenshot_ref(device_id: str, image_ref: str, observation: Optional[dict] = None) -> None:
        """Persist screenshot and screen observation references."""
        session = await SessionContextManager.get_session(device_id)
        session["references"]["last_screenshot_ref"] = image_ref
        session["references"]["last_screenshot_device"] = device_id
        if observation:
            session["references"]["last_screen_observation"] = observation
            if isinstance(observation, dict) and observation.get("summary"):
                session["references"]["last_screen_summary"] = observation["summary"]
        await save_session_context(
            device_id,
            session.get("turns", []),
            session.get("references", {}),
            session.get("active_task"),
        )

    @staticmethod
    async def set_foreground_app(device_id: str, app_name: str, window_title: Optional[str] = None) -> None:
        """Persist authoritative foreground application and active window."""
        session = await SessionContextManager.get_session(device_id)
        session["references"]["foreground_app"] = app_name
        session["references"]["current_app"] = app_name
        session["references"]["last_confirmed_app"] = app_name
        if window_title:
            session["references"]["active_window"] = window_title
        await save_session_context(
            device_id,
            session.get("turns", []),
            session.get("references", {}),
            session.get("active_task"),
        )

    @staticmethod
    async def update_session(
        device_id: str,
        request: str,
        response: dict,
        action_plan: dict = None,
        speaker_info: Optional[dict] = None
    ) -> None:
        loop = asyncio.get_running_loop()
        entry = SessionContextManager._update_locks.get(device_id)
        if entry is None or entry[0] is not loop:
            entry = (loop, asyncio.Lock())
            SessionContextManager._update_locks[device_id] = entry
        async with entry[1]:
            await SessionContextManager._update_session(device_id, request, response, action_plan, speaker_info)

    @staticmethod
    async def _update_session(device_id, request, response, action_plan=None, speaker_info=None):
        session = await SessionContextManager.get_session(device_id)
        response_data = response.get("data")
        task_id = response.get("task_id") or (response_data.get("task_id") if isinstance(response_data, dict) else None)
        existing_index = next((i for i, turn in enumerate(session["turns"])
            if task_id and turn.get("task_id") == task_id), None)
        existing = session["turns"][existing_index] if existing_index is not None else None
        if existing:
            if existing.get("request") != request:
                log.warning("session_task_request_mismatch device=%s task_id=%s", device_id, task_id)
                return
            # Completion may arrive before the initial acknowledgement is persisted.
            if response.get("status") == "in_progress" and existing.get("response_status") not in (None, "in_progress"):
                if speaker_info:
                    existing.update(speaker_id=speaker_info.get("speaker_id"),
                        speaker_label=speaker_info.get("label", "User"),
                        person_id=speaker_info.get("person_id"), person_name=speaker_info.get("display_name"),
                        relationship_type=speaker_info.get("relationship_type"))
                    await save_session_context(device_id, session["turns"], session["references"], session.get("active_task"))
                return
            if (existing.get("response_status"), existing.get("response_message")) == (response.get("status"), response.get("message")):
                return
        
        # If speaker info wasn't explicitly passed, resolve/default to primary
        if not speaker_info:
            try:
                from .speakers import SpeakerManager
                speaker_info = await SpeakerManager.resolve_speaker(session_refs=session.get("references"))
            except Exception:
                speaker_info = {"speaker_id": "spk_primary", "label": "Primary User", "is_primary_user": True}

        # Add new turn
        turn = {
            "timestamp": time.time(),
            "request": request,
            "response_status": response.get("status"),
            "response_message": response.get("message"),
            "speaker_id": speaker_info.get("speaker_id") if speaker_info else None,
            "speaker_label": speaker_info.get("label") if speaker_info else "User",
            "person_id": speaker_info.get("person_id") if speaker_info else None,
            "person_name": speaker_info.get("display_name") if speaker_info else None,
            "relationship_type": speaker_info.get("relationship_type") if speaker_info else None,
        }
        if action_plan:
            turn["intent"] = action_plan.get("intent")
            turn["action"] = action_plan.get("action")
            turn["target"] = action_plan.get("target_device_reference")
            turn["arguments"] = action_plan.get("arguments", {})
        
        if task_id:
            turn["task_id"] = task_id
        if existing is not None:
            # Preserve the original request time and speaker when completing its turn.
            session["turns"][existing_index] = {**turn, **existing,
                "response_status": response.get("status"), "response_message": response.get("message")}
        else:
            session["turns"].append(turn)
        
        # Bound context
        if len(session["turns"]) > SessionContextManager.MAX_TURNS:
            session["turns"] = session["turns"][-SessionContextManager.MAX_TURNS:]
            
        # simple heuristic: if an action is performed, add it to references
        if action_plan:
            action = action_plan.get("action")
            args = action_plan.get("arguments", {})
            session["references"]["last_intent"] = action_plan.get("intent")
            session["references"]["last_action"] = action
            session["references"]["last_capability"] = action_plan.get("capability")

            if action in ("open_app", "app.launch") and ("app" in args or "target" in args):
                app_val = args.get("app") or args.get("target")
                session["references"]["last_app"] = app_val
                session["references"]["last_action_target"] = app_val
                session["references"]["last_target"] = app_val
                session["references"]["foreground_app"] = app_val
                session["references"]["current_app"] = app_val
                session["references"]["last_confirmed_app"] = app_val
            elif action in ("close_app", "app.close"):
                closed = args.get("app") or args.get("title")
                if closed:
                    session["references"]["last_closed_app"] = closed
                    session["references"]["last_action_target"] = closed
                    session["references"]["last_target"] = closed
                    if session["references"].get("foreground_app") == closed:
                        session["references"].pop("foreground_app", None)
                        session["references"].pop("current_app", None)
            elif action == "move_file" and "source" in args:
                session["references"]["last_file"] = args["source"]
                session["references"]["last_action_target"] = args["source"]
                session["references"]["last_target"] = args["source"]
            elif action in ("delete_file", "delete", "fs.delete") and "path" in args:
                session["references"]["last_file"] = args["path"]
                session["references"]["last_action_target"] = args["path"]
                session["references"]["last_target"] = args["path"]

            if action_plan.get("grounded_target"):
                session["references"]["grounded_target"] = action_plan["grounded_target"]
                gt_name = action_plan["grounded_target"].get("target_name")
                if gt_name:
                    session["references"]["last_action_target"] = gt_name
                    session["references"]["last_target"] = gt_name

        # Capture screenshot references from action plan or response message
        if action_plan and action_plan.get("action") in ("screen.capture", "take_screenshot"):
            session["references"]["last_action"] = "screen.capture"
            session["references"]["last_screenshot_device"] = device_id
        if response and isinstance(response, dict):
            resp_msg = response.get("message", "")
            if "Screenshot saved: " in resp_msg:
                s_ref = resp_msg.replace("Screenshot saved: ", "").strip()
                session["references"]["last_screenshot_ref"] = s_ref
                session["references"]["last_screenshot_device"] = device_id
            elif "Screenshot taken: " in resp_msg:
                s_ref = resp_msg.replace("Screenshot taken: ", "").strip()
                session["references"]["last_screenshot_ref"] = s_ref
                session["references"]["last_screenshot_device"] = device_id
            if response.get("data") and isinstance(response.get("data"), dict):
                data_img = response["data"].get("image_ref") or response["data"].get("source_image_reference")
                if data_img:
                    session["references"]["last_screenshot_ref"] = data_img
                    session["references"]["last_screenshot_device"] = device_id
                if response["data"].get("observation_id"):
                    session["references"]["last_screen_observation"] = response["data"]
                    if response["data"].get("summary"):
                        session["references"]["last_screen_summary"] = response["data"]["summary"]

        # Phase 8D: Track calendar topic for follow-up resolution
        # Detect if the request was a calendar query and store the topic
        import re as _re
        _clean_req = request.lower().strip(" \t.,!?;:")
        _cal_topic = None
        if _re.search(r'\bnext\s+event\b|\bnext\s+meeting\b|\bupcoming\s+event\b', _clean_req):
            _cal_topic = "next_event"
        elif _re.search(r'\btomorrow\b', _clean_req):
            _cal_topic = "tomorrow"
        elif _re.search(r'\bday\s+after\s+tomorrow\b', _clean_req):
            _cal_topic = "day_after"
        elif _re.search(r'\btoday\b|\bright\s+now\b|\bat\s+the\s+moment\b', _clean_req):
            _cal_topic = "today"
        elif _re.search(r'\bcalendar\b|\bschedule\b|\bagenda\b|\bmeeting\b|\bevent\b', _clean_req):
            _cal_topic = "calendar_general"
        if _cal_topic:
            session["references"]["last_calendar_topic"] = _cal_topic
        elif session["references"].get("last_calendar_topic") and not _re.search(
            r'\bcalendar\b|\bschedule\b|\bagenda\b|\bmeeting\b|\bevent\b|\btomorrow\b|\btoday\b|\bnext\s+event\b',
            _clean_req
        ):
            # Clear calendar topic after a non-calendar turn to avoid stale follow-ups
            # (keep it for up to 2 turns by not clearing eagerly)
            pass


        # Track person references generically
        person_name = speaker_info.get("display_name") if speaker_info else None
        person_id = speaker_info.get("person_id") if speaker_info else None
        if action_plan and action_plan.get("action") in ("remember_person", "get_person", "update_person", "search_people"):
            args = action_plan.get("arguments", {})
            person_name = args.get("name") or args.get("display_name") or person_name
            person_id = args.get("person_id") or person_id
            
        if response and isinstance(response, dict):
            if response.get("person_id"):
                person_id = response.get("person_id")
            if response.get("display_name"):
                person_name = response.get("display_name")

        # Generic resolution if not explicitly in action args
        if not person_name:
            try:
                from .people import PeopleManager
                res = await PeopleManager.resolve_person(request, session["references"])
                if res.matched and res.person:
                    person_name = res.person["display_name"]
                    person_id = res.person["person_id"]
            except Exception:
                pass

        if person_name:
            session["references"]["last_person_name"] = person_name
            if person_id:
                session["references"]["last_person_id"] = person_id
            recent = session["references"].get("recent_people", [])
            if person_name in recent:
                recent.remove(person_name)
            recent.append(person_name)
            session["references"]["recent_people"] = recent[-3:]

        # Phase 7B: Track speaker references and switching
        if speaker_info:
            new_speaker_id = speaker_info.get("speaker_id")
            prev_speaker_id = session["references"].get("current_speaker_id")
            if prev_speaker_id and new_speaker_id and prev_speaker_id != new_speaker_id:
                session["references"]["last_speaker_change"] = time.time()
                log.info(f"[SpeakerSwitch] Device {device_id}: {prev_speaker_id} -> {new_speaker_id}")
            
            session["references"]["current_speaker_id"] = new_speaker_id
            session["references"]["current_speaker_label"] = speaker_info.get("label", "Unknown Speaker")
            session["references"]["current_speaker"] = speaker_info
            
            # Maintain recent speakers list
            recent_spks = session["references"].get("recent_speakers", [])
            recent_spks = [s for s in recent_spks if s.get("speaker_id") != new_speaker_id]
            spk_summary = {
                "speaker_id": new_speaker_id,
                "label": speaker_info.get("label", "Unknown Speaker"),
                "is_primary_user": speaker_info.get("is_primary_user", False),
                "person_id": speaker_info.get("person_id"),
                "display_name": speaker_info.get("display_name"),
                "relationship_type": speaker_info.get("relationship_type"),
                "timestamp": time.time()
            }
            recent_spks.append(spk_summary)
            session["references"]["recent_speakers"] = recent_spks[-5:]
        
        await save_session_context(
            device_id,
            session["turns"],
            session["references"],
            session["active_task"]
        )

    @staticmethod
    def format_context_for_prompt(session: dict) -> str:
        """Format the session context into a compact string for the LLM."""
        lines = []
        if session["turns"]:
            lines.append("Recent Conversation History:")
            for idx, turn in enumerate(session["turns"][-5:]): # Only last 5 turns for prompt
                spk_label = turn.get("speaker_label") or "User"
                p_name = turn.get("person_name")
                rel = turn.get("relationship_type")
                if p_name and rel:
                    spk_prefix = f"{spk_label} ({p_name} - {rel})"
                elif p_name:
                    spk_prefix = f"{spk_label} ({p_name})"
                else:
                    spk_prefix = spk_label

                lines.append(f"{spk_prefix}: {turn['request']}")
                if "action" in turn:
                    lines.append(f"Cortex: [Executed {turn['action']} with args {turn.get('arguments', {})}] {turn.get('response_message', '')}")
                else:
                    lines.append(f"Cortex: {turn.get('response_message', '')}")
        
        if session["references"]:
            lines.append("\nActive References:")
            for k, v in session["references"].items():
                if k not in ("current_speaker", "recent_speakers"):
                    lines.append(f"- {k}: {v}")
            if "current_speaker_label" in session["references"]:
                lines.append(f"- Current Speaker: {session['references']['current_speaker_label']}")
            if "recent_speakers" in session["references"]:
                labels = [s.get("label") for s in session["references"]["recent_speakers"]]
                lines.append(f"- Recent Speakers: {', '.join(labels)}")
                
        if session.get("active_task"):
            lines.append(f"\nActive Task ID: {session['active_task']}")
            
        return "\n".join(lines)
