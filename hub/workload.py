"""
hub/workload.py
---------------
Phase 8E: Academic + Workload Context Foundation.
Maintains structured tracking of user obligations:
- assignments, exams, quizzes, projects, readings, work shifts, personal tasks
- deadlines, completion states, remaining effort, and pressure metrics
- integration with World State, Google Calendar, ProactiveEngine, and AttentionRouter
"""

import json
import logging
import uuid
import re
from datetime import datetime, timezone, timedelta, date as _date
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, asdict

from .database import (
    save_work_item,
    get_work_item,
    list_work_items,
    update_work_item,
    delete_work_item,
)
from .world_state import WorldStateManager, Signal, RuntimeTimeAdapter, CalendarSignalAdapter

log = logging.getLogger("cortex.workload")

WORK_ITEM_TYPES = [
    "assignment",
    "exam",
    "quiz",
    "project",
    "reading",
    "work_shift",
    "personal_task"
]

WORK_ITEM_STATUSES = [
    "pending",
    "in_progress",
    "completed",
    "overdue",
    "blocked"
]


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class WorkItem:
    """A persistent academic, occupational, or personal obligation."""
    work_item_id: str
    title: str
    type: str = "assignment"
    course_or_project: str = ""
    source: str = "user"
    due_at: str = ""  # ISO-8601 string
    estimated_minutes: int = 0
    remaining_minutes: int = 0
    priority: int = 3  # 1 (low) to 5 (critical)
    status: str = "pending"  # pending, in_progress, completed, overdue, blocked
    confidence: float = 1.0
    notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    related_task_id: str = ""
    related_calendar_event_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def get_due_datetime(self) -> Optional[datetime]:
        """Return timezone-aware datetime for due_at, or None."""
        if not self.due_at:
            return None
        try:
            return CalendarSignalAdapter._parse_event_dt(self.due_at)
        except Exception:
            return None

    def minutes_until_due(self, now: Optional[datetime] = None) -> Optional[float]:
        """Minutes until due_at. Negative means overdue."""
        due_dt = self.get_due_datetime()
        if not due_dt:
            return None
        if now is None:
            now = CalendarSignalAdapter._get_local_now()
        due_local = due_dt.astimezone(now.tzinfo)
        diff = (due_local - now).total_seconds() / 60.0
        return diff

    def is_overdue(self, now: Optional[datetime] = None) -> bool:
        mins = self.minutes_until_due(now)
        if mins is None:
            return False
        return mins < 0 and self.status not in ("completed", "blocked")


# ---------------------------------------------------------------------------
# Workload Signal Adapter
# ---------------------------------------------------------------------------

class WorkloadSignalAdapter:
    """Adapter to convert workload calculations into structured World State signals."""

    @staticmethod
    def create_workload_signals(metrics: Dict[str, Any], ttl: float = 300.0) -> List[Signal]:
        now_iso = datetime.now(timezone.utc).isoformat()
        signals = [
            Signal(
                key="workload.total_pending",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={"count": metrics.get("total_pending", 0), "value": metrics.get("total_pending", 0)}
            ),
            Signal(
                key="workload.total_overdue",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={"count": metrics.get("total_overdue", 0), "value": metrics.get("total_overdue", 0)}
            ),
            Signal(
                key="workload.due_today",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={
                    "count": len(metrics.get("due_today", [])),
                    "value": len(metrics.get("due_today", [])),
                    "items": [i.to_dict() if hasattr(i, "to_dict") else i for i in metrics.get("due_today", [])]
                }
            ),
            Signal(
                key="workload.due_tomorrow",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={
                    "count": len(metrics.get("due_tomorrow", [])),
                    "value": len(metrics.get("due_tomorrow", [])),
                    "items": [i.to_dict() if hasattr(i, "to_dict") else i for i in metrics.get("due_tomorrow", [])]
                }
            ),
            Signal(
                key="workload.minutes_due_24h",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={"minutes": metrics.get("minutes_due_24h", 0), "value": metrics.get("minutes_due_24h", 0)}
            ),
            Signal(
                key="workload.minutes_due_72h",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={"minutes": metrics.get("minutes_due_72h", 0), "value": metrics.get("minutes_due_72h", 0)}
            ),
            Signal(
                key="workload.free_time_before_next_commitment",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={"minutes": metrics.get("free_time_before_next_commitment", 0.0), "value": metrics.get("free_time_before_next_commitment", 0.0)}
            ),
            Signal(
                key="workload.current_pressure",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={"pressure": metrics.get("current_pressure", 0.0), "value": metrics.get("current_pressure", 0.0)}
            )
        ]

        next_dl = metrics.get("next_deadline")
        if next_dl:
            signals.append(Signal(
                key="workload.next_deadline",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={
                    "item_id": next_dl.get("work_item_id"),
                    "title": next_dl.get("title"),
                    "due_at": next_dl.get("due_at"),
                    "minutes_until": next_dl.get("minutes_until"),
                    "value": next_dl.get("minutes_until")
                }
            ))
        else:
            signals.append(Signal(
                key="workload.next_deadline",
                source="workload_manager",
                timestamp=now_iso,
                ttl=ttl,
                payload={"value": None, "minutes_until": None}
            ))

        return signals


# ---------------------------------------------------------------------------
# Workload Manager (Singleton)
# ---------------------------------------------------------------------------

class WorkloadManager:
    """Central manager for academic obligations and workload state."""
    _instance: Optional["WorkloadManager"] = None

    def __init__(self):
        self._items: Dict[str, WorkItem] = {}
        self._initialized: bool = False

    @classmethod
    def get_instance(cls) -> "WorkloadManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        if cls._instance is not None:
            cls._instance._items.clear()
            cls._instance._initialized = False
        cls._instance = None

    async def ensure_initialized(self) -> None:
        if not self._initialized:
            await self.load_from_db()
            self._initialized = True

    async def load_from_db(self) -> None:
        """Load all work items from SQLite into memory."""
        try:
            records = await list_work_items(include_completed=True)
            self._items.clear()
            for r in records:
                item = WorkItem(
                    work_item_id=r["work_item_id"],
                    title=r["title"],
                    type=r.get("type", "assignment"),
                    course_or_project=r.get("course_or_project", ""),
                    source=r.get("source", "user"),
                    due_at=r.get("due_at", ""),
                    estimated_minutes=int(r.get("estimated_minutes", 0)),
                    remaining_minutes=int(r.get("remaining_minutes", 0)),
                    priority=int(r.get("priority", 3)),
                    status=r.get("status", "pending"),
                    confidence=float(r.get("confidence", 1.0)),
                    notes=r.get("notes", ""),
                    created_at=r.get("created_at", datetime.now(timezone.utc).isoformat()),
                    updated_at=r.get("updated_at", datetime.now(timezone.utc).isoformat()),
                    related_task_id=r.get("related_task_id", ""),
                    related_calendar_event_id=r.get("related_calendar_event_id", "")
                )
                self._items[item.work_item_id] = item
        except Exception as e:
            log.warning(f"[WorkloadManager] Failed to load work items from DB: {e}")

    async def add_item(
        self,
        title: str,
        type: str = "assignment",
        course_or_project: str = "",
        due_at: str = "",
        estimated_minutes: int = 0,
        remaining_minutes: Optional[int] = None,
        priority: int = 3,
        status: str = "pending",
        source: str = "natural_language",
        notes: str = "",
        confidence: float = 1.0,
        related_task_id: str = "",
        related_calendar_event_id: str = ""
    ) -> WorkItem:
        """Add or update an obligation with duplicate prevention."""
        await self.ensure_initialized()

        if remaining_minutes is None:
            remaining_minutes = estimated_minutes

        # Duplicate check: same course/title and close due date
        existing = self.find_duplicate(title, course_or_project, due_at)
        if existing:
            updates = {}
            if estimated_minutes > 0:
                updates["estimated_minutes"] = estimated_minutes
            if remaining_minutes > 0:
                updates["remaining_minutes"] = remaining_minutes
            if due_at:
                updates["due_at"] = due_at
            if notes:
                updates["notes"] = notes
            return await self.update_item(existing.work_item_id, updates)

        item_id = f"wi_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        item = WorkItem(
            work_item_id=item_id,
            title=title,
            type=type,
            course_or_project=course_or_project,
            source=source,
            due_at=due_at,
            estimated_minutes=estimated_minutes,
            remaining_minutes=remaining_minutes,
            priority=priority,
            status=status,
            confidence=confidence,
            notes=notes,
            created_at=now_iso,
            updated_at=now_iso,
            related_task_id=related_task_id,
            related_calendar_event_id=related_calendar_event_id
        )

        self._items[item_id] = item
        try:
            await save_work_item(item.to_dict())
        except Exception as e:
            log.error(f"[WorkloadManager] Error saving work item to DB: {e}")

        await self.emit_workload_signals()
        await self.evaluate_proactive_triggers()
        return item

    def find_duplicate(self, title: str, course: str, due_at: str) -> Optional[WorkItem]:
        t_clean = title.lower().strip()
        c_clean = course.lower().strip()
        for item in self._items.values():
            if item.status == "completed":
                continue
            item_t = item.title.lower().strip()
            item_c = item.course_or_project.lower().strip()
            if (t_clean == item_t) or (c_clean and c_clean == item_c and item.type in t_clean):
                return item
        return None

    async def get_item(self, work_item_id: str) -> Optional[WorkItem]:
        await self.ensure_initialized()
        item = self._items.get(work_item_id)
        if not item:
            rec = await get_work_item(work_item_id)
            if rec:
                item = WorkItem(**rec)
                self._items[work_item_id] = item
        return item

    async def update_item(self, work_item_id: str, updates: Dict[str, Any]) -> Optional[WorkItem]:
        await self.ensure_initialized()
        item = self._items.get(work_item_id)
        if not item:
            item = await self.get_item(work_item_id)
        if not item:
            return None

        for k, v in updates.items():
            if hasattr(item, k):
                setattr(item, k, v)
        item.updated_at = datetime.now(timezone.utc).isoformat()

        try:
            await update_work_item(work_item_id, updates)
        except Exception as e:
            log.error(f"[WorkloadManager] Error updating work item in DB: {e}")

        await self.emit_workload_signals()
        await self.evaluate_proactive_triggers()
        return item

    async def complete_item(self, identifier: str) -> Optional[WorkItem]:
        """Mark an item as completed by ID, title, or course."""
        await self.ensure_initialized()
        item = self.find_item(identifier)
        if not item:
            return None
        return await self.update_item(item.work_item_id, {"status": "completed", "remaining_minutes": 0})

    async def list_items(self, status: Optional[str] = None, include_completed: bool = False) -> List[WorkItem]:
        await self.ensure_initialized()
        items = list(self._items.values())
        if status:
            return [i for i in items if i.status == status]
        if not include_completed:
            return [i for i in items if i.status != "completed"]
        return items

    def find_item(self, query: str, active_only: bool = True) -> Optional[WorkItem]:
        """Fuzzy/substring search for an item by title or course."""
        q = query.lower().strip()
        candidates = [i for i in self._items.values() if not (active_only and i.status == "completed")]

        # 1. Exact match on ID
        for i in candidates:
            if i.work_item_id == query:
                return i

        # 2. Exact match on title or course
        for i in candidates:
            if i.title.lower() == q or (i.course_or_project and i.course_or_project.lower() == q):
                return i

        # 3. Substring match
        for i in candidates:
            if q in i.title.lower() or (i.course_or_project and q in i.course_or_project.lower()):
                return i
            if any(w in i.title.lower() for w in q.split() if len(w) > 3):
                return i

        return candidates[0] if candidates and len(candidates) == 1 else None

    # -----------------------------------------------------------------------
    # Workload Metrics & Calculations
    # -----------------------------------------------------------------------

    async def compute_workload_metrics(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Calculate structured metrics across all work items and calendar context."""
        await self.ensure_initialized()
        if now is None:
            now = CalendarSignalAdapter._get_local_now()
        local_tz = now.tzinfo

        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        tomorrow_start = today_end
        tomorrow_end = tomorrow_start + timedelta(days=1)
        in_24h = now + timedelta(hours=24)
        in_72h = now + timedelta(hours=72)

        pending_items: List[WorkItem] = []
        overdue_items: List[WorkItem] = []
        due_today: List[WorkItem] = []
        due_tomorrow: List[WorkItem] = []
        active_items_with_dl: List[Tuple[WorkItem, datetime, float]] = []

        minutes_due_24h = 0
        minutes_due_72h = 0

        for item in self._items.values():
            if item.status == "completed":
                continue

            due_dt = item.get_due_datetime()
            is_ov = False

            if due_dt:
                due_local = due_dt.astimezone(local_tz)
                mins_left = (due_local - now).total_seconds() / 60.0

                if mins_left < 0:
                    is_ov = True
                    item.status = "overdue"
                    overdue_items.append(item)
                else:
                    active_items_with_dl.append((item, due_local, mins_left))

                if not is_ov:
                    if today_start <= due_local < today_end:
                        due_today.append(item)
                    elif tomorrow_start <= due_local < tomorrow_end:
                        due_tomorrow.append(item)

                    if now <= due_local <= in_24h:
                        minutes_due_24h += item.remaining_minutes
                    if now <= due_local <= in_72h:
                        minutes_due_72h += item.remaining_minutes
            else:
                pending_items.append(item)

            if not is_ov:
                pending_items.append(item)

        # Determine next deadline
        next_deadline_dict = None
        if active_items_with_dl:
            active_items_with_dl.sort(key=lambda x: x[1])
            best_item, best_dt, best_mins = active_items_with_dl[0]
            next_deadline_dict = {
                "work_item_id": best_item.work_item_id,
                "title": best_item.title,
                "due_at": best_item.due_at,
                "minutes_until": max(0, int(best_mins))
            }
        elif overdue_items:
            ov = overdue_items[0]
            next_deadline_dict = {
                "work_item_id": ov.work_item_id,
                "title": ov.title,
                "due_at": ov.due_at,
                "minutes_until": 0
            }

        # Available free time before next commitment via Calendar
        free_time_mins = await self._calculate_free_time_before_commitment(now)

        metrics = {
            "total_pending": len(pending_items),
            "total_overdue": len(overdue_items),
            "due_today": due_today,
            "due_tomorrow": due_tomorrow,
            "next_deadline": next_deadline_dict,
            "minutes_due_24h": minutes_due_24h,
            "minutes_due_72h": minutes_due_72h,
            "free_time_before_next_commitment": free_time_mins,
            "current_pressure": 0.0
        }

        metrics["current_pressure"] = self.calculate_pressure(metrics)
        return metrics

    async def _calculate_free_time_before_commitment(self, now: datetime) -> float:
        """Calculate free minutes before the next calendar event, class, or work shift."""
        try:
            wsm = WorldStateManager.get_instance()
            fused = await wsm.get_fused_world_state()
            cal = fused.calendar_context or {}

            cur = cal.get("current_event")
            nxt = cal.get("next_event")

            if nxt and nxt.get("start"):
                nxt_start_dt = CalendarSignalAdapter._parse_event_dt(nxt["start"])
                if nxt_start_dt:
                    diff_mins = (nxt_start_dt.astimezone(now.tzinfo) - now).total_seconds() / 60.0
                    return max(0.0, round(diff_mins, 1))

            time_until_secs = cal.get("time_until_seconds")
            if time_until_secs is not None and time_until_secs > 0:
                return round(time_until_secs / 60.0, 1)

            return 240.0
        except Exception:
            return 240.0

    @staticmethod
    def calculate_pressure(metrics: Dict[str, Any]) -> float:
        """
        Deterministic, bounded [0.0, 1.0] workload pressure score.
        Inputs:
        - overdue count
        - deadline proximity
        - workload volume in 24h vs available free time
        - workload volume in 72h
        - concurrent deadlines today & tomorrow
        """
        pressure = 0.0

        # 1. Overdue penalty (up to 0.30)
        overdue_cnt = metrics.get("total_overdue", 0)
        pressure += min(0.30, overdue_cnt * 0.15)

        # 2. Next deadline proximity (up to 0.35)
        next_dl = metrics.get("next_deadline")
        if next_dl:
            mins = next_dl.get("minutes_until", 999999)
            if mins is not None:
                if mins <= 0:
                    pressure += 0.35
                elif mins <= 120:  # < 2 hours
                    pressure += 0.30
                elif mins <= 1440:  # < 24 hours
                    pressure += 0.20
                elif mins <= 4320:  # < 72 hours
                    pressure += 0.10

        # 3. Workload volume in 24h vs free time (up to 0.25)
        mins_24h = metrics.get("minutes_due_24h", 0)
        free_mins = max(30.0, float(metrics.get("free_time_before_next_commitment", 240.0)))
        if mins_24h > 0:
            ratio = mins_24h / free_mins
            pressure += min(0.25, ratio * 0.15)

        # 4. Workload volume in 72h (up to 0.10)
        mins_72h = metrics.get("minutes_due_72h", 0)
        if mins_72h > 0:
            pressure += min(0.10, (mins_72h / 720.0) * 0.10)

        # 5. Concurrent deadlines today + tomorrow (up to 0.10)
        concur = len(metrics.get("due_today", [])) + len(metrics.get("due_tomorrow", []))
        if concur > 1:
            pressure += min(0.10, (concur - 1) * 0.05)

        return round(max(0.0, min(1.0, pressure)), 2)

    # -----------------------------------------------------------------------
    # Deterministic Prioritization
    # -----------------------------------------------------------------------

    def prioritize(self, items: Optional[List[WorkItem]] = None, now: Optional[datetime] = None) -> List[Tuple[WorkItem, float, str]]:
        """
        Rank items using deterministic multi-factor scoring:
        - Deadline urgency
        - Importance/priority
        - Task type weighting
        - Remaining effort
        - Status continuity
        Returns list of (WorkItem, score, rationale) sorted descending by score.
        """
        if items is None:
            items = [i for i in self._items.values() if i.status not in ("completed", "blocked")]
        if now is None:
            now = CalendarSignalAdapter._get_local_now()

        scored: List[Tuple[WorkItem, float, str]] = []
        for item in items:
            score = 0.0
            reasons = []

            if item.status == "blocked":
                scored.append((item, -100.0, "Blocked"))
                continue
            if item.status == "completed":
                scored.append((item, -200.0, "Completed"))
                continue

            mins = item.minutes_until_due(now)
            if mins is not None:
                if mins < 0:
                    score += 150.0
                    reasons.append("Overdue")
                elif mins <= 120:
                    score += 100.0
                    reasons.append("Due in <2 hours")
                elif mins <= 1440:
                    score += 70.0
                    reasons.append("Due in <24 hours")
                elif mins <= 2880:
                    score += 50.0
                    reasons.append("Due in <48 hours")
                elif mins <= 4320:
                    score += 30.0
                    reasons.append("Due in <3 days")
                else:
                    score += 10.0
                    reasons.append("Future deadline")

            score += item.priority * 5.0

            type_weights = {
                "exam": 20.0,
                "quiz": 15.0,
                "project": 12.0,
                "assignment": 10.0,
                "work_shift": 8.0,
                "reading": 5.0,
                "personal_task": 5.0
            }
            score += type_weights.get(item.type, 5.0)

            if mins is not None and mins <= 1440 and item.remaining_minutes >= 120:
                score += 15.0
                reasons.append("High effort due soon")

            if item.status == "in_progress":
                score += 5.0
                reasons.append("In progress")

            rationale = ", ".join(reasons) or "Standard priority"
            scored.append((item, round(score, 1), rationale))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    # -----------------------------------------------------------------------
    # World State & Proactive Integration
    # -----------------------------------------------------------------------

    async def emit_workload_signals(self) -> None:
        """Publish updated workload signals to WorldStateManager."""
        metrics = await self.compute_workload_metrics()
        signals = WorkloadSignalAdapter.create_workload_signals(metrics)
        wsm = WorldStateManager.get_instance()
        for s in signals:
            await wsm.ingest_signal(s)

    async def evaluate_proactive_triggers(self) -> None:
        """
        Evaluate workload conditions against ProactiveEngine and route to AttentionRouter.
        Supports:
        - deadline < 15 minutes
        - deadline < 2 hours
        - item becomes overdue
        - exam within 24 hours
        - workload.current_pressure > threshold
        """
        try:
            from .proactive_engine import ProactiveEngine
            from .attention_router import AttentionRouter

            pe = ProactiveEngine.get_instance()
            await self._ensure_proactive_conditions(pe)

            events = await pe.evaluate(force=True)
            if events:
                router = AttentionRouter.get_instance()
                for ev in events:
                    await router.route_event(ev.to_dict())
        except Exception as e:
            log.debug(f"[WorkloadManager] Proactive evaluation note: {e}")

    async def _ensure_proactive_conditions(self, pe) -> None:
        """Register default workload proactive conditions if not already registered."""
        existing = await pe.list_conditions(active_only=False)
        sig_keys = {c.signal_key for c in existing}

        if "workload.next_deadline" not in sig_keys:
            await pe.register_condition(
                condition_id="cond_workload_dl_15m",
                condition_type="signal_threshold",
                signal_key="workload.next_deadline",
                operator="less_than_or_equal",
                target_value=15,
                description="Upcoming task deadline in less than 15 minutes",
                metadata={"priority": 0.9}
            )

        if "workload.total_overdue" not in sig_keys:
            await pe.register_condition(
                condition_id="cond_workload_overdue",
                condition_type="signal_threshold",
                signal_key="workload.total_overdue",
                operator="greater_than",
                target_value=0,
                description="One or more obligations are overdue",
                metadata={"priority": 0.85}
            )

        if "workload.current_pressure" not in sig_keys:
            await pe.register_condition(
                condition_id="cond_workload_pressure",
                condition_type="signal_threshold",
                signal_key="workload.current_pressure",
                operator="greater_than_or_equal",
                target_value=0.75,
                description="High academic workload pressure detected",
                metadata={"priority": 0.7}
            )

    # -----------------------------------------------------------------------
    # Natural Language Interaction & Queries
    # -----------------------------------------------------------------------

    def _format_relative_deadline(self, due_dt: Optional[datetime], now: datetime) -> str:
        if not due_dt:
            return ""
        due_local = due_dt.astimezone(now.tzinfo)
        today_date = now.date()
        due_date = due_local.date()

        t_str = due_local.strftime("%I:%M %p").lstrip("0")
        if due_date == today_date:
            return f"today at {t_str}"
        elif due_date == today_date + timedelta(days=1):
            return f"tomorrow at {t_str}"
        elif 0 < (due_date - today_date).days < 7:
            return f"on {due_local.strftime('%A')} at {t_str}"
        else:
            return f"on {due_local.strftime('%B %d')} at {t_str}"

    async def _find_free_minutes_before_event(self, keyword: str, now: datetime) -> Tuple[Optional[float], str]:
        kw = keyword.lower()
        wsm = WorldStateManager.get_instance()
        fused = await wsm.get_fused_world_state()
        cal = fused.calendar_context or {}
        all_evs = cal.get("all_events", [])

        matching = []
        for ev in all_evs:
            title = ev.get("title", ev.get("summary", "")).lower()
            if kw in title or (kw == "class" and ("class" in title or "lecture" in title)) or (kw == "work" and ("work" in title or "shift" in title)):
                st = ev.get("start")
                if st:
                    ev_dt = CalendarSignalAdapter._parse_event_dt(st)
                    if ev_dt:
                        matching.append((ev_dt.astimezone(now.tzinfo), ev.get("title", ev.get("summary", keyword.title()))))

        if not matching:
            nxt = cal.get("next_event")
            if nxt and nxt.get("start"):
                t = nxt.get("title", "").lower()
                if kw in t or kw in ("class", "event", "meeting", "work"):
                    nxt_dt = CalendarSignalAdapter._parse_event_dt(nxt["start"])
                    if nxt_dt:
                        matching.append((nxt_dt.astimezone(now.tzinfo), nxt.get("title", keyword.title())))

        if not matching:
            return None, keyword.title()

        matching.sort(key=lambda x: x[0])
        for ev_dt, ev_title in matching:
            diff = (ev_dt - now).total_seconds() / 60.0
            if diff > 0:
                return round(diff, 1), f"'{ev_title}' (at {ev_dt.strftime('%I:%M %p').lstrip('0')})"

        return None, keyword.title()

    async def _find_event_start(self, keyword: str, now: datetime) -> Tuple[Optional[datetime], str]:
        free_mins, comm_title = await self._find_free_minutes_before_event(keyword, now)
        if free_mins is None:
            return None, keyword.title()
        return now + timedelta(minutes=free_mins), comm_title

    async def handle_natural_language(self, text: str, context: Optional[dict] = None) -> Optional[str]:
        """
        Deterministically recognize and handle natural language obligation entries, updates,
        queries, and calendar-fusion questions.
        """
        await self.ensure_initialized()
        clean = text.lower().strip(" \t.,!?;:")
        now = CalendarSignalAdapter._get_local_now()

        # 1. Completion: "I finished the Calc assignment"
        m_done = re.search(r'\b(?:i\s+)?(?:finished|completed|done\s+with)\s+(?:the\s+)?([A-Za-z0-9_\-\s]+?)(?:\s+assignment|\s+project|\s+task|\s+exam|\s+quiz|\s+reading)?$', clean)
        if m_done and not re.search(r'\b(?:what|when|can|how|will)\b', clean):
            target = m_done.group(1).strip()
            item = self.find_item(target)
            if item:
                await self.complete_item(item.work_item_id)
                return f"Marked '{item.title}' as completed."

        # 2. Status update: "I started the Java project"
        m_start = re.search(r'\b(?:i\s+)?(?:started|working\s+on)\s+(?:the\s+)?([A-Za-z0-9_\-\s]+?)(?:\s+assignment|\s+project|\s+task|\s+reading)?$', clean)
        if m_start and not re.search(r'\b(?:what|when|can|how|will)\b', clean):
            target = m_start.group(1).strip()
            item = self.find_item(target)
            if item:
                await self.update_item(item.work_item_id, {"status": "in_progress"})
                return f"Marked '{item.title}' as in progress."

        # 3. Effort update: "That assignment should take about 90 minutes"
        m_eff_update = re.search(r'\b(?:that|the|this)\s+(?:assignment|project|task|reading|exam)?\s*(?:should\s+)?(?:take|takes|is\s+about)\s+(?:about\s+)?(\d+|one|two|three|four|five|six|ninety)\s+(minute|hour)s?', clean)
        if m_eff_update and not ("i have" in clean or "my" in clean and "due" in clean):
            effort = extract_effort_minutes(clean)
            if effort > 0:
                active = [i for i in self._items.values() if i.status not in ("completed", "blocked")]
                if active:
                    active.sort(key=lambda x: x.created_at, reverse=True)
                    target_item = active[0]
                    await self.update_item(target_item.work_item_id, {
                        "estimated_minutes": effort,
                        "remaining_minutes": effort
                    })
                    return f"Updated '{target_item.title}' effort to {effort} minutes."

        # 4. Entry / Creation:
        # "I have a Calc assignment due tomorrow at 11:59 PM. It should take about 90 minutes."
        # "My Java project is due Friday."
        # "I have a Discrete exam Monday."
        # "I have a test task due in 10 minutes."
        # "I have a Java project due in two days. I have about three hours left."
        m_entry = re.search(
            r'\b(?:i\s+have\s+(?:a|an)|my)\s+([A-Za-z0-9_\-\s]+?)\s+(assignment|project|exam|quiz|reading|work\s+shift|shift|personal\s+task|task)\b',
            clean
        )
        if m_entry and ("due" in clean or any(d in clean for d in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'tomorrow', 'tonight', 'today'])):
            course = m_entry.group(1).strip()
            raw_type = m_entry.group(2).strip().lower()
            if raw_type == "shift":
                itype = "work_shift"
            elif raw_type == "task":
                itype = "personal_task"
            else:
                itype = raw_type.replace(" ", "_")

            title = f"{course.title()} {itype.replace('_', ' ').title()}"
            due_dt = parse_relative_due_date(clean, now)
            due_iso = due_dt.isoformat() if due_dt else ""

            effort = extract_effort_minutes(clean)

            item = await self.add_item(
                title=title,
                type=itype,
                course_or_project=course.title(),
                due_at=due_iso,
                estimated_minutes=effort,
                remaining_minutes=effort,
                source="natural_language"
            )

            due_str = f" due on {due_dt.strftime('%A, %B %d at %I:%M %p').lstrip('0')}" if due_dt else ""
            effort_str = f" (estimated {effort} minutes)" if effort > 0 else ""
            return f"Added {itype.replace('_', ' ')} '{item.title}'{due_str}{effort_str}."

        # 5. Queries:
        # "What should I work on first?"
        if re.search(r'\b(?:what\s+should\s+i\s+work\s+on\s+first|what\s+to\s+do\s+first|prioritize(?:\s+my)?\s+(?:work|tasks|assignments)?|highest\s+priority\s+task)\b', clean):
            ranked = self.prioritize()
            if not ranked:
                return "You don't have any pending obligations right now."
            top_item, top_score, top_rat = ranked[0]
            t_rem = f", {top_item.remaining_minutes} minutes remaining" if top_item.remaining_minutes else ""
            due_dt = top_item.get_due_datetime()
            due_str = f", due {self._format_relative_deadline(due_dt, now)}" if due_dt else ""

            resp = f"You should work on '{top_item.title}' first ({top_rat}{due_str}{t_rem})."
            if len(ranked) > 1:
                next_item, _, next_rat = ranked[1]
                resp += f" After that, focus on '{next_item.title}'."
            return resp

        # "What do I have due?" / "What do I have left?"
        if re.search(r'\b(?:what\s+(?:do\s+i\s+have|is)\s+due|what\s+do\s+i\s+have\s+left|list\s+(?:my\s+)?(?:assignments|tasks|obligations|work))\b', clean):
            active = [i for i in self._items.values() if i.status not in ("completed", "blocked")]
            if not active:
                return "You have nothing due right now."
            ranked = self.prioritize(active)
            lines = [f"You have {len(active)} pending obligation{'s' if len(active) > 1 else ''}:"]
            for item, score, rat in ranked:
                due_dt = item.get_due_datetime()
                d_str = f" due {self._format_relative_deadline(due_dt, now)}" if due_dt else ""
                rem = f" ({item.remaining_minutes}m left)" if item.remaining_minutes else ""
                lines.append(f"  * '{item.title}'{d_str}{rem}")
            return "\n".join(lines)

        # "What's due tomorrow?"
        if re.search(r'\bwhat(?:\'s|\s+is)\s+due\s+tomorrow\b', clean):
            metrics = await self.compute_workload_metrics(now)
            due_tmrw = metrics.get("due_tomorrow", [])
            if not due_tmrw:
                return "You have nothing due tomorrow."
            lines = [f"You have {len(due_tmrw)} item{'s' if len(due_tmrw) > 1 else ''} due tomorrow:"]
            for item in due_tmrw:
                t_str = f" ({item.remaining_minutes}m remaining)" if item.remaining_minutes else ""
                lines.append(f"  * '{item.title}'{t_str}")
            return "\n".join(lines)

        # "Am I behind on anything?"
        if re.search(r'\b(?:am\s+i\s+behind\s+on\s+anything|any\s+overdue\s+tasks|what\s+is\s+overdue)\b', clean):
            metrics = await self.compute_workload_metrics(now)
            overdue_cnt = metrics.get("total_overdue", 0)
            if overdue_cnt == 0:
                return "You are not behind on anything. No overdue obligations."
            ov_items = [i for i in self._items.values() if i.status == "overdue" or i.is_overdue(now)]
            names = ", ".join([f"'{i.title}'" for i in ov_items])
            return f"You are behind on {overdue_cnt} item{'s' if overdue_cnt > 1 else ''}: {names}."

        # "What's my next deadline?"
        if re.search(r'\b(?:what(?:\'s|\s+is)\s+my\s+next\s+deadline|next\s+deadline)\b', clean):
            metrics = await self.compute_workload_metrics(now)
            nd = metrics.get("next_deadline")
            if not nd:
                return "You have no upcoming deadlines recorded."
            mins = nd.get("minutes_until", 0)
            if mins <= 60:
                t_phrase = f"in {mins} minutes"
            elif mins <= 1440:
                t_phrase = f"in {mins // 60} hours and {mins % 60} minutes"
            else:
                t_phrase = f"in {mins // 1440} days"
            return f"Your next deadline is '{nd['title']}' {t_phrase}."

        # "How much work do I have tonight?" / "today"
        if re.search(r'\bhow\s+much\s+work\s+do\s+i\s+have\s+(?:tonight|today)\b', clean):
            metrics = await self.compute_workload_metrics(now)
            due_td = metrics.get("due_today", [])
            mins_24h = metrics.get("minutes_due_24h", 0)
            if not due_td and mins_24h == 0:
                return "You have no assignments or tasks due tonight."
            items_str = ", ".join([f"'{i.title}'" for i in due_td])
            return f"You have {len(due_td)} item{'s' if len(due_td) > 1 else ''} due tonight ({items_str}), totaling about {mins_24h} minutes of work."

        # 6. Calendar Fusion Queries:
        # "Do I have enough time to finish [X] before [commitment]?"
        m_enough = re.search(r'\bdo\s+i\s+have\s+enough\s+time\s+to\s+finish\s+([A-Za-z0-9_\-\s]+?)\s+before\s+([A-Za-z0-9_\-\s]+?)\??$', clean)
        if m_enough:
            task_name = m_enough.group(1).strip()
            comm_name = m_enough.group(2).strip()
            item = self.find_item(task_name)
            rem_mins = item.remaining_minutes if item else 60
            free_mins, comm_title = await self._find_free_minutes_before_event(comm_name, now)
            if free_mins is None:
                return f"I don't see an upcoming '{comm_name}' on your calendar."

            item_label = f"'{item.title}'" if item else f"'{task_name}'"
            if free_mins >= rem_mins:
                return f"Yes, you have about {int(free_mins)} minutes before {comm_title}, and {item_label} requires about {rem_mins} minutes."
            else:
                return f"No, you only have {int(free_mins)} minutes before {comm_title}, but {item_label} requires about {rem_mins} minutes."

        # "How much work can I get done before [commitment]?"
        m_done_before = re.search(r'\bhow\s+much\s+work\s+can\s+i\s+get\s+done\s+before\s+([A-Za-z0-9_\-\s]+?)\??$', clean)
        if m_done_before:
            comm_name = m_done_before.group(1).strip()
            free_mins, comm_title = await self._find_free_minutes_before_event(comm_name, now)
            if free_mins is None:
                return f"I don't see an upcoming '{comm_name}' on your calendar."
            return f"You have about {int(free_mins)} minutes of free time before {comm_title}."

        # "What's due before my next [class/meeting/commitment]?"
        m_due_before = re.search(r'\bwhat(?:\'s|\s+is)\s+due\s+before\s+(?:my\s+)?next\s+([A-Za-z0-9_\-\s]+?)\??$', clean)
        if m_due_before:
            comm_name = m_due_before.group(1).strip()
            target_dt, comm_title = await self._find_event_start(comm_name, now)
            if not target_dt:
                return f"I don't see an upcoming '{comm_name}' on your calendar."
            items_before = [i for i in self._items.values() if i.status not in ("completed", "blocked") and i.get_due_datetime() and i.get_due_datetime().astimezone(now.tzinfo) <= target_dt]
            if not items_before:
                return f"You have no obligations due before {comm_title}."
            lines = [f"You have {len(items_before)} item{'s' if len(items_before) > 1 else ''} due before {comm_title}:"]
            for i in items_before:
                lines.append(f"  * '{i.title}'")
            return "\n".join(lines)

        return None


# ---------------------------------------------------------------------------
# Natural Language Parsing Helpers
# ---------------------------------------------------------------------------

def parse_relative_due_date(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse relative due phrases into a timezone-aware local datetime."""
    if now is None:
        now = CalendarSignalAdapter._get_local_now()
    clean = text.lower()

    # 1. "in X minutes" / "in X hours" / "in X days"
    m_rel = re.search(r'\bin\s+(\d+|one|two|three|four|five|ten)\s+(minute|hour|day)s?\b', clean)
    if m_rel:
        num_map = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'ten': 10}
        raw_n = m_rel.group(1)
        val = int(raw_n) if raw_n.isdigit() else num_map.get(raw_n, 1)
        unit = m_rel.group(2)
        if unit == 'minute':
            return now + timedelta(minutes=val)
        if unit == 'hour':
            return now + timedelta(hours=val)
        if unit == 'day':
            return (now + timedelta(days=val)).replace(hour=23, minute=59, second=0, microsecond=0)

    # 2. "tomorrow" with optional time
    if "tomorrow" in clean:
        target = now + timedelta(days=1)
        m_time = re.search(r'at\s+(\d+)(?::(\d+))?\s*(pm|am)?', clean)
        if m_time:
            h = int(m_time.group(1))
            m = int(m_time.group(2) or 0)
            mer = (m_time.group(3) or '').lower()
            if mer == 'pm' and h < 12:
                h += 12
            elif mer == 'am' and h == 12:
                h = 0
            return target.replace(hour=h, minute=m, second=0, microsecond=0)
        return target.replace(hour=23, minute=59, second=0, microsecond=0)

    # 3. "today" / "tonight"
    if "today" in clean or "tonight" in clean:
        m_time = re.search(r'at\s+(\d+)(?::(\d+))?\s*(pm|am)?', clean)
        if m_time:
            h = int(m_time.group(1))
            m = int(m_time.group(2) or 0)
            mer = (m_time.group(3) or '').lower()
            if mer == 'pm' and h < 12:
                h += 12
            elif mer == 'am' and h == 12:
                h = 0
            return now.replace(hour=h, minute=m, second=0, microsecond=0)
        return now.replace(hour=23, minute=59, second=0, microsecond=0)

    # 4. Day of the week (e.g. "due Friday", "exam Monday")
    days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    for idx, day in enumerate(days):
        if re.search(r'\b(?:due\s+(?:on\s+|this\s+|next\s+)?|on\s+)?' + day + r'\b', clean):
            diff = (idx - now.weekday()) % 7
            if diff == 0:
                diff = 7  # Next week if today
            target = now + timedelta(days=diff)
            m_time = re.search(r'at\s+(\d+)(?::(\d+))?\s*(pm|am)?', clean)
            if m_time:
                h = int(m_time.group(1))
                m = int(m_time.group(2) or 0)
                mer = (m_time.group(3) or '').lower()
                if mer == 'pm' and h < 12:
                    h += 12
                elif mer == 'am' and h == 12:
                    h = 0
                return target.replace(hour=h, minute=m, second=0, microsecond=0)
            return target.replace(hour=23, minute=59, second=0, microsecond=0)

    return None


def extract_effort_minutes(text: str) -> int:
    """Extract estimated or remaining minutes from natural language."""
    clean = text.lower()
    m_effort = re.search(
        r'(?:take|takes|left|about|have|need)\s+(?:about\s+)?(\d+|one|two|three|four|five|six|ninety)\s+(minute|hour)s?',
        clean
    )
    if m_effort:
        num_map = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'ninety': 90}
        n_str = m_effort.group(1).lower()
        n_val = int(n_str) if n_str.isdigit() else num_map.get(n_str, 0)
        u_str = m_effort.group(2).lower()
        return n_val * 60 if 'hour' in u_str else n_val
    return 0
