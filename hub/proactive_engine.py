"""
hub/proactive_engine.py
-----------------------
Phase 8B: Proactive Context + Contextual Event Engine.
Detects when a World State change matters to an existing task, reminder,
dependency, or user obligation.

Bridges World State changes:
world state change -> relevant condition becomes true -> Cortex generates proactive candidate event.

Features:
- Generic structured Condition model (signals, operators, thresholds, composite AND/OR)
- ProactiveEvent candidate model with priority scoring and context snapshots
- State machine deduplication (false -> true create, true -> true deduplicate,
  true -> false reset, false -> true retrigger)
- Stale signal rejection
- Task dependency integration (waiting -> resumable)
- Contextual reminders (location + time + network)
- Persistence across restarts
"""

import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, asdict

from .database import (
    save_proactive_condition,
    get_proactive_condition,
    list_proactive_conditions,
    update_condition_truth_state,
    update_proactive_condition,
    delete_proactive_condition,
    save_proactive_event,
    get_proactive_event,
    list_proactive_events,
    update_proactive_event_status,
    bound_proactive_events,
    update_task,
    get_task,
)

log = logging.getLogger("cortex.proactive")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class Condition:
    """A generic, structured condition evaluated against World State."""
    condition_id: str
    condition_type: str = "signal"  # signal, signal_threshold, task_dependency, contextual_reminder, composite
    signal_key: str = ""
    operator: str = "equals"  # equals, not_equals, less_than, greater_than, less_than_or_equal, greater_than_or_equal, contains, after_time, before_time
    target_value: Any = None
    clauses: List[Dict[str, Any]] = field(default_factory=list)
    combination: str = "AND"  # AND, OR
    description: str = ""
    related_task_id: Optional[str] = None
    related_goal_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    current_truth_state: bool = False
    last_evaluated_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: Optional[str] = None
    is_active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProactiveEvent:
    """A candidate proactive event triggered by a World State condition transition."""
    event_id: str
    event_type: str  # signal_threshold, task_dependency, contextual_reminder, world_state_change
    source: str = "proactive_engine"
    related_task_id: Optional[str] = None
    related_goal_id: Optional[str] = None
    condition_id: str = ""
    trigger_condition: Dict[str, Any] = field(default_factory=dict)
    context_snapshot: Dict[str, Any] = field(default_factory=dict)
    priority: float = 0.5  # Bounded [0.0, 1.0]
    confidence: float = 1.0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: Optional[str] = None
    status: str = "candidate"  # candidate, queued, delivered, dismissed, expired

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Priority Scoring
# ---------------------------------------------------------------------------

def calculate_priority(
    condition_type: str,
    trigger_signal_key: str,
    actual_value: Any,
    target_value: Any,
    signal_confidence: float = 1.0,
    signal_freshness: float = 1.0,
    task_priority: Optional[str] = None,
    deadline_seconds: Optional[float] = None
) -> float:
    """
    Bounded deterministic scoring [0.0, 1.0] computed from:
    - urgency (0.30)
    - deadline proximity (0.25)
    - task importance (0.20)
    - confidence (0.15)
    - freshness (0.10)
    """
    # 1. Urgency
    urgency = 0.50
    if "battery" in trigger_signal_key:
        try:
            lvl = float(actual_value)
            if lvl <= 15:
                urgency = 0.95
            elif lvl <= 25:
                urgency = 0.85
            elif lvl <= 50:
                urgency = 0.60
            else:
                urgency = 0.35
        except Exception:
            urgency = 0.50
    elif condition_type == "task_dependency":
        urgency = 0.70
    elif condition_type == "contextual_reminder":
        urgency = 0.60

    # 2. Deadline proximity
    if deadline_seconds is not None:
        if deadline_seconds <= 900:  # <= 15m
            deadline_prox = 0.95
        elif deadline_seconds <= 3600:  # <= 1h
            deadline_prox = 0.70
        elif deadline_seconds <= 86400:  # <= 24h
            deadline_prox = 0.40
        else:
            deadline_prox = 0.10
    else:
        deadline_prox = 0.0

    # 3. Task importance
    if task_priority in ("high", "urgent"):
        task_imp = 0.90
    elif task_priority in ("medium", "normal"):
        task_imp = 0.60
    elif condition_type == "task_dependency":
        task_imp = 0.75
    else:
        task_imp = 0.20

    # 4. Confidence
    conf = max(0.0, min(1.0, float(signal_confidence)))

    # 5. Freshness
    fresh = max(0.0, min(1.0, float(signal_freshness)))

    score = (
        0.30 * urgency +
        0.25 * deadline_prox +
        0.20 * task_imp +
        0.15 * conf +
        0.10 * fresh
    )
    return max(0.0, min(1.0, round(score, 2)))


# ---------------------------------------------------------------------------
# Proactive Engine
# ---------------------------------------------------------------------------

class ProactiveEngine:
    """
    Core engine managing structured conditions, evaluating them against World State,
    and generating candidate proactive events upon false -> true transitions.
    """
    _instance: Optional["ProactiveEngine"] = None

    def __init__(self):
        self._conditions: Dict[str, Condition] = {}
        self._events: Dict[str, ProactiveEvent] = {}

    @classmethod
    def get_instance(cls) -> "ProactiveEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset in-memory state for testing."""
        if cls._instance:
            cls._instance._conditions.clear()
            cls._instance._events.clear()

    async def load_from_db(self) -> None:
        """Load persisted conditions and candidate events on startup."""
        persisted_conds = await list_proactive_conditions(active_only=False)
        for c_data in persisted_conds:
            cond = Condition(
                condition_id=c_data["condition_id"],
                condition_type=c_data.get("condition_type", "signal"),
                signal_key=c_data.get("signal_key", ""),
                operator=c_data.get("operator", "equals"),
                target_value=c_data.get("target_value"),
                clauses=c_data.get("clauses", []),
                combination=c_data.get("combination", "AND"),
                description=c_data.get("description", ""),
                related_task_id=c_data.get("related_task_id"),
                related_goal_id=c_data.get("related_goal_id"),
                metadata=c_data.get("metadata", {}),
                current_truth_state=c_data.get("current_truth_state", False),
                last_evaluated_at=c_data.get("last_evaluated_at"),
                created_at=c_data.get("created_at", datetime.now(timezone.utc).isoformat()),
                expires_at=c_data.get("expires_at"),
                is_active=c_data.get("is_active", True)
            )
            self._conditions[cond.condition_id] = cond

        persisted_events = await list_proactive_events(status=None, limit=100)
        for e_data in persisted_events:
            ev = ProactiveEvent(
                event_id=e_data["event_id"],
                event_type=e_data["event_type"],
                source=e_data.get("source", "proactive_engine"),
                related_task_id=e_data.get("related_task_id"),
                related_goal_id=e_data.get("related_goal_id"),
                condition_id=e_data["condition_id"],
                trigger_condition=e_data.get("trigger_condition", {}),
                context_snapshot=e_data.get("context_snapshot", {}),
                priority=e_data.get("priority", 0.5),
                confidence=e_data.get("confidence", 1.0),
                created_at=e_data.get("created_at", datetime.now(timezone.utc).isoformat()),
                expires_at=e_data.get("expires_at"),
                status=e_data.get("status", "candidate")
            )
            self._events[ev.event_id] = ev

    async def register_condition(
        self,
        condition: Optional[Condition] = None,
        **kwargs
    ) -> Condition:
        """Register a new structured condition and persist to SQLite."""
        if condition is None:
            cond_id = kwargs.get("condition_id") or f"cond_{uuid.uuid4().hex[:12]}"
            condition = Condition(
                condition_id=cond_id,
                condition_type=kwargs.get("condition_type", "signal"),
                signal_key=kwargs.get("signal_key", kwargs.get("signal", "")),
                operator=kwargs.get("operator", "equals"),
                target_value=kwargs.get("target_value", kwargs.get("value")),
                clauses=kwargs.get("clauses", []),
                combination=kwargs.get("combination", "AND"),
                description=kwargs.get("description", ""),
                related_task_id=kwargs.get("related_task_id"),
                related_goal_id=kwargs.get("related_goal_id"),
                metadata=kwargs.get("metadata", {}),
                expires_at=kwargs.get("expires_at")
            )

        self._conditions[condition.condition_id] = condition
        await save_proactive_condition(condition.to_dict())
        log.info(f"[ProactiveEngine] Registered condition: {condition.condition_id} ({condition.description or condition.signal_key})")
        return condition

    async def register_task_dependency(
        self,
        task_id: str,
        waiting_for_signal: str,
        target_value: Any = True,
        operator: str = "equals",
        description: str = ""
    ) -> Condition:
        """Convenience method to register a task dependency condition."""
        desc = description or f"Task {task_id} waiting for {waiting_for_signal}"
        cond = Condition(
            condition_id=f"cond_task_{uuid.uuid4().hex[:8]}",
            condition_type="task_dependency",
            signal_key=waiting_for_signal,
            operator=operator,
            target_value=target_value,
            related_task_id=task_id,
            description=desc
        )
        return await self.register_condition(cond)

    async def list_conditions(self, active_only: bool = True) -> List[Condition]:
        """List active or all registered conditions."""
        if active_only:
            return [c for c in self._conditions.values() if c.is_active]
        return list(self._conditions.values())

    async def get_condition(self, condition_id: str) -> Optional[Condition]:
        """Retrieve condition by ID."""
        return self._conditions.get(condition_id)

    def _resolve_signal_value(
        self,
        sig_key: str,
        now: datetime
    ) -> Tuple[Optional[Any], Optional[float], Optional[float]]:
        """
        Resolve current value, confidence, and freshness fraction for a signal key.
        Returns (None, None, None) if missing or STALE.
        """
        from .world_state import WorldStateManager
        ws_mgr = WorldStateManager.get_instance()
        signals = ws_mgr._signals

        # Check exact key first
        sig = signals.get(sig_key)
        
        # Check alias keys if not found
        if not sig:
            if sig_key.startswith("battery.level:"):
                dev = sig_key.split(":", 1)[1]
                sig = signals.get(f"device.battery:{dev}")
            elif sig_key == "battery.level":
                # Look for any fresh device battery signal or active device
                if ws_mgr._active_device_id:
                    sig = signals.get(f"device.battery:{ws_mgr._active_device_id}")
                if not sig:
                    for k, s in signals.items():
                        if k.startswith("device.battery:"):
                            sig = s
                            break
            elif sig_key.startswith("device.online:"):
                dev = sig_key.split(":", 1)[1]
                # Check explicit signal
                sig = signals.get(sig_key) or signals.get(f"device.heartbeat:{dev}")
            elif sig_key == "location.label":
                sig = signals.get("location.current")
            elif sig_key == "calendar.time_until_next_event":
                sig = signals.get("calendar.time_until_next_event") or signals.get("calendar.summary") or signals.get("calendar.next") or signals.get("calendar.current")

        if not sig:
            return None, None, None

        # STALE CHECK: ignore stale world state signals
        if not sig.is_fresh(now):
            log.debug(f"[ProactiveEngine] Ignored stale signal: {sig.key} (expired)")
            return None, None, None

        confidence = sig.confidence
        freshness = max(0.0, min(1.0, sig.remaining_ttl(now) / max(1.0, sig.ttl)))

        # Extract value from payload
        payload = sig.payload
        val = None

        if sig_key.startswith("battery.level") or "battery" in sig_key:
            val = payload.get("battery_level")
        elif sig_key.startswith("device.online:"):
            val = payload.get("is_online", payload.get("is_connected", True))
        elif sig_key == "location.label":
            val = payload.get("label")
        elif sig_key == "calendar.time_until_next_event":
            val = payload.get("time_until_seconds") if payload.get("time_until_seconds") is not None else payload.get("seconds", payload.get("value", payload.get("time_until_next_event")))
        elif "value" in payload:
            val = payload["value"]
        elif len(payload) == 1:
            val = next(iter(payload.values()))
        else:
            val = payload

        return val, confidence, freshness

    def _eval_clause(self, clause: Dict[str, Any], now: datetime) -> Tuple[Optional[bool], Optional[Any], float, float]:
        """
        Evaluate a single condition clause against World State.
        Returns (result_bool_or_None, actual_value, confidence, freshness).
        """
        sig_key = clause.get("signal") or clause.get("signal_key", "")
        op = clause.get("operator", "equals").lower()
        target = clause.get("value", clause.get("target_value"))

        # Time-based clauses (e.g. signal="time.hour" or op in ("after_time", "before_time"))
        if sig_key.startswith("time") or op in ("after_time", "before_time"):
            return self._eval_time_clause(sig_key, op, target, now)

        actual_val, conf, fresh = self._resolve_signal_value(sig_key, now)
        if actual_val is None:
            # Signal missing or stale: cannot evaluate to true
            return None, None, 1.0, 1.0

        res = self._compare_values(actual_val, op, target)
        return res, actual_val, (conf or 1.0), (fresh or 1.0)

    def _eval_time_clause(self, sig_key: str, op: str, target: Any, now: datetime) -> Tuple[bool, Any, float, float]:
        """Evaluate time-of-day or time threshold conditions."""
        cur_hour = now.hour
        cur_minute = now.minute

        if op == "after_time":
            # target format: "HH:MM" or "17:00"
            try:
                parts = str(target).split(":")
                tgt_hour = int(parts[0])
                tgt_min = int(parts[1]) if len(parts) > 1 else 0
                now_mins = cur_hour * 60 + cur_minute
                tgt_mins = tgt_hour * 60 + tgt_min
                return (now_mins >= tgt_mins), f"{cur_hour:02d}:{cur_minute:02d}", 1.0, 1.0
            except Exception:
                return False, None, 1.0, 1.0

        elif op == "before_time":
            try:
                parts = str(target).split(":")
                tgt_hour = int(parts[0])
                tgt_min = int(parts[1]) if len(parts) > 1 else 0
                now_mins = cur_hour * 60 + cur_minute
                tgt_mins = tgt_hour * 60 + tgt_min
                return (now_mins < tgt_mins), f"{cur_hour:02d}:{cur_minute:02d}", 1.0, 1.0
            except Exception:
                return False, None, 1.0, 1.0

        if sig_key == "time.hour":
            actual_val = cur_hour
            res = self._compare_values(actual_val, op, target)
            return res, actual_val, 1.0, 1.0

        return False, None, 1.0, 1.0

    @staticmethod
    def _compare_values(actual: Any, op: str, target: Any) -> bool:
        """Compare actual value against target value with operator."""
        try:
            if op == "equals":
                if isinstance(actual, str) and isinstance(target, str):
                    return actual.strip().lower() == target.strip().lower()
                if isinstance(actual, bool) or isinstance(target, bool):
                    return bool(actual) == bool(target)
                return actual == target

            elif op == "not_equals":
                if isinstance(actual, str) and isinstance(target, str):
                    return actual.strip().lower() != target.strip().lower()
                return actual != target

            elif op == "less_than":
                return float(actual) < float(target)

            elif op == "greater_than":
                return float(actual) > float(target)

            elif op == "less_than_or_equal":
                return float(actual) <= float(target)

            elif op == "greater_than_or_equal":
                return float(actual) >= float(target)

            elif op == "contains":
                return str(target).lower() in str(actual).lower()

        except Exception as e:
            log.debug(f"[ProactiveEngine] Comparison error ({actual} {op} {target}): {e}")
            return False

        return False

    async def evaluate(self, force: bool = False) -> List[ProactiveEvent]:
        """
        Evaluate all active registered conditions against the current World State.
        Detects transitions:
        - false -> true : create event, mark task resumable if applicable
        - true -> true  : deduplicate (no duplicate event)
        - true -> false : reset state to false
        - false -> true : retrigger new event
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        generated_events: List[ProactiveEvent] = []

        for cond_id, cond in list(self._conditions.items()):
            if not cond.is_active:
                continue

            # Check expiration
            if cond.expires_at:
                try:
                    exp_dt = datetime.fromisoformat(cond.expires_at)
                    if now > exp_dt:
                        cond.is_active = False
                        await update_proactive_condition(cond_id, {"is_active": False})
                        log.info(f"[ProactiveEngine] Condition {cond_id} expired.")
                        continue
                except Exception:
                    pass

            # Evaluate condition clauses
            is_truth: bool = False
            last_actual_val: Any = None
            avg_conf: float = 1.0
            avg_fresh: float = 1.0

            if cond.clauses:
                # Composite condition
                clause_results = []
                confs = []
                freshes = []
                for cl in cond.clauses:
                    c_res, act_v, c_conf, c_fresh = self._eval_clause(cl, now)
                    if c_res is None:
                        # Missing or stale signal in clause
                        clause_results.append(False)
                    else:
                        clause_results.append(c_res)
                    last_actual_val = act_v
                    confs.append(c_conf)
                    freshes.append(c_fresh)

                if cond.combination.upper() == "OR":
                    is_truth = any(clause_results)
                else:  # AND
                    is_truth = all(clause_results) if clause_results else False

                avg_conf = sum(confs) / len(confs) if confs else 1.0
                avg_fresh = sum(freshes) / len(freshes) if freshes else 1.0
            else:
                # Single clause condition
                single_clause = {
                    "signal": cond.signal_key,
                    "operator": cond.operator,
                    "value": cond.target_value
                }
                c_res, act_v, c_conf, c_fresh = self._eval_clause(single_clause, now)
                is_truth = bool(c_res) if c_res is not None else False
                last_actual_val = act_v
                avg_conf = c_conf
                avg_fresh = c_fresh

            prev_truth = cond.current_truth_state

            # State Machine Deduplication & Transitions
            if not prev_truth and is_truth:
                # -----------------------------------------------------------
                # Transition: FALSE -> TRUE
                # Create proactive candidate event
                # -----------------------------------------------------------
                log.info(f"[ProactiveEngine] Condition {cond_id} transitioned: FALSE -> TRUE")
                cond.current_truth_state = True
                cond.last_evaluated_at = now_iso
                await update_condition_truth_state(cond_id, True, now_iso)

                # Assemble context snapshot
                from .world_state import WorldStateManager
                ws = await WorldStateManager.get_instance().get_fused_world_state()
                ctx_snapshot = {
                    "active_device_id": ws.active_device_id,
                    "user_presence": ws.user_presence,
                    "battery_states": ws.battery_states,
                    "network_context": ws.network_context,
                    "foreground_activity": ws.foreground_activity,
                    "location_context": ws.location_context,
                    "calendar_context": ws.calendar_context,
                    "trigger_value": last_actual_val
                }

                # Priority scoring
                prio = calculate_priority(
                    condition_type=cond.condition_type,
                    trigger_signal_key=cond.signal_key,
                    actual_value=last_actual_val,
                    target_value=cond.target_value,
                    signal_confidence=avg_conf,
                    signal_freshness=avg_fresh
                )

                # Determine event type
                ev_type = cond.condition_type
                if ev_type == "signal":
                    ev_type = "signal_threshold"

                ev_id = f"pe_{uuid.uuid4().hex[:12]}"
                event = ProactiveEvent(
                    event_id=ev_id,
                    event_type=ev_type,
                    source="proactive_engine",
                    related_task_id=cond.related_task_id,
                    related_goal_id=cond.related_goal_id,
                    condition_id=cond.condition_id,
                    trigger_condition={
                        "condition_id": cond.condition_id,
                        "signal_key": cond.signal_key,
                        "operator": cond.operator,
                        "target_value": cond.target_value,
                        "actual_value": last_actual_val,
                        "description": cond.description
                    },
                    context_snapshot=ctx_snapshot,
                    priority=prio,
                    confidence=avg_conf,
                    created_at=now_iso,
                    expires_at=(now + timedelta(hours=2)).isoformat(),
                    status="candidate"
                )

                self._events[ev_id] = event
                await save_proactive_event(event.to_dict())
                await bound_proactive_events(max_events=100)
                generated_events.append(event)

                # Task Integration: mark waiting task as resumable
                if cond.related_task_id:
                    task = await get_task(cond.related_task_id)
                    if task and task.get("status") in ("waiting", "pending", "paused"):
                        await update_task(cond.related_task_id, {"status": "resumable"})
                        log.info(f"[ProactiveEngine] Marked task {cond.related_task_id} as 'resumable' due to condition {cond_id}")

            elif prev_truth and is_truth:
                # -----------------------------------------------------------
                # Transition: TRUE -> TRUE
                # Deduplicate: do NOT create candidate event
                # -----------------------------------------------------------
                cond.last_evaluated_at = now_iso

            elif prev_truth and not is_truth:
                # -----------------------------------------------------------
                # Transition: TRUE -> FALSE
                # Reset state
                # -----------------------------------------------------------
                log.info(f"[ProactiveEngine] Condition {cond_id} transitioned: TRUE -> FALSE (reset)")
                cond.current_truth_state = False
                cond.last_evaluated_at = now_iso
                await update_condition_truth_state(cond_id, False, now_iso)

            else:
                # FALSE -> FALSE
                cond.last_evaluated_at = now_iso

        return generated_events

    async def list_candidates(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List active candidate proactive events."""
        db_events = await list_proactive_events(status="candidate", limit=limit)
        return db_events

    async def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a proactive event by ID."""
        ev = self._events.get(event_id)
        if ev:
            return ev.to_dict()
        return await get_proactive_event(event_id)

    async def dismiss(self, event_id: str) -> bool:
        """Dismiss a proactive candidate event."""
        if event_id in self._events:
            self._events[event_id].status = "dismissed"
        success = await update_proactive_event_status(event_id, "dismissed")
        log.info(f"[ProactiveEngine] Dismissed proactive event: {event_id} (success={success})")
        return success
