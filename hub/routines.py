"""
hub/routines.py
---------------
Phase 8F: Minimal Learned Routines.
Learns simple, deterministic recurring patterns from existing structured history:
- Google Calendar
- WorkItems
- Device state / usage history
- World State

No ML models, no health learning, no GPS learning, no anomaly detection.
Pure deterministic aggregation and bounded confidence [0, 1].
"""

import json
import logging
import uuid
import re
from datetime import datetime, timezone, timedelta, time as _time
from typing import Optional, Dict, Any, List, Tuple, Set
from dataclasses import dataclass, field, asdict

from .database import (
    save_learned_routine,
    get_learned_routine,
    list_learned_routines,
    delete_learned_routine,
)
from .world_state import WorldStateManager, Signal, RuntimeTimeAdapter, CalendarSignalAdapter

log = logging.getLogger("cortex.routines")

ROUTINE_CATEGORIES = [
    "class",
    "work",
    "study",
    "personal",
    "device_usage"
]

WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday"
]


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class LearnedRoutine:
    """A persistent, learned recurring schedule pattern."""
    routine_id: str
    label: str
    category: str  # "class", "work", "study", "personal", "device_usage"
    days_of_week: List[str]  # e.g. ["monday", "wednesday", "friday"]
    typical_start_time: str  # "HH:MM" 24h format
    typical_end_time: str    # "HH:MM" 24h format
    confidence: float        # Bounded [0.0, 1.0]
    observation_count: int   # >= 3
    source: str              # "calendar", "workload", "device"
    last_observed_at: str    # ISO-8601 string
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LearnedRoutine":
        days = d.get("days_of_week", [])
        if isinstance(days, str):
            try:
                days = json.loads(days)
            except Exception:
                days = [days]
        meta = d.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        return cls(
            routine_id=d.get("routine_id") or f"rtn_{uuid.uuid4().hex[:10]}",
            label=d.get("label", ""),
            category=d.get("category", "personal"),
            days_of_week=list(days),
            typical_start_time=d.get("typical_start_time", "09:00"),
            typical_end_time=d.get("typical_end_time", "10:00"),
            confidence=max(0.0, min(1.0, float(d.get("confidence", 0.8)))),
            observation_count=int(d.get("observation_count", 3)),
            source=d.get("source", "calendar"),
            last_observed_at=d.get("last_observed_at") or datetime.now(timezone.utc).isoformat(),
            metadata=dict(meta)
        )


# ---------------------------------------------------------------------------
# General Categorization & Confidence (No Hardcoded Courses or Employers)
# ---------------------------------------------------------------------------

def categorize_event(label: str) -> str:
    """
    Categorize an event label into one of the 5 standardized routine categories.
    Uses generic structural keywords and regex patterns; never hardcodes specific
    course names, universities, or employers.
    """
    clean = label.lower().strip()

    # 1. Class: Academic course code pattern (e.g., CS 101, PHYS 260, MATH 213, GGS 101)
    # or generic classroom terms
    course_code_regex = r'\b[a-z]{2,4}\s*\d{3}[a-z]?\b'
    if re.search(course_code_regex, clean):
        return "class"
    if re.search(r'\b(?:class|lecture|recitation|lab|seminar)\b', clean):
        return "class"

    # 2. Study: Academic prep, homework, reading, review
    if re.search(r'\b(?:study|homework|hw|review|reading|assignment|prep|library)\b', clean):
        return "study"

    # 3. Work: Professional shifts, office hours, workplace roles
    if re.search(r'\b(?:work|shift|office|desk|job|duty|meeting)\b', clean):
        return "work"

    # 4. Device Usage: Computing / active workstation session
    if re.search(r'\b(?:device|usage|laptop|workstation|screen)\b', clean):
        return "device_usage"

    # 5. Personal: Default category
    return "personal"


def compute_routine_confidence(observation_count: int, num_days: int = 1) -> float:
    """
    Deterministic, bounded confidence function [0.0, 1.0].
    Requires at least 3 observations. Bounded strictly without LLM involvement.
    """
    if observation_count < 3:
        return 0.0
    # Base confidence of 0.80 for 3 observations, +0.05 per extra observation up to 1.0
    raw = 0.80 + 0.05 * (observation_count - 3) + min(0.05, 0.02 * (num_days - 1))
    return round(max(0.0, min(1.0, raw)), 2)


# ---------------------------------------------------------------------------
# Routine Manager
# ---------------------------------------------------------------------------

class RoutineManager:
    """
    Singleton manager for persistent learned routines.
    Learns recurring patterns, syncs with World State, and answers natural language queries.
    """
    _instance: Optional["RoutineManager"] = None

    def __init__(self):
        self._routines: Dict[str, LearnedRoutine] = {}
        self._initialized: bool = False

    @classmethod
    def get_instance(cls) -> "RoutineManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset in-memory singleton state for testing."""
        if cls._instance is not None:
            cls._instance._routines.clear()
            cls._instance._initialized = False
        cls._instance = None

    async def ensure_initialized(self) -> None:
        if not self._initialized:
            await self.load_from_db()
            self._initialized = True

    async def load_from_db(self) -> None:
        """Load all persisted routines from SQLite."""
        rows = await list_learned_routines()
        self._routines.clear()
        for r in rows:
            routine = LearnedRoutine.from_dict(r)
            self._routines[routine.routine_id] = routine
        self._initialized = True
        log.info(f"[Routines] Loaded {len(self._routines)} learned routines from database.")

    async def save_routine(self, routine: LearnedRoutine) -> None:
        """Persist a routine to SQLite and update memory cache."""
        self._routines[routine.routine_id] = routine
        await save_learned_routine(routine.to_dict())

    async def delete_routine(self, routine_id: str) -> bool:
        """Delete a routine by ID."""
        self._routines.pop(routine_id, None)
        return await delete_learned_routine(routine_id)

    def get_routine(self, routine_id: str) -> Optional[LearnedRoutine]:
        return self._routines.get(routine_id)

    def list_routines(self, category: Optional[str] = None) -> List[LearnedRoutine]:
        routines = list(self._routines.values())
        if category:
            routines = [r for r in routines if r.category == category]
        routines.sort(key=lambda r: (r.typical_start_time, -r.confidence))
        return routines

    # -----------------------------------------------------------------------
    # Deterministic Learning Engine
    # -----------------------------------------------------------------------

    async def learn_from_calendar_events(self, events: List[Dict[str, Any]]) -> List[LearnedRoutine]:
        """
        Cluster repeated calendar observations into recurring candidate routines.
        Requires >= 3 observations of same/similar event window.
        """
        await self.ensure_initialized()
        if not events:
            return []

        # 1. Parse and extract atomic observations
        parsed_obs: List[Dict[str, Any]] = []
        for ev in events:
            title = ev.get("summary") or ev.get("title") or ""
            title = title.strip()
            if not title or title.lower() in ("(no title)", "busy", "untitled"):
                continue

            start_raw = ev.get("start")
            end_raw = ev.get("end")
            try:
                start_dt = CalendarSignalAdapter._parse_event_dt(start_raw)
                end_dt = CalendarSignalAdapter._parse_event_dt(end_raw)
            except Exception:
                continue

            if not start_dt or not end_dt:
                continue

            local_tz = RuntimeTimeAdapter.get_local_now().tzinfo
            start_local = start_dt.astimezone(local_tz)
            end_local = end_dt.astimezone(local_tz)

            weekday = start_local.strftime("%A").lower()
            start_time_str = start_local.strftime("%H:%M")
            end_time_str = end_local.strftime("%H:%M")
            start_mins = start_local.hour * 60 + start_local.minute
            end_mins = end_local.hour * 60 + end_local.minute

            clean_label = re.sub(r'[,;:\-—!?.]+', ' ', title).strip()
            clean_label = re.sub(r'\s+', ' ', clean_label)

            parsed_obs.append({
                "raw_title": title,
                "clean_label": clean_label,
                "normalized_key": clean_label.lower(),
                "weekday": weekday,
                "start_mins": start_mins,
                "end_mins": end_mins,
                "start_time_str": start_time_str,
                "end_time_str": end_time_str,
                "dt_iso": start_local.isoformat()
            })

        # 2. Cluster observations by normalized label and approximate start time (+/- 45 mins)
        clusters: List[Dict[str, Any]] = []
        for obs in parsed_obs:
            matched_cluster = None
            for c in clusters:
                if c["normalized_key"] == obs["normalized_key"]:
                    # Check start time similarity within 45 minutes
                    if abs(c["avg_start_mins"] - obs["start_mins"]) <= 45:
                        matched_cluster = c
                        break

            if matched_cluster:
                matched_cluster["observations"].append(obs)
                matched_cluster["days"].add(obs["weekday"])
                matched_cluster["start_mins_list"].append(obs["start_mins"])
                matched_cluster["end_mins_list"].append(obs["end_mins"])
                matched_cluster["avg_start_mins"] = int(sum(matched_cluster["start_mins_list"]) / len(matched_cluster["start_mins_list"]))
                if obs["dt_iso"] > matched_cluster["last_observed_at"]:
                    matched_cluster["last_observed_at"] = obs["dt_iso"]
            else:
                clusters.append({
                    "raw_title": obs["raw_title"],
                    "clean_label": obs["clean_label"],
                    "normalized_key": obs["normalized_key"],
                    "observations": [obs],
                    "days": {obs["weekday"]},
                    "start_mins_list": [obs["start_mins"]],
                    "end_mins_list": [obs["end_mins"]],
                    "avg_start_mins": obs["start_mins"],
                    "last_observed_at": obs["dt_iso"]
                })

        # 3. Filter clusters with >= 3 observations and construct LearnedRoutine
        learned: List[LearnedRoutine] = []
        for c in clusters:
            count = len(c["observations"])
            if count < 3:
                continue

            # Compute median or average start & end times
            avg_start = int(sum(c["start_mins_list"]) / count)
            avg_end = int(sum(c["end_mins_list"]) / count)

            start_hh = avg_start // 60
            start_mm = avg_start % 60
            end_hh = avg_end // 60
            end_mm = avg_end % 60

            typical_start = f"{start_hh:02d}:{start_mm:02d}"
            typical_end = f"{end_hh:02d}:{end_mm:02d}"

            # Sort days of week in calendar order (monday -> sunday)
            sorted_days = [d for d in WEEKDAYS if d in c["days"]]

            category = categorize_event(c["raw_title"])
            confidence = compute_routine_confidence(count, len(sorted_days))

            # Deterministic routine ID based on label + typical start
            label_slug = re.sub(r'[^a-z0-9]+', '_', c["normalized_key"]).strip('_')[:20]
            routine_id = f"rtn_{label_slug}_{start_hh:02d}{start_mm:02d}"

            routine = LearnedRoutine(
                routine_id=routine_id,
                label=c["clean_label"],
                category=category,
                days_of_week=sorted_days,
                typical_start_time=typical_start,
                typical_end_time=typical_end,
                confidence=confidence,
                observation_count=count,
                source="calendar",
                last_observed_at=c["last_observed_at"],
                metadata={"cluster_size": count}
            )

            await self.save_routine(routine)
            learned.append(routine)
            log.info(f"[Routines] Learned routine '{routine.label}' ({category}): {sorted_days} {typical_start}-{typical_end} (count={count}, conf={confidence})")

        return learned

    async def learn_from_history(
        self,
        calendar_events: Optional[List[Dict[str, Any]]] = None,
        device_sessions: Optional[List[Dict[str, Any]]] = None
    ) -> List[LearnedRoutine]:
        """Unified entry point to learn routines from multiple structured sources."""
        results: List[LearnedRoutine] = []
        if calendar_events:
            cal_results = await self.learn_from_calendar_events(calendar_events)
            results.extend(cal_results)

        if device_sessions:
            # Cluster repeated device sessions (device_id, weekday, start_time)
            dev_results = await self._learn_from_device_sessions(device_sessions)
            results.extend(dev_results)

        # Update World State signals with latest learned routines
        await self.update_world_state_signals()
        return results

    async def _learn_from_device_sessions(self, sessions: List[Dict[str, Any]]) -> List[LearnedRoutine]:
        """Learn recurring device usage routines (category='device_usage')."""
        dev_clusters: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = {}
        for s in sessions:
            dev_id = s.get("device_id")
            if not dev_id:
                continue
            weekday = s.get("weekday", "").lower()
            start_str = s.get("start_time", "09:00")
            try:
                parts = start_str.split(":")
                hour_bucket = int(parts[0])
            except Exception:
                hour_bucket = 9

            if weekday in WEEKDAYS:
                key = (dev_id, weekday, hour_bucket)
                dev_clusters.setdefault(key, []).append(s)

        learned = []
        for (dev_id, weekday, hour_bucket), obs_list in dev_clusters.items():
            count = len(obs_list)
            if count >= 3:
                typical_start = f"{hour_bucket:02d}:00"
                typical_end = f"{(hour_bucket + 2) % 24:02d}:00"
                routine_id = f"rtn_dev_{dev_id}_{weekday}_{hour_bucket:02d}"
                confidence = compute_routine_confidence(count, 1)

                routine = LearnedRoutine(
                    routine_id=routine_id,
                    label=f"Active on {dev_id.upper()}",
                    category="device_usage",
                    days_of_week=[weekday],
                    typical_start_time=typical_start,
                    typical_end_time=typical_end,
                    confidence=confidence,
                    observation_count=count,
                    source="device",
                    last_observed_at=datetime.now(timezone.utc).isoformat(),
                    metadata={"target_device": dev_id}
                )
                await self.save_routine(routine)
                learned.append(routine)

        return learned

    # -----------------------------------------------------------------------
    # World State Integration
    # -----------------------------------------------------------------------

    def get_expected_routines(self, target_dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Evaluate expected routines for a given local datetime (defaults to now).
        Returns dict with current_expected, next_expected, and confidence.
        """
        if target_dt is None:
            target_dt = RuntimeTimeAdapter.get_local_now()

        weekday = target_dt.strftime("%A").lower()
        now_time_str = target_dt.strftime("%H:%M")

        today_routines = [r for r in self._routines.values() if weekday in r.days_of_week]
        today_routines.sort(key=lambda r: r.typical_start_time)

        current_expected = None
        next_expected = None
        max_conf = 0.0

        for r in today_routines:
            if r.typical_start_time <= now_time_str <= r.typical_end_time:
                current_expected = {
                    "routine_id": r.routine_id,
                    "label": r.label,
                    "category": r.category,
                    "start": r.typical_start_time,
                    "end": r.typical_end_time,
                    "confidence": r.confidence
                }
                max_conf = max(max_conf, r.confidence)
                break

        for r in today_routines:
            if r.typical_start_time > now_time_str:
                next_expected = {
                    "routine_id": r.routine_id,
                    "label": r.label,
                    "category": r.category,
                    "start": r.typical_start_time,
                    "end": r.typical_end_time,
                    "confidence": r.confidence
                }
                if not current_expected:
                    max_conf = max(max_conf, r.confidence)
                break

        return {
            "current_expected": current_expected,
            "next_expected": next_expected,
            "confidence": max_conf
        }

    async def update_world_state_signals(self, target_dt: Optional[datetime] = None) -> None:
        """Ingest routine.current_expected, routine.next_expected, routine.confidence into World State."""
        exp = self.get_expected_routines(target_dt)
        wsm = WorldStateManager.get_instance()
        now_iso = datetime.now(timezone.utc).isoformat()

        # Emit routine signals
        sig_curr = Signal(
            key="routine.current_expected",
            source="learned_routines",
            timestamp=now_iso,
            confidence=exp["confidence"] or 0.8,
            ttl=1800.0,
            payload={"value": exp["current_expected"]}
        )
        sig_next = Signal(
            key="routine.next_expected",
            source="learned_routines",
            timestamp=now_iso,
            confidence=exp["confidence"] or 0.8,
            ttl=1800.0,
            payload={"value": exp["next_expected"]}
        )
        sig_conf = Signal(
            key="routine.confidence",
            source="learned_routines",
            timestamp=now_iso,
            confidence=1.0,
            ttl=1800.0,
            payload={"value": exp["confidence"]}
        )

        await wsm.ingest_signal(sig_curr)
        await wsm.ingest_signal(sig_next)
        await wsm.ingest_signal(sig_conf)

    # -----------------------------------------------------------------------
    # Natural Language Queries
    # -----------------------------------------------------------------------

    async def handle_natural_language(self, text: str, context: Optional[dict] = None) -> Optional[str]:
        """
        Deterministically recognize and answer learned routine queries:
        1. "What does my normal Monday look like?"
        2. "When do I usually work?"
        3. "When am I normally free?"
        4. "What patterns have you learned about me?"
        """
        await self.ensure_initialized()
        clean = text.lower().strip(" \t.,!?;:")

        # 1. "What does my normal Monday look like?" / "What is my typical Monday?"
        m_day = re.search(
            r'\b(?:what(?:\s+does|\'s|\s+is)?\s+(?:my\s+)?(?:normal|typical|usual)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\'s)?(?:\s+schedule)?(?:\s+look\s+like)?|how\s+does\s+my\s+(?:normal|typical|usual)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+look)\b',
            clean
        )
        if m_day:
            day_name = (m_day.group(1) or m_day.group(2)).lower()
            return self._format_normal_day_schedule(day_name)

        # 2. "When do I usually work?" / "When do I normally work?"
        if re.search(r'\bwhen\s+do\s+i\s+(?:usually|normally|typically)\s+work\b', clean):
            return self._format_usually_work()

        # 3. "When am I normally free?" / "When am I usually free?"
        m_free = re.search(
            r'\bwhen\s+am\s+i\s+(?:normally|usually|typically)\s+free(?:\s+on\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday))?\b',
            clean
        )
        if m_free:
            specific_day = m_free.group(1).lower() if m_free.group(1) else None
            return self._format_normally_free(specific_day)

        # 4. "What patterns have you learned about me?" / "What routines have you learned?"
        if re.search(
            r'\bwhat\s+(?:patterns|routines)\s+(?:have\s+you\s+learned|do\s+you\s+know)(?:\s+about\s+me|\s+about\s+my\s+schedule)?\b|\bwhat\s+have\s+you\s+learned\s+about\s+my\s+(?:routine|schedule|patterns)\b',
            clean
        ):
            return self._format_all_learned_patterns()

        return None

    def _format_normal_day_schedule(self, day_name: str) -> str:
        """Format the routine schedule for a given weekday."""
        matching = [r for r in self._routines.values() if day_name in r.days_of_week]
        if not matching:
            return f"I haven't learned any recurring routines for {day_name.capitalize()}s yet."

        matching.sort(key=lambda r: r.typical_start_time)
        lines = [f"Based on your learned routines, your normal {day_name.capitalize()} looks like:"]
        for r in matching:
            cat_display = r.category.capitalize()
            lines.append(f"  * {r.typical_start_time} - {r.typical_end_time}: {r.label} ({cat_display})")

        # Mention free intervals
        free_intervals = self._compute_free_windows_for_day(matching)
        if free_intervals:
            free_str = ", ".join(free_intervals)
            lines.append(f"You are typically free: {free_str}.")

        return "\n".join(lines)

    def _format_usually_work(self) -> str:
        """Format when the user usually works."""
        work_routines = [r for r in self._routines.values() if r.category == "work" or "work" in r.label.lower()]
        if not work_routines:
            return "I haven't learned any recurring work routines yet."

        work_routines.sort(key=lambda r: (r.typical_start_time, -r.confidence))
        lines = ["Based on your learned routines, you usually work:"]
        for r in work_routines:
            days_fmt = ", ".join([d.capitalize() for d in r.days_of_week])
            pct = int(r.confidence * 100)
            lines.append(f"  * {days_fmt} from {r.typical_start_time} to {r.typical_end_time} ({r.label}, observed {r.observation_count} times, {pct}% confidence)")

        return "\n".join(lines)

    def _format_normally_free(self, specific_day: Optional[str] = None) -> str:
        """Format when the user is normally free."""
        if specific_day:
            matching = [r for r in self._routines.values() if specific_day in r.days_of_week]
            matching.sort(key=lambda r: r.typical_start_time)
            windows = self._compute_free_windows_for_day(matching)
            if not windows:
                return f"On {specific_day.capitalize()}s, you have routines throughout the day with no major recurring gaps."
            return f"On {specific_day.capitalize()}s, you are normally free: {', '.join(windows)}."

        # Overall summary across the week
        if not self._routines:
            return "I haven't learned enough recurring routines yet to determine your normal free time."

        lines = ["Based on your learned routines, you are normally free:"]
        for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
            matching = [r for r in self._routines.values() if day in r.days_of_week]
            matching.sort(key=lambda r: r.typical_start_time)
            windows = self._compute_free_windows_for_day(matching)
            if windows:
                lines.append(f"  * {day.capitalize()}: {', '.join(windows)}")
            else:
                lines.append(f"  * {day.capitalize()}: Most of the day (no recurring commitments learned)")

        return "\n".join(lines)

    def _compute_free_windows_for_day(self, sorted_routines: List[LearnedRoutine]) -> List[str]:
        """Compute free intervals between routines on a day."""
        if not sorted_routines:
            return ["all day"]

        windows = []
        first_start = sorted_routines[0].typical_start_time
        if first_start > "09:00":
            windows.append(f"mornings before {first_start}")

        for i in range(len(sorted_routines) - 1):
            curr_end = sorted_routines[i].typical_end_time
            next_start = sorted_routines[i + 1].typical_start_time
            if next_start > curr_end:
                windows.append(f"between {curr_end} and {next_start}")

        last_end = sorted_routines[-1].typical_end_time
        if last_end < "21:00":
            windows.append(f"evenings after {last_end}")

        return windows

    def _format_all_learned_patterns(self) -> str:
        """Format all persisted learned routines."""
        routines = self.list_routines()
        if not routines:
            return "I haven't learned any recurring patterns yet. Once events or habits are observed at least 3 times, I will establish learned routines."

        lines = [f"I have learned {len(routines)} recurring {'routine' if len(routines) == 1 else 'routines'}:"]
        for idx, r in enumerate(routines, 1):
            days_fmt = ", ".join([d.capitalize() for d in r.days_of_week])
            cat_display = r.category.capitalize()
            pct = int(r.confidence * 100)
            lines.append(
                f"  {idx}. {r.label} ({cat_display}): {days_fmt}, {r.typical_start_time} - {r.typical_end_time} "
                f"(observed {r.observation_count} times, {pct}% confidence)"
            )

        return "\n".join(lines)
