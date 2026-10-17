"""
hub/attention_router.py
-----------------------
Phase 8C: Attention Router + Delivery Decision Foundation.
Evaluates user, device, and channel context for proactive candidate events
and determines target device, channel, timing, and interruption policy.

Flow:
proactive event -> evaluate user/device context -> choose target device/channel/timing -> create delivery decision.

Features:
- DeliveryDecision model (voice, desktop_notification, in_app, queued)
- Generic bounded scoring: availability + recency + suitability + urgency - interruption_cost
- Interruption policy (high-priority override, busy deferral, offline fallback, queueing)
- Multi-device support (asus, phone, desktop)
- Capability registry checks (voice, notifications, in-app UI)
- Deferred delivery & automatic reevaluation on World State changes
- Persistence across restarts
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, asdict

from .database import (
    save_delivery_decision,
    get_delivery_decision,
    list_delivery_decisions,
    update_delivery_decision_status,
    delete_delivery_decision,
    bound_delivery_decisions,
    list_devices,
    get_device,
    get_device_capabilities,
)

log = logging.getLogger("cortex.attention")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class DeliveryDecision:
    """A structured decision on how and when to deliver a proactive event."""
    decision_id: str
    proactive_event_id: str
    target_device_id: Optional[str] = None
    channel: str = "desktop_notification"  # voice, desktop_notification, in_app, queued
    timing: str = "now"                    # now, deferred, scheduled
    reason: str = ""
    confidence: float = 1.0
    score: float = 0.5                     # Bounded [0.0, 1.0]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    deliver_after: Optional[str] = None
    expires_at: Optional[str] = None
    status: str = "pending"                # pending, ready, deferred, delivered, cancelled, expired

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Device / Channel Scoring
# ---------------------------------------------------------------------------

SUPPORTED_CHANNELS = ["desktop_notification", "voice", "in_app"]

CHANNEL_CAPABILITY_MAP = {
    "desktop_notification": ["ui.notify", "desktop_notification", "ui.notification"],
    "voice": ["voice", "tts.speak", "audio.output"],
    "in_app": ["ui.render", "in_app", "ui.in_app"]
}


def score_device_channel(
    device_id: str,
    channel: str,
    event_priority: float,
    is_online: bool,
    is_active_device: bool,
    device_capabilities: List[str],
    user_presence: str = "active",
    is_calendar_busy: bool = False,
    is_voice_session_active: bool = False,
    last_interaction_recent: bool = False,
    learned_routine_bonus: float = 0.0
) -> Tuple[float, str]:
    """
    Compute bounded deterministic delivery score [0.0, 1.0]:
    delivery_score = availability + recency + suitability + urgency + routine_bonus - interruption_cost
    Returns (score, rationale).
    """
    # 1. Availability check
    if not is_online:
        return 0.0, f"Device '{device_id}' is offline."

    availability = 0.35

    # 2. Channel capability check
    required_caps = CHANNEL_CAPABILITY_MAP.get(channel, [])
    has_cap = any(c in device_capabilities for c in required_caps)
    
    # Built-in fallback: asus desktop/laptop supports desktop_notification by default
    if not has_cap and channel == "desktop_notification" and ("asus" in device_id.lower() or "laptop" in device_id.lower() or "desktop" in device_id.lower()):
        has_cap = True

    if channel == "voice" and is_voice_session_active:
        has_cap = True

    if not has_cap:
        return 0.0, f"Device '{device_id}' does not support channel '{channel}'."

    # 3. Recency & active device
    if is_active_device:
        recency = 0.25
    elif last_interaction_recent:
        recency = 0.15
    else:
        recency = 0.05

    # 4. Suitability
    if channel == "desktop_notification":
        suitability = 0.20
    elif channel == "voice":
        suitability = 0.25 if is_voice_session_active else 0.10
    elif channel == "in_app":
        suitability = 0.15
    else:
        suitability = 0.10

    # 5. Urgency
    urgency = 0.20 * max(0.0, min(1.0, event_priority))

    # 6. Interruption cost
    interruption_cost = 0.0
    if is_calendar_busy:
        interruption_cost += 0.30
    if user_presence == "away":
        interruption_cost += 0.20
    elif user_presence == "idle":
        interruption_cost += 0.10

    # 7. Learned device routine bonus (Phase 8F - small and strictly bounded)
    clamped_bonus = max(0.0, min(0.10, learned_routine_bonus))

    raw_score = availability + recency + suitability + urgency + clamped_bonus - interruption_cost
    final_score = max(0.0, min(1.0, round(raw_score, 2)))

    bonus_str = f", routine_bonus={clamped_bonus:.2f}" if clamped_bonus > 0 else ""
    rationale = (
        f"avail={availability}, recency={recency}, suit={suitability}, "
        f"urgency={urgency:.2f}{bonus_str}, cost={interruption_cost:.2f} -> {final_score}"
    )
    return final_score, rationale


# ---------------------------------------------------------------------------
# Attention Router
# ---------------------------------------------------------------------------

class AttentionRouter:
    """
    Evaluates proactive events and World State to make delivery decisions
    (target device, channel, timing, deferrals, and reevaluations).
    """
    _instance: Optional["AttentionRouter"] = None

    def __init__(self):
        self._decisions: Dict[str, DeliveryDecision] = {}

    @classmethod
    def get_instance(cls) -> "AttentionRouter":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset in-memory state for testing."""
        if cls._instance:
            cls._instance._decisions.clear()

    async def load_from_db(self) -> None:
        """Load persisted delivery decisions from SQLite."""
        persisted = await list_delivery_decisions(status=None, limit=100)
        for p in persisted:
            dec = DeliveryDecision(
                decision_id=p["decision_id"],
                proactive_event_id=p["proactive_event_id"],
                target_device_id=p.get("target_device_id"),
                channel=p.get("channel", "desktop_notification"),
                timing=p.get("timing", "now"),
                reason=p.get("reason", ""),
                confidence=p.get("confidence", 1.0),
                score=p.get("score", 0.5),
                created_at=p.get("created_at", datetime.now(timezone.utc).isoformat()),
                deliver_after=p.get("deliver_after"),
                expires_at=p.get("expires_at"),
                status=p.get("status", "pending")
            )
            self._decisions[dec.decision_id] = dec

    def set_control_layer(self, control_layer: Any) -> None:
        self._control_layer = control_layer

    def get_control_layer(self) -> Optional[Any]:
        if hasattr(self, "_control_layer") and self._control_layer:
            return self._control_layer
        try:
            import hub.main
            if hasattr(hub.main, "_control_layer") and hub.main._control_layer:
                return hub.main._control_layer
        except Exception:
            pass
        return None

    async def _dispatch_decision(self, dec: DeliveryDecision) -> bool:
        """
        Minimal real delivery: for desktop_notification, dispatch ui.notify
        to the target device via CortexControlLayer if connected.
        """
        if dec.channel != "desktop_notification" or not dec.target_device_id:
            return False

        try:
            ctrl = self.get_control_layer()
            if not ctrl:
                return False

            ws = ctrl.connected_devices.get(dec.target_device_id)
            if ws is None:
                return False

            from .proactive_engine import ProactiveEngine
            pe = ProactiveEngine.get_instance()
            ev = await pe.get_event(dec.proactive_event_id)
            title = "Cortex Proactive Notification"
            message = "New proactive event detected."
            if ev:
                tc = ev.get("trigger_condition") or {}
                desc = tc.get("description")
                if desc:
                    message = desc
                else:
                    sig_key = tc.get("signal_key", "event")
                    target_val = tc.get("target_value", "")
                    message = f"{sig_key} reached threshold ({target_val})"

            log.info(f"[AttentionRouter] Dispatching ui.notify to '{dec.target_device_id}' for decision {dec.decision_id}")
            cmd_id = await ctrl.execute_command(
                target_device=dec.target_device_id,
                action="ui.notify",
                args={"title": title, "message": message}
            )
            dec.status = "delivered"
            await update_delivery_decision_status(dec.decision_id, "delivered")
            log.info(f"[AttentionRouter] Notification dispatched successfully (cmd_id={cmd_id}) | decision {dec.decision_id} -> delivered")
            return True
        except Exception as e:
            log.warning(f"[AttentionRouter] Delivery dispatch failed for decision {dec.decision_id}: {e}")
            return False

    async def route_event(
        self,
        event_id_or_data: Any,
        force_now: bool = False
    ) -> Optional[DeliveryDecision]:
        """
        Evaluate user/device context for a proactive event and create a DeliveryDecision.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # 1. Resolve proactive event
        from .proactive_engine import ProactiveEngine, ProactiveEvent
        pe_engine = ProactiveEngine.get_instance()

        if isinstance(event_id_or_data, str):
            ev_id = event_id_or_data
            ev = await pe_engine.get_event(ev_id)
        elif isinstance(event_id_or_data, dict):
            ev_id = event_id_or_data.get("event_id")
            ev = event_id_or_data
        elif hasattr(event_id_or_data, "event_id"):
            ev_id = event_id_or_data.event_id
            ev = event_id_or_data.to_dict() if hasattr(event_id_or_data, "to_dict") else event_id_or_data.__dict__
        else:
            log.warning(f"[AttentionRouter] Invalid event target: {event_id_or_data}")
            return None

        if not ev:
            log.warning(f"[AttentionRouter] Event '{ev_id}' not found.")
            return None

        # Check deduplication: avoid duplicate decisions for the same proactive event
        for d in self._decisions.values():
            if d.proactive_event_id == ev_id and d.status in ("pending", "ready", "deferred", "delivered"):
                log.info(f"[AttentionRouter] Decision already exists for event {ev_id}: {d.decision_id}")
                return d

        # Check event expiration
        exp_str = ev.get("expires_at")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(exp_str)
                if now > exp_dt:
                    log.info(f"[AttentionRouter] Event {ev_id} is expired; skipping delivery.")
                    expired_dec = DeliveryDecision(
                        decision_id=f"dd_{uuid.uuid4().hex[:12]}",
                        proactive_event_id=ev_id,
                        timing="now",
                        status="expired",
                        reason="Proactive event expired before routing."
                    )
                    self._decisions[expired_dec.decision_id] = expired_dec
                    await save_delivery_decision(expired_dec.to_dict())
                    return expired_dec
            except Exception:
                pass

        event_priority = float(ev.get("priority", 0.5))

        # 2. Inspect World State
        from .world_state import WorldStateManager
        wsm = WorldStateManager.get_instance()
        world_state = await wsm.get_fused_world_state()

        active_device_id = world_state.active_device_id
        user_presence = world_state.user_presence
        cal_ctx = world_state.calendar_context or {}
        is_cal_busy = bool(cal_ctx.get("is_busy", False) or cal_ctx.get("current_event"))
        if not is_cal_busy:
            for k, sig in wsm._signals.items():
                if k.startswith("calendar.") and sig.is_fresh(now):
                    if sig.payload.get("is_busy") or sig.payload.get("current_event") or sig.payload.get("busy"):
                        is_cal_busy = True
                        break

        # 3. Enumerate eligible devices
        db_devices = await list_devices()
        connected_dev_ids = {d.get("device_id") for d in world_state.connected_devices}
        explicit_offline = set()

        # Check signals for device online states
        for k, sig in wsm._signals.items():
            if k.startswith("device.online:") and sig.is_fresh(now):
                dev_name = k.split(":", 1)[1]
                is_on = sig.payload.get("is_online", sig.payload.get("is_connected", True))
                if is_on:
                    connected_dev_ids.add(dev_name)
                else:
                    explicit_offline.add(dev_name)

        device_candidates: List[Dict[str, Any]] = []

        # Gather devices
        seen_ids = set()
        for d in db_devices:
            d_id = d["device_id"]
            seen_ids.add(d_id)
            if d_id in explicit_offline:
                is_online = False
            else:
                is_online = (d_id in connected_dev_ids) or (d.get("connection_state") == "connected")
            caps = await get_device_capabilities(d_id)
            device_candidates.append({
                "device_id": d_id,
                "is_online": is_online,
                "capabilities": caps,
                "is_active": (d_id == active_device_id and is_online)
            })

        # Include any connected devices not yet in DB
        for c_id in connected_dev_ids:
            if c_id not in seen_ids and c_id and c_id not in explicit_offline:
                device_candidates.append({
                    "device_id": c_id,
                    "is_online": True,
                    "capabilities": ["ui.notify", "desktop_notification"],
                    "is_active": (c_id == active_device_id)
                })

        # Fallback for active device if no list available
        if not device_candidates and active_device_id:
            is_act_on = active_device_id not in explicit_offline
            device_candidates.append({
                "device_id": active_device_id,
                "is_online": is_act_on,
                "capabilities": ["ui.notify", "desktop_notification"],
                "is_active": is_act_on
            })

        # 4. Score each device/channel pair
        scored_pairs: List[Tuple[float, str, str, str]] = []  # (score, device_id, channel, rationale)

        for dev in device_candidates:
            d_id = dev["device_id"]
            is_on = dev["is_online"]
            is_act = dev["is_active"]
            caps = dev["capabilities"]

            # Phase 8F: Check learned device usage bonus from routines
            dev_bonus = 0.0
            try:
                from .routines import RoutineManager
                rm = RoutineManager.get_instance()
                exp = rm.get_expected_routines()
                curr_exp = exp.get("current_expected")
                if curr_exp and curr_exp.get("category") == "device_usage":
                    r_obj = rm.get_routine(curr_exp.get("routine_id"))
                    if r_obj and r_obj.metadata.get("target_device", "").lower() == d_id.lower():
                        dev_bonus = 0.05
            except Exception:
                pass

            for chan in SUPPORTED_CHANNELS:
                sc, rat = score_device_channel(
                    device_id=d_id,
                    channel=chan,
                    event_priority=event_priority,
                    is_online=is_on,
                    is_active_device=is_act,
                    device_capabilities=caps,
                    user_presence=user_presence,
                    is_calendar_busy=is_cal_busy,
                    is_voice_session_active=False,
                    last_interaction_recent=is_act,
                    learned_routine_bonus=dev_bonus
                )
                if sc > 0.0:
                    scored_pairs.append((sc, d_id, chan, rat))

        # 5. Apply Interruption Policy
        decision_id = f"dd_{uuid.uuid4().hex[:12]}"

        # Urgent override: event priority >= 0.75 bypasses calendar busy deferral
        is_urgent = (event_priority >= 0.75) or force_now

        if not scored_pairs:
            # No eligible device/channel found -> queue for reevaluation
            dec = DeliveryDecision(
                decision_id=decision_id,
                proactive_event_id=ev_id,
                target_device_id=None,
                channel="queued",
                timing="deferred",
                status="deferred",
                score=0.0,
                confidence=1.0,
                deliver_after=(now + timedelta(minutes=5)).isoformat(),
                reason="No eligible device online; queued for reevaluation."
            )
        elif is_cal_busy and not is_urgent:
            # User busy and event is not urgent -> defer delivery
            best_sc, best_dev, best_chan, best_rat = max(scored_pairs, key=lambda x: x[0])
            dec = DeliveryDecision(
                decision_id=decision_id,
                proactive_event_id=ev_id,
                target_device_id=best_dev,
                channel=best_chan,
                timing="deferred",
                status="deferred",
                score=best_sc,
                confidence=1.0,
                deliver_after=(now + timedelta(minutes=15)).isoformat(),
                reason=f"Deferred due to active calendar meeting (event priority={event_priority:.2f})."
            )
        else:
            # Eligible device selected for delivery
            # Sort by score descending; if tie, prefer active device
            scored_pairs.sort(key=lambda x: (x[0], 1 if x[1] == active_device_id else 0), reverse=True)
            best_sc, best_dev, best_chan, best_rat = scored_pairs[0]

            reason_str = f"Routed to '{best_dev}' via '{best_chan}' (score={best_sc:.2f})."
            if is_urgent and is_cal_busy:
                reason_str += " Urgent event overrode busy calendar state."

            dec = DeliveryDecision(
                decision_id=decision_id,
                proactive_event_id=ev_id,
                target_device_id=best_dev,
                channel=best_chan,
                timing="now",
                status="ready",
                score=best_sc,
                confidence=1.0,
                reason=reason_str
            )

        # 6. Persist decision
        self._decisions[dec.decision_id] = dec
        await save_delivery_decision(dec.to_dict())
        await bound_delivery_decisions(max_decisions=100)
        log.info(f"[AttentionRouter] Created decision: {dec.decision_id} | status={dec.status} | target={dec.target_device_id} | channel={dec.channel}")

        if dec.status == "ready" and dec.timing == "now":
            await self._dispatch_decision(dec)

        return dec

    async def reevaluate(self) -> List[DeliveryDecision]:
        """
        Reevaluate all deferred or pending delivery decisions against current World State.
        Returns list of decisions that transitioned to 'ready'.
        """
        now = datetime.now(timezone.utc)
        ready_decisions: List[DeliveryDecision] = []

        for dec_id, dec in list(self._decisions.items()):
            if dec.status not in ("deferred", "pending"):
                continue

            # Check expiration
            if dec.expires_at:
                try:
                    exp_dt = datetime.fromisoformat(dec.expires_at)
                    if now > exp_dt:
                        dec.status = "expired"
                        await update_delivery_decision_status(dec_id, "expired")
                        continue
                except Exception:
                    pass

            # Re-run route evaluation without creating a new duplicate decision
            from .proactive_engine import ProactiveEngine
            pe_engine = ProactiveEngine.get_instance()
            ev = await pe_engine.get_event(dec.proactive_event_id)
            if not ev:
                continue

            from .world_state import WorldStateManager
            wsm = WorldStateManager.get_instance()
            world_state = await wsm.get_fused_world_state()

            active_device_id = world_state.active_device_id
            cal_ctx = world_state.calendar_context or {}
            is_cal_busy = bool(cal_ctx.get("is_busy", False) or cal_ctx.get("current_event"))
            if not is_cal_busy:
                for k, sig in wsm._signals.items():
                    if k.startswith("calendar.") and sig.is_fresh(now):
                        if sig.payload.get("is_busy") or sig.payload.get("current_event") or sig.payload.get("busy"):
                            is_cal_busy = True
                            break

            user_presence = world_state.user_presence

            # Check device availability
            connected_dev_ids = {d.get("device_id") for d in world_state.connected_devices}
            explicit_offline = set()
            for k, sig in wsm._signals.items():
                if k.startswith("device.online:") and sig.is_fresh(now):
                    dev_name = k.split(":", 1)[1]
                    is_on = sig.payload.get("is_online", sig.payload.get("is_connected", True))
                    if is_on:
                        connected_dev_ids.add(dev_name)
                    else:
                        explicit_offline.add(dev_name)

            db_devices = await list_devices()
            device_candidates = []
            seen_ids = set()
            for d in db_devices:
                d_id = d["device_id"]
                seen_ids.add(d_id)
                if d_id in explicit_offline:
                    is_online = False
                else:
                    is_online = (d_id in connected_dev_ids) or (d.get("connection_state") == "connected")
                caps = await get_device_capabilities(d_id)
                device_candidates.append({
                    "device_id": d_id,
                    "is_online": is_online,
                    "capabilities": caps,
                    "is_active": (d_id == active_device_id and is_online)
                })

            for c_id in connected_dev_ids:
                if c_id not in seen_ids and c_id and c_id not in explicit_offline:
                    device_candidates.append({
                        "device_id": c_id,
                        "is_online": True,
                        "capabilities": ["ui.notify", "desktop_notification"],
                        "is_active": (c_id == active_device_id)
                    })

            scored_pairs = []
            ev_priority = float(ev.get("priority", 0.5))

            for dev in device_candidates:
                d_id = dev["device_id"]
                is_on = dev["is_online"]
                is_act = dev["is_active"]
                caps = dev["capabilities"]

                for chan in SUPPORTED_CHANNELS:
                    sc, rat = score_device_channel(
                        device_id=d_id,
                        channel=chan,
                        event_priority=ev_priority,
                        is_online=is_on,
                        is_active_device=is_act,
                        device_capabilities=caps,
                        user_presence=user_presence,
                        is_calendar_busy=is_cal_busy,
                        is_voice_session_active=False,
                        last_interaction_recent=is_act
                    )
                    if sc > 0.0:
                        scored_pairs.append((sc, d_id, chan, rat))

            if scored_pairs and (not is_cal_busy or ev_priority >= 0.75):
                scored_pairs.sort(key=lambda x: (x[0], 1 if x[1] == active_device_id else 0), reverse=True)
                best_sc, best_dev, best_chan, _ = scored_pairs[0]

                dec.target_device_id = best_dev
                dec.channel = best_chan
                dec.timing = "now"
                dec.status = "ready"
                dec.score = best_sc
                dec.reason = f"Reevaluated on context change: ready for '{best_dev}' via '{best_chan}'."
                await update_delivery_decision_status(dec_id, "ready", {
                    "target_device_id": best_dev,
                    "channel": best_chan,
                    "timing": "now",
                    "score": best_sc,
                    "reason": dec.reason
                })
                ready_decisions.append(dec)
                log.info(f"[AttentionRouter] Reevaluation: decision {dec_id} transitioned to READY for {best_dev}")

                await self._dispatch_decision(dec)

        return ready_decisions

    async def get_decision(self, decision_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a delivery decision by ID."""
        dec = self._decisions.get(decision_id)
        if dec:
            return dec.to_dict()
        return await get_delivery_decision(decision_id)

    async def list_pending(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List decisions that are pending, ready, or deferred."""
        results = []
        for d in self._decisions.values():
            if d.status in ("pending", "ready", "deferred"):
                results.append(d.to_dict())
        if not results:
            db_list = await list_delivery_decisions(status=None, limit=limit)
            results = [d for d in db_list if d.get("status") in ("pending", "ready", "deferred")]
        return results[:limit]

    async def cancel(self, decision_id: str) -> bool:
        """Cancel a delivery decision."""
        if decision_id in self._decisions:
            self._decisions[decision_id].status = "cancelled"
        success = await update_delivery_decision_status(decision_id, "cancelled")
        log.info(f"[AttentionRouter] Cancelled delivery decision {decision_id} (success={success})")
        return success
