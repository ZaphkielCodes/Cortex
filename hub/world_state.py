"""
hub/world_state.py
------------------
Phase 8A/8D: World State and Context Fusion Foundation.
Maintains a bounded, continuously updated World State representing the user's
current situation across devices, location, calendar, foreground activity, tasks,
battery, and network.

Phase 8D additions:
- RuntimeTimeAdapter: authoritative local datetime/timezone/UTC offset from OS
- Calendar timezone-aware datetime handling
- Expanded calendar query window (today through end of tomorrow)
- Runtime time signals injected into compact world summary
"""

import os
import asyncio
from .config import get_hub_device_id
import json
import logging
import ctypes
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, asdict

from .database import (
    save_world_state_signal,
    get_world_state_signals,
    delete_world_state_signal,
    add_context_event,
    get_recent_context_events,
    get_device,
    list_devices,
    get_task,
)

log = logging.getLogger("cortex.world_state")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """A generic, atomic context signal with provenance and freshness."""
    key: str
    source: str
    timestamp: str  # ISO-8601 UTC
    confidence: float = 1.0
    ttl: float = 300.0  # seconds
    payload: Dict[str, Any] = field(default_factory=dict)

    def get_datetime(self) -> datetime:
        try:
            return datetime.fromisoformat(self.timestamp)
        except Exception:
            return datetime.now(timezone.utc)

    def is_fresh(self, now: Optional[datetime] = None) -> bool:
        if now is None:
            now = datetime.now(timezone.utc)
        sig_dt = self.get_datetime()
        age_seconds = (now - sig_dt).total_seconds()
        return age_seconds <= self.ttl

    def remaining_ttl(self, now: Optional[datetime] = None) -> float:
        if now is None:
            now = datetime.now(timezone.utc)
        sig_dt = self.get_datetime()
        age_seconds = (now - sig_dt).total_seconds()
        return max(0.0, self.ttl - age_seconds)


@dataclass
class WorldState:
    """The fused situation representation of the user and their environment."""
    timestamp: str
    user_presence: str  # "active", "idle", "away", "unknown"
    active_device_id: Optional[str]
    connected_devices: List[Dict[str, Any]]
    location_context: Dict[str, Any]
    calendar_context: Dict[str, Any]
    foreground_activity: Dict[str, Any]
    current_task: Optional[Dict[str, Any]]
    network_context: Dict[str, Any]
    battery_states: Dict[str, Any]
    recent_context_events: List[Dict[str, Any]]
    confidence: float
    freshness_metadata: Dict[str, Any]
    system_telemetry: Dict[str, Any] = field(default_factory=dict)
    workload_context: Dict[str, Any] = field(default_factory=dict)
    routine_context: Dict[str, Any] = field(default_factory=dict)
    screen_perception: Dict[str, Any] = field(default_factory=dict)
    foreground_history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Signal Adapters & Providers
# ---------------------------------------------------------------------------

class BaseSignalAdapter:
    """Base class for context signal providers and adapters."""
    @property
    def source_name(self) -> str:
        return "generic"


class DeviceSignalAdapter(BaseSignalAdapter):
    """Adapts device telemetry (battery, charging, network, foreground, hardware) into signals."""
    @property
    def source_name(self) -> str:
        return "device_agent"

    @staticmethod
    def inspect_system_telemetry(device_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Inspect live system telemetry directly via safe local OS APIs (psutil, platform, winreg).
        Returns deterministic hardware and resource utilization metrics without subprocess shelling.
        """
        device_id = device_id or get_hub_device_id()
        import platform
        import psutil

        # 1. CPU & RAM
        try:
            # A bounded sample is valid on the first call and on a new worker thread.
            cpu_pct = float(psutil.cpu_percent(interval=0.1))
        except Exception:
            cpu_pct = None

        try:
            vmem = psutil.virtual_memory()
            ram_pct = round(float(vmem.percent), 1)
            ram_used_gb = round(float(vmem.used) / (1024**3), 2)
            ram_total_gb = round(float(vmem.total) / (1024**3), 2)
        except Exception:
            ram_pct, ram_used_gb, ram_total_gb = 0.0, 0.0, 0.0

        # 2. Disk
        try:
            drive = "C:\\" if os.name == "nt" else "/"
            disk = psutil.disk_usage(drive)
            disk_pct = round(float(disk.percent), 1)
            disk_free_gb = round(float(disk.free) / (1024**3), 2)
            disk_total_gb = round(float(disk.total) / (1024**3), 2)
        except Exception:
            disk_pct, disk_free_gb, disk_total_gb = 0.0, 0.0, 0.0

        # 3. Hostname & OS
        hostname = platform.node() or "localhost"
        os_str = f"{platform.system()} {platform.release()}"

        # 4. Hardware metadata (Registry on Windows)
        mfg = "Unknown"
        model = "PC"
        processor = platform.processor() or "Unknown CPU"
        gpu_list: List[str] = []

        if os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as key:
                    try:
                        mfg = str(winreg.QueryValueEx(key, "SystemManufacturer")[0]).strip()
                    except Exception:
                        pass
                    try:
                        model = str(winreg.QueryValueEx(key, "SystemProductName")[0]).strip()
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                    processor = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
            except Exception:
                pass

            try:
                import winreg
                class_key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, class_key) as k:
                    subkeys, _, _ = winreg.QueryInfoKey(k)
                    for i in range(subkeys):
                        try:
                            subkey_name = winreg.EnumKey(k, i)
                            with winreg.OpenKey(k, subkey_name) as subk:
                                desc, _ = winreg.QueryValueEx(subk, "DriverDesc")
                                if desc and str(desc).strip() not in gpu_list:
                                    gpu_list.append(str(desc).strip())
                        except Exception:
                            pass
            except Exception:
                pass

        gpu_str = ", ".join(gpu_list) if gpu_list else "Standard Display Adapter"

        return {
            "device_id": device_id,
            "cpu_percent": cpu_pct,
            "ram_percent": ram_pct,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "disk_percent": disk_pct,
            "disk_free_gb": disk_free_gb,
            "disk_total_gb": disk_total_gb,
            "hostname": hostname,
            "os": os_str,
            "manufacturer": mfg,
            "model": model,
            "processor": processor,
            "gpu": gpu_str,
            "gpus": gpu_list
        }

    @staticmethod
    def create_telemetry_signals(device_id: Optional[str] = None, telemetry: Optional[Dict[str, Any]] = None) -> List[Signal]:
        """Creates atomic Phase 8D telemetry signals for the device."""
        device_id = device_id or get_hub_device_id()
        telem = telemetry if telemetry is not None else DeviceSignalAdapter.inspect_system_telemetry(device_id)
        now = datetime.now(timezone.utc).isoformat()
        sigs: List[Signal] = []

        # CPU (15s TTL)
        sigs.append(Signal(
            key=f"device.cpu_percent:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=15.0,
            payload={"value": telem["cpu_percent"], "percent": telem["cpu_percent"]}
        ))

        # RAM (30s TTL)
        sigs.append(Signal(
            key=f"device.ram_percent:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=30.0,
            payload={"value": telem["ram_percent"], "percent": telem["ram_percent"]}
        ))
        sigs.append(Signal(
            key=f"device.ram_used_gb:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=30.0,
            payload={"value": telem["ram_used_gb"], "used_gb": telem["ram_used_gb"]}
        ))
        sigs.append(Signal(
            key=f"device.ram_total_gb:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=30.0,
            payload={"value": telem["ram_total_gb"], "total_gb": telem["ram_total_gb"]}
        ))

        # Disk (300s TTL)
        sigs.append(Signal(
            key=f"device.disk_percent:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=300.0,
            payload={"value": telem["disk_percent"], "percent": telem["disk_percent"]}
        ))
        sigs.append(Signal(
            key=f"device.disk_free_gb:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=300.0,
            payload={"value": telem["disk_free_gb"], "free_gb": telem["disk_free_gb"]}
        ))
        sigs.append(Signal(
            key=f"device.disk_total_gb:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=300.0,
            payload={"value": telem["disk_total_gb"], "total_gb": telem["disk_total_gb"]}
        ))

        # Hardware Metadata (3600s TTL)
        sigs.append(Signal(
            key=f"device.hostname:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=3600.0,
            payload={"value": telem["hostname"], "hostname": telem["hostname"]}
        ))
        sigs.append(Signal(
            key=f"device.os:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=3600.0,
            payload={"value": telem["os"], "os": telem["os"]}
        ))
        sigs.append(Signal(
            key=f"device.manufacturer:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=3600.0,
            payload={"value": telem["manufacturer"], "manufacturer": telem["manufacturer"]}
        ))
        sigs.append(Signal(
            key=f"device.model:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=3600.0,
            payload={"value": telem["model"], "model": telem["model"]}
        ))
        sigs.append(Signal(
            key=f"device.processor:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=3600.0,
            payload={"value": telem["processor"], "processor": telem["processor"]}
        ))
        sigs.append(Signal(
            key=f"device.gpu:{device_id}",
            source="windows_telemetry",
            timestamp=now,
            confidence=1.0,
            ttl=3600.0,
            payload={"value": telem["gpu"], "gpu": telem["gpu"], "gpus": telem["gpus"]}
        ))

        return sigs

    @staticmethod
    def create_battery_signal(device_id: str, battery_level: int, is_charging: bool, ttl: float = 300.0) -> Signal:
        now = datetime.now(timezone.utc).isoformat()
        return Signal(
            key=f"device.battery:{device_id}",
            source="device_agent",
            timestamp=now,
            confidence=0.98,
            ttl=ttl,
            payload={
                "device_id": device_id,
                "battery_level": int(battery_level),
                "is_charging": bool(is_charging)
            }
        )

    @staticmethod
    def create_network_signal(device_id: str, network_state: str, is_connected: bool = True, ttl: float = 300.0) -> Signal:
        now = datetime.now(timezone.utc).isoformat()
        return Signal(
            key=f"device.network:{device_id}",
            source="device_agent",
            timestamp=now,
            confidence=0.95,
            ttl=ttl,
            payload={
                "device_id": device_id,
                "network_state": network_state,
                "is_connected": is_connected
            }
        )

    @staticmethod
    def inspect_network_state() -> Dict[str, Any]:
        """Inspect host internet connectivity via rapid socket check."""
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 53))
            s.close()
            return {"network_state": "connected", "is_connected": True}
        except Exception:
            return {"network_state": "offline", "is_connected": False}

    @staticmethod
    def create_foreground_signal(device_id: str, app_name: str, window_title: str = "", ttl: float = 60.0) -> Signal:
        now = datetime.now(timezone.utc).isoformat()
        return Signal(
            key=f"device.foreground:{device_id}",
            source="device_agent",
            timestamp=now,
            confidence=0.95,
            ttl=ttl,
            payload={
                "device_id": device_id,
                "app_name": app_name,
                "window_title": window_title
            }
        )


# ---------------------------------------------------------------------------
# Phase 8D: Runtime Time Adapter
# ---------------------------------------------------------------------------

class RuntimeTimeAdapter:
    """
    Provides authoritative runtime temporal context directly from the OS.
    Never hardcodes timezone — always reads from system.
    """

    @staticmethod
    def get_local_now() -> datetime:
        """Returns the host's current local timezone-aware datetime."""
        return datetime.now().astimezone()

    @staticmethod
    def get_local_timezone_info() -> Dict[str, Any]:
        """
        Returns the host's current local timezone name and UTC offset.
        Uses Python's datetime library which reads from the OS timezone database.
        """
        try:
            import time as _time
            local_now = datetime.now().astimezone()
            tz_offset = local_now.utcoffset()
            total_seconds = int(tz_offset.total_seconds())
            sign = "+" if total_seconds >= 0 else "-"
            abs_sec = abs(total_seconds)
            offset_hours = abs_sec // 3600
            offset_mins = (abs_sec % 3600) // 60
            utc_offset_str = f"UTC{sign}{offset_hours:02d}:{offset_mins:02d}"

            # Try to get a human-readable TZ name
            tz_name = local_now.tzname() or ""
            # On Windows, tzname() may be a generic name; enrich via time module
            if not tz_name or tz_name.startswith("UTC"):
                import time as _time_mod
                tz_name = _time_mod.tzname[0] if not _time.daylight else _time_mod.tzname[1]

            return {
                "timezone": tz_name,
                "utc_offset": utc_offset_str,
                "utc_offset_seconds": total_seconds,
            }
        except Exception as e:
            log.warning(f"[RuntimeTime] Failed to get timezone info: {e}")
            return {"timezone": "UTC", "utc_offset": "UTC+00:00", "utc_offset_seconds": 0}

    @staticmethod
    def get_runtime_time_context() -> Dict[str, Any]:
        """
        Returns a complete runtime temporal context dictionary from OS clock.
        All values are computed from local system time — never hardcoded.
        """
        local_now = datetime.now().astimezone()
        utc_now = datetime.now(timezone.utc)
        tz_info = RuntimeTimeAdapter.get_local_timezone_info()

        today = local_now.date()
        tomorrow = today + timedelta(days=1)
        day_of_week = local_now.strftime("%A")  # e.g. "Friday"
        tomorrow_name = tomorrow.strftime("%A")

        return {
            "local_datetime": local_now.isoformat(),
            "local_date": today.isoformat(),
            "local_time": local_now.strftime("%H:%M:%S"),
            "local_time_12h": local_now.strftime("%I:%M %p"),
            "timezone": tz_info["timezone"],
            "utc_offset": tz_info["utc_offset"],
            "utc_offset_seconds": tz_info["utc_offset_seconds"],
            "day_of_week": day_of_week,
            "tomorrow_date": tomorrow.isoformat(),
            "tomorrow_name": tomorrow_name,
            "utc_datetime": utc_now.isoformat(),
        }

    @staticmethod
    def create_time_signals(ttl: float = 60.0) -> List["Signal"]:
        """
        Creates atomic Phase 8D time signals from OS runtime clock.
        TTL is short (60s) so these are always fresh.
        """
        ctx = RuntimeTimeAdapter.get_runtime_time_context()
        now_iso = ctx["utc_datetime"]
        signals: List[Signal] = []

        # time.local_datetime (60s TTL — refreshed on each query)
        signals.append(Signal(
            key="time.local_datetime",
            source="runtime_clock",
            timestamp=now_iso,
            confidence=1.0,
            ttl=ttl,
            payload=ctx
        ))
        # time.timezone (3600s TTL — only changes if user changes system TZ)
        signals.append(Signal(
            key="time.timezone",
            source="runtime_clock",
            timestamp=now_iso,
            confidence=1.0,
            ttl=3600.0,
            payload={
                "timezone": ctx["timezone"],
                "utc_offset": ctx["utc_offset"],
                "value": ctx["timezone"]
            }
        ))
        return signals


class CalendarSignalAdapter(BaseSignalAdapter):
    """
    Extracts bounded calendar context:
    - current event
    - next event
    - time until next event
    - free/busy window

    Phase 8D: All datetime comparisons are timezone-aware using local time.
    """
    @property
    def source_name(self) -> str:
        return "calendar_provider"

    @staticmethod
    def _get_local_now() -> datetime:
        """Returns the current moment as a timezone-aware local datetime from OS."""
        return datetime.now().astimezone()

    @staticmethod
    def _parse_event_dt(dt_str: str) -> Optional[datetime]:
        """
        Parse a Google Calendar datetime string into a timezone-aware datetime.
        All-day events (date-only strings like '2026-10-10') are treated as
        midnight local time. Never returns a naive datetime.
        """
        if not dt_str:
            return None
        s = str(dt_str).strip()
        # All-day event: date-only
        if len(s) == 10 and s[4] == '-' and s[7] == '-':
            try:
                local_tz = datetime.now().astimezone().tzinfo
                return datetime.fromisoformat(s).replace(tzinfo=local_tz)
            except Exception:
                return None
        # Normal datetime string — may or may not have tz info
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                # Treat naive datetimes as local
                dt = dt.astimezone()
            return dt
        except Exception:
            return None

    @staticmethod
    def parse_events(events: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Parses raw calendar events into bounded current/next event context.
        Phase 8D: All comparisons are timezone-aware using local time.
        """
        if now is None:
            now = CalendarSignalAdapter._get_local_now()
        elif now.tzinfo is None:
            # Ensure 'now' is timezone-aware
            now = now.astimezone()

        current_event = None
        next_event = None
        upcoming_events = []
        diff_seconds = None
        diff_mins = None

        for ev in (events or []):
            # Parse start and end
            start_val = ev.get("start")
            start_str = (start_val.get("dateTime") or start_val.get("date")) if isinstance(start_val, dict) else start_val
            end_val = ev.get("end")
            end_str = (end_val.get("dateTime") or end_val.get("date")) if isinstance(end_val, dict) else end_val
            summary = ev.get("summary") or ev.get("title") or "Untitled Event"

            if not start_str:
                continue

            start_dt = CalendarSignalAdapter._parse_event_dt(str(start_str))
            if start_dt is None:
                continue
            end_dt = CalendarSignalAdapter._parse_event_dt(str(end_str)) if end_str else start_dt + timedelta(hours=1)
            if end_dt is None:
                end_dt = start_dt + timedelta(hours=1)

            event_obj = {
                "title": summary,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "location": ev.get("location", "")
            }

            if start_dt <= now <= end_dt:
                current_event = event_obj
            elif start_dt > now:
                upcoming_events.append((start_dt, event_obj))

        upcoming_events.sort(key=lambda x: x[0])
        if upcoming_events:
            next_start, next_event = upcoming_events[0]
            diff_seconds = max(0, int((next_start - now).total_seconds()))
            diff_mins = diff_seconds // 60
            if diff_mins < 60:
                time_until = f"{diff_mins} minutes"
            else:
                hours = diff_mins // 60
                rem_mins = diff_mins % 60
                time_until = f"{hours} hour{'s' if hours > 1 else ''}" + (f" {rem_mins} minutes" if rem_mins else "")
            free_until = next_event["start"]
        else:
            time_until = None
            free_until = "rest of the day"

        # Determine free/busy window
        if current_event:
            free_busy = f"Busy: '{current_event['title']}' until {current_event['end']}"
            busy = True
            free_until = current_event["end"]
        elif next_event:
            free_busy = f"Free for {time_until} until '{next_event['title']}'"
            busy = False
        else:
            free_busy = "Free for the rest of the day"
            busy = False

        return {
            "current_event": current_event,
            "next_event": next_event,
            "time_until_next_event": time_until,
            "time_until_seconds": diff_seconds,
            "time_until_minutes": diff_mins,
            "busy": busy,
            "free_until": free_until,
            "free_busy_window": free_busy,
            "all_events": ([current_event] if current_event else []) + [e[1] for e in upcoming_events],
            "updated_at": now.isoformat()
        }

    @staticmethod
    def parse_events_for_date(
        events: List[Dict[str, Any]],
        target_date,  # date object
        local_tz=None
    ) -> List[Dict[str, Any]]:
        """
        Returns all events that occur on target_date in local time.
        Handles timezone-aware and all-day events correctly.
        """
        if local_tz is None:
            local_tz = datetime.now().astimezone().tzinfo

        result = []
        for ev in (events or []):
            start_val = ev.get("start")
            start_str = (start_val.get("dateTime") or start_val.get("date")) if isinstance(start_val, dict) else start_val
            end_val = ev.get("end")
            end_str = (end_val.get("dateTime") or end_val.get("date")) if isinstance(end_val, dict) else end_val
            summary = ev.get("summary") or ev.get("title") or "Untitled Event"

            if not start_str:
                continue

            is_all_day = False
            if isinstance(start_val, dict) and "date" in start_val and "dateTime" not in start_val:
                is_all_day = True
            elif isinstance(start_str, str) and len(str(start_str).strip()) == 10 and str(start_str).strip()[4] == '-' and str(start_str).strip()[7] == '-':
                is_all_day = True

            start_dt = CalendarSignalAdapter._parse_event_dt(str(start_str))
            if start_dt is None:
                continue
            end_dt = CalendarSignalAdapter._parse_event_dt(str(end_str)) if end_str else start_dt + timedelta(hours=1)
            if end_dt is None:
                end_dt = start_dt + timedelta(hours=1)

            # Convert to local timezone for date comparison
            local_start = start_dt.astimezone(local_tz)
            local_end = end_dt.astimezone(local_tz)
            target_start = datetime.combine(target_date, datetime.min.time(), tzinfo=local_tz)
            target_end = datetime.combine(target_date + timedelta(days=1), datetime.min.time(), tzinfo=local_tz)

            if (local_start < target_end) and (local_end > target_start):
                time_str = "all day" if is_all_day else local_start.strftime("%I:%M %p").lstrip("0")
                result.append({
                    "title": summary,
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "local_start": time_str,
                    "all_day": is_all_day,
                    "location": ev.get("location", "")
                })

        result.sort(key=lambda e: e["start"])
        return result

    @staticmethod
    async def get_connection_status(account_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Check OAuth configuration and credential status for Google Calendar.
        Returns:
            {"connected": bool, "status": "connected"|"disconnected"|"token_expired"|"unavailable", "account_id": ..., "message": ...}
        """
        from .database import get_integrations
        from .credentials import CredentialStore
        import os

        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

        try:
            integrations = await get_integrations()
            cal_integrations = [i for i in integrations if i.get("plugin_name") == "calendar" and i.get("status") == "active"]
            if not cal_integrations:
                status = "unavailable" if not (client_id and client_secret) else "disconnected"
                return {
                    "connected": False,
                    "status": status,
                    "account_id": None,
                    "message": "Google Calendar is disconnected."
                }

            target_acc = account_id or cal_integrations[0].get("account_id") or "primary"
            tokens = CredentialStore.get_tokens(f"calendar_{target_acc}")
            if not tokens:
                return {
                    "connected": False,
                    "status": "disconnected",
                    "account_id": target_acc,
                    "message": f"No credentials stored for calendar account {target_acc}."
                }

            from .integrations.calendar import CalendarIntegration
            plugin = CalendarIntegration()
            valid_token = await plugin.get_valid_token(target_acc)
            if not valid_token:
                return {
                    "connected": False,
                    "status": "token_expired",
                    "account_id": target_acc,
                    "message": "Google Calendar access token is expired."
                }

            return {
                "connected": True,
                "status": "connected",
                "account_id": target_acc,
                "message": f"Connected to Google Calendar ({target_acc})."
            }
        except Exception as e:
            log.warning(f"Failed to check calendar connection status: {e}")
            return {
                "connected": False,
                "status": "unavailable",
                "account_id": account_id,
                "message": f"Calendar service unavailable: {str(e)}"
            }

    @staticmethod
    async def fetch_and_create_signals(
        account_id: Optional[str] = None,
        raw_events: Optional[List[Dict[str, Any]]] = None,
        now: Optional[datetime] = None,
        ttl: float = 300.0
    ) -> List[Signal]:
        """
        Produces atomic, structured Phase 8D World State signals for Google Calendar:
        calendar.connected, calendar.current_event, calendar.current_event.start/end/title,
        calendar.next_event, calendar.next_event.start/end/title,
        calendar.time_until_next_event, calendar.busy, calendar.free_until, calendar.summary.

        Phase 8D Fix: Uses local timezone-aware 'now', expands query window to cover
        today through end of tomorrow so events are never missed.
        Also stores raw_events in payload for date-specific follow-up queries.
        """
        if now is None:
            now = CalendarSignalAdapter._get_local_now()
        elif now.tzinfo is None:
            now = now.astimezone()
        now_iso = now.isoformat()

        if raw_events is not None:
            conn_status = {"connected": True, "status": "connected", "account_id": account_id or "test_account"}
            parsed = CalendarSignalAdapter.parse_events(raw_events, now=now)
            cached_raw = raw_events
        else:
            conn_status = await CalendarSignalAdapter.get_connection_status(account_id)
            cached_raw = []
            if conn_status.get("connected"):
                try:
                    from .integrations.calendar import CalendarIntegration
                    plugin = CalendarIntegration()
                    acc = conn_status.get("account_id") or "primary"
                    # Phase 8D Fix: query from start of today through end of tomorrow
                    # to ensure we always capture today's remaining events and all of tomorrow's
                    local_tz = now.tzinfo
                    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    tomorrow_end = (today_start + timedelta(days=2)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    time_min = today_start.isoformat()
                    time_max = tomorrow_end.isoformat()
                    res = await plugin.execute_action(
                        acc, "calendar.read",
                        time_min=time_min,
                        time_max=time_max
                    )
                    items = res.get("items", []) if isinstance(res, dict) else []
                    cached_raw = items
                    parsed = CalendarSignalAdapter.parse_events(items, now=now)
                except Exception as e:
                    log.warning(f"Error fetching live Google Calendar events: {e}")
                    conn_status = {"connected": False, "status": "unavailable", "message": str(e)}
                    parsed = CalendarSignalAdapter.parse_events([], now=now)
            else:
                parsed = CalendarSignalAdapter.parse_events([], now=now)

        is_connected = conn_status.get("connected", False)
        status = conn_status.get("status", "disconnected")
        cur = parsed.get("current_event")
        nxt = parsed.get("next_event")
        diff_seconds = parsed.get("time_until_seconds")
        diff_mins = parsed.get("time_until_minutes")
        time_until_str = parsed.get("time_until_next_event")
        free_until = parsed.get("free_until")
        busy = parsed.get("busy", False)

        signals: List[Signal] = []

        # calendar.connected
        signals.append(Signal(
            key="calendar.connected",
            source="google_calendar",
            timestamp=now_iso,
            confidence=1.0,
            ttl=ttl,
            payload={
                "connected": is_connected,
                "status": status,
                "account_id": conn_status.get("account_id"),
                "value": is_connected
            }
        ))

        # calendar.current_event
        signals.append(Signal(
            key="calendar.current_event",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload=cur if cur else {"title": None, "start": None, "end": None, "location": None}
        ))

        # calendar.current_event.start
        signals.append(Signal(
            key="calendar.current_event.start",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload={"value": cur.get("start") if cur else None}
        ))

        # calendar.current_event.end
        signals.append(Signal(
            key="calendar.current_event.end",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload={"value": cur.get("end") if cur else None}
        ))

        # calendar.current_event.title
        signals.append(Signal(
            key="calendar.current_event.title",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload={"value": cur.get("title") if cur else None}
        ))

        # calendar.next_event
        signals.append(Signal(
            key="calendar.next_event",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload=nxt if nxt else {"title": None, "start": None, "end": None, "location": None}
        ))

        # calendar.next_event.start
        signals.append(Signal(
            key="calendar.next_event.start",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload={"value": nxt.get("start") if nxt else None}
        ))

        # calendar.next_event.end
        signals.append(Signal(
            key="calendar.next_event.end",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload={"value": nxt.get("end") if nxt else None}
        ))

        # calendar.next_event.title
        signals.append(Signal(
            key="calendar.next_event.title",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload={"value": nxt.get("title") if nxt else None}
        ))

        # calendar.time_until_next_event (numeric seconds payload for proactive conditions)
        signals.append(Signal(
            key="calendar.time_until_next_event",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=min(60.0, ttl),
            payload={
                "value": diff_seconds,
                "seconds": diff_seconds,
                "time_until_seconds": diff_seconds,
                "minutes": diff_mins,
                "formatted": time_until_str
            }
        ))

        # calendar.busy
        signals.append(Signal(
            key="calendar.busy",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload={"value": busy, "busy": busy}
        ))

        # calendar.free_until
        signals.append(Signal(
            key="calendar.free_until",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload={"value": free_until, "free_until": free_until}
        ))

        # calendar.summary (includes raw_events for date-targeted follow-ups)
        summary_payload = dict(parsed)
        summary_payload["connected"] = is_connected
        summary_payload["status"] = status
        signals.append(Signal(
            key="calendar.summary",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload=summary_payload
        ))

        # calendar.raw_events — stores full event list for date-specific queries
        signals.append(Signal(
            key="calendar.raw_events",
            source="google_calendar",
            timestamp=now_iso,
            confidence=0.95,
            ttl=ttl,
            payload={"events": cached_raw, "account_id": conn_status.get("account_id")}
        ))

        return signals

    @staticmethod
    def create_calendar_signal(calendar_summary: Dict[str, Any], ttl: float = 300.0) -> Signal:
        now = datetime.now(timezone.utc).isoformat()
        payload = dict(calendar_summary)
        if "connected" not in payload:
            payload["connected"] = bool(payload.get("current_event") or payload.get("next_event"))
        return Signal(
            key="calendar.summary",
            source="calendar_provider",
            timestamp=now,
            confidence=0.95,
            ttl=ttl,
            payload=payload
        )


class LocationSignalAdapter(BaseSignalAdapter):
    """
    Generic location signal interface.
    Supports manual updates, mocks, and future phone-provided location.
    """
    @property
    def source_name(self) -> str:
        return "location_provider"

    @staticmethod
    def create_location_signal(
        label: str,
        address: str = "",
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        confidence: float = 0.9,
        ttl: float = 3600.0  # 1 hour default
    ) -> Signal:
        now = datetime.now(timezone.utc).isoformat()
        return Signal(
            key="location.current",
            source="location_provider",
            timestamp=now,
            confidence=confidence,
            ttl=ttl,
            payload={
                "label": label,
                "address": address,
                "latitude": latitude,
                "longitude": longitude
            }
        )


class ActivitySignalAdapter(BaseSignalAdapter):
    """
    Foreground window / app activity signal interface.
    Supports native Windows inspection with fallback to mock or last-reported signal.
    """
    @property
    def source_name(self) -> str:
        return "activity_provider"

    @staticmethod
    def inspect_windows_foreground() -> Optional[Dict[str, str]]:
        """Inspect current Windows foreground window title if available."""
        if os.name != "nt":
            return None
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return None
            buf = ctypes.create_unicode_buffer(512)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value.strip()
            if not title:
                return None
            
            # Simple heuristic for app name from window title
            app_name = title.split(" - ")[-1] if " - " in title else title
            return {"app_name": app_name, "window_title": title}
        except Exception as e:
            log.debug(f"Native foreground window check failed: {e}")
            return None

    @classmethod
    def create_activity_signal(cls, device_id: str, app_name: str, window_title: str = "", ttl: float = 60.0) -> Signal:
        now = datetime.now(timezone.utc).isoformat()
        return Signal(
            key=f"device.foreground:{device_id}",
            source="activity_provider",
            timestamp=now,
            confidence=0.95,
            ttl=ttl,
            payload={
                "device_id": device_id,
                "app_name": app_name,
                "window_title": window_title or app_name
            }
        )


class TaskSignalAdapter(BaseSignalAdapter):
    """Adapts active tasks into a world state signal."""
    @property
    def source_name(self) -> str:
        return "task_orchestrator"

    @staticmethod
    def create_task_signal(task_id: str, title: str, status: str = "in_progress", ttl: float = 300.0) -> Signal:
        now = datetime.now(timezone.utc).isoformat()
        return Signal(
            key="task.active",
            source="task_orchestrator",
            timestamp=now,
            confidence=1.0,
            ttl=ttl,
            payload={
                "id": task_id,
                "title": title,
                "status": status
            }
        )


# ---------------------------------------------------------------------------
# World State Manager (Context Fusion Engine)
# ---------------------------------------------------------------------------

class WorldStateManager:
    """
    Fuses generic context signals into a unified, bounded, continuously updated World State.
    Resolves newer vs stale values, tracks freshness/TTL, and records bounded context events.
    """
    _instance: Optional["WorldStateManager"] = None

    def __init__(self):
        self._signals: Dict[str, Signal] = {}
        self._recent_events: List[Dict[str, Any]] = []
        self._foreground_history: List[Dict[str, Any]] = []
        self._active_device_id: Optional[str] = None
        self._last_interaction_at: Optional[str] = None

    @classmethod
    def get_instance(cls) -> "WorldStateManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset in-memory state for testing."""
        if cls._instance:
            cls._instance._signals.clear()
            cls._instance._recent_events.clear()
            cls._instance._foreground_history.clear()
            cls._instance._active_device_id = None
            cls._instance._last_interaction_at = None

    def record_foreground_switch(
        self,
        app_name: str,
        window_title: str = "",
        window_id: Optional[str] = None,
        device_id: Optional[str] = None,
        pid: Optional[int] = None,
    ) -> None:
        """Record a foreground window/app activation into bounded history."""
        if not app_name or app_name.lower() in ("unknown", "none"):
            return
        entry = {
            "app_name": app_name,
            "window_title": window_title or app_name,
            "window_id": str(window_id) if window_id is not None else None,
            "device_id": device_id or self._active_device_id or "local",
            "pid": pid,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # Avoid duplicate consecutive entries for the exact same app
        if self._foreground_history and self._foreground_history[-1]["app_name"].lower() == app_name.lower():
            if window_title and self._foreground_history[-1].get("window_title") != window_title:
                self._foreground_history[-1]["window_title"] = window_title
            return
        self._foreground_history.append(entry)
        if len(self._foreground_history) > 20:
            self._foreground_history = self._foreground_history[-20:]

    def get_foreground_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return recent foreground history entries, newest last."""
        return list(self._foreground_history[-limit:])

    def get_previous_foreground(self, device_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the immediately preceding foreground window/app from history."""
        if device_id:
            dev_history = [e for e in self._foreground_history if e.get("device_id") == device_id]
            if len(dev_history) >= 2:
                return dev_history[-2]
            elif dev_history:
                return dev_history[-1]
        if len(self._foreground_history) >= 2:
            return self._foreground_history[-2]
        return None

    async def load_from_db(self) -> None:
        """Load persisted signals and events across Hub restart."""
        persisted = await get_world_state_signals()
        for p in persisted:
            sig = Signal(
                key=p["signal_key"],
                source=p["source"],
                timestamp=p["timestamp"],
                confidence=p["confidence"],
                ttl=p["ttl"],
                payload=p["payload"]
            )
            # Only keep non-expired signals on rehydration
            if sig.is_fresh():
                self._signals[sig.key] = sig

        events = await get_recent_context_events(limit=20)
        self._recent_events = events

    async def ingest_signal(
        self,
        signal: Signal,
        record_event: bool = False,
        event_summary: Optional[str] = None
    ) -> bool:
        """
        Accept and fuse a signal into world state.
        Applies conflict resolution, confidence filtering, and freshness checks.
        """
        now = datetime.now(timezone.utc)

        # 1. Stale rejection: Reject signal if already expired upon ingestion
        if not signal.is_fresh(now):
            log.warning(f"[WorldState] Rejected stale signal {signal.key} (expired {signal.ttl}s TTL)")
            return False

        # 2. Conflict resolution: check against existing signal with same key
        existing = self._signals.get(signal.key)
        if existing:
            existing_dt = existing.get_datetime()
            sig_dt = signal.get_datetime()
            # If new signal is strictly older than existing fresh signal, reject
            if sig_dt < existing_dt:
                log.debug(f"[WorldState] Rejected older signal {signal.key} ({sig_dt} < {existing_dt})")
                return False
            # If same timestamp but existing has higher confidence, reject
            if sig_dt == existing_dt and signal.confidence < existing.confidence:
                log.debug(f"[WorldState] Rejected lower confidence signal {signal.key}")
                return False

        # 3. Store in-memory and persist to DB
        self._signals[signal.key] = signal
        if signal.key.startswith("device.foreground:") or signal.key.startswith("screen.foreground_app"):
            app_n = signal.payload.get("app_name") or signal.payload.get("app")
            win_t = signal.payload.get("window_title", "")
            d_id = signal.payload.get("device_id") or (signal.key.split(":")[-1] if ":" in signal.key else None)
            if app_n:
                self.record_foreground_switch(app_n, win_t, device_id=d_id)
        await save_world_state_signal(
            signal_key=signal.key,
            source=signal.source,
            timestamp=signal.timestamp,
            confidence=signal.confidence,
            ttl=signal.ttl,
            payload=signal.payload
        )

        # 4. Record event if requested or significant change detected
        if record_event or event_summary:
            summary = event_summary or f"Updated {signal.key} from {signal.source}"
            ev_id = await add_context_event(
                event_type="signal_update",
                source=signal.source,
                summary=summary,
                details=signal.payload,
                timestamp=signal.timestamp
            )
            self._recent_events.append({
                "id": ev_id,
                "event_type": "signal_update",
                "source": signal.source,
                "timestamp": signal.timestamp,
                "summary": summary,
                "details": signal.payload
            })
            # Bound in-memory history to 20 events
            if len(self._recent_events) > 20:
                self._recent_events = self._recent_events[-20:]

        log.info(f"[WorldState] Ingested signal {signal.key} | source={signal.source} | conf={signal.confidence}")

        # Phase 8B & 8C: Trigger Proactive Engine and Attention Router upon World State change
        try:
            from .proactive_engine import ProactiveEngine
            new_events = await ProactiveEngine.get_instance().evaluate()
            
            # Phase 8C: Route newly created candidate events through AttentionRouter
            from .attention_router import AttentionRouter
            router = AttentionRouter.get_instance()
            for ev in (new_events or []):
                await router.route_event(ev)
            # Reevaluate any deferred delivery decisions on context change
            await router.reevaluate()
        except Exception as e:
            log.warning(f"[WorldState] ProactiveEngine / AttentionRouter error: {e}")

        # Phase 9C: Trigger GoalManager wait condition evaluation on World State change
        try:
            from .goals import GoalManager
            await GoalManager.get_instance().evaluate_wait_conditions()
        except Exception as e:
            log.warning(f"[WorldState] GoalManager evaluate_wait_conditions error: {e}")

        return True

    def get_signal(self, key: str) -> Optional[Signal]:
        """Get a fresh signal by key, or None if missing or expired."""
        sig = self._signals.get(key)
        if sig and sig.is_fresh():
            return sig
        return None

    def set_active_device(self, device_id: str) -> None:
        """Mark the active interacting device and interaction timestamp."""
        self._active_device_id = device_id
        self._last_interaction_at = datetime.now(timezone.utc).isoformat()

    async def get_fused_world_state(self, active_device_id: Optional[str] = None) -> WorldState:
        """
        Assemble the current fused World State.
        Excludes expired signals and resolves cross-signal relations.
        """
        now = datetime.now(timezone.utc)
        dev_id = active_device_id or self._active_device_id

        # 1. Filter out expired signals
        fresh_signals: Dict[str, Signal] = {}
        freshness_meta: Dict[str, Any] = {}
        for k, sig in list(self._signals.items()):
            if sig.is_fresh(now):
                fresh_signals[k] = sig
                freshness_meta[k] = {
                    "source": sig.source,
                    "age_seconds": round((now - sig.get_datetime()).total_seconds(), 1),
                    "remaining_ttl": round(sig.remaining_ttl(now), 1),
                    "confidence": sig.confidence
                }
            else:
                # Clean up expired from active memory
                self._signals.pop(k, None)

        # 2. Location Context
        loc_sig = fresh_signals.get("location.current")
        if loc_sig:
            loc_context = loc_sig.payload
        else:
            loc_context = {"label": "unknown", "status": "no_fresh_signal"}

        # 3. Calendar Context
        cal_sig = fresh_signals.get("calendar.summary")
        if cal_sig:
            cal_context = dict(cal_sig.payload)
            if "connected" not in cal_context:
                cal_context["connected"] = bool(cal_context.get("current_event") or cal_context.get("next_event"))
        else:
            cal_conn_sig = fresh_signals.get("calendar.connected")
            is_conn = cal_conn_sig.payload.get("connected", False) if cal_conn_sig else False
            cal_status = cal_conn_sig.payload.get("status", "disconnected") if cal_conn_sig else "disconnected"
            cur_ev = fresh_signals.get("calendar.current_event")
            nxt_ev = fresh_signals.get("calendar.next_event")
            time_until_sig = fresh_signals.get("calendar.time_until_next_event")
            busy_sig = fresh_signals.get("calendar.busy")
            free_sig = fresh_signals.get("calendar.free_until")
            cal_summary_sig = fresh_signals.get("calendar.summary")

            all_events = []
            if cal_summary_sig and isinstance(cal_summary_sig.payload, dict) and "all_events" in cal_summary_sig.payload:
                all_events = cal_summary_sig.payload.get("all_events", [])
            else:
                if cur_ev and cur_ev.payload.get("title"):
                    all_events.append(cur_ev.payload)
                if nxt_ev and nxt_ev.payload.get("title"):
                    all_events.append(nxt_ev.payload)

            is_busy = busy_sig.payload.get("busy", False) if busy_sig else (bool(cur_ev and cur_ev.payload.get("title")))
            cal_context = {
                "connected": is_conn,
                "status": cal_status,
                "current_event": cur_ev.payload if (cur_ev and cur_ev.payload.get("title")) else None,
                "next_event": nxt_ev.payload if (nxt_ev and nxt_ev.payload.get("title")) else None,
                "time_until_next_event": time_until_sig.payload.get("formatted") if time_until_sig else None,
                "time_until_seconds": time_until_sig.payload.get("seconds") if time_until_sig else None,
                "busy": is_busy,
                "is_busy": is_busy,
                "free_until": free_sig.payload.get("free_until") if free_sig else "unknown",
                "free_busy_window": "unknown (no calendar data)" if not is_conn else "Free for the rest of the day",
                "all_events": all_events
            }

        # 4. Foreground Activity
        fg_payload = {}
        if dev_id:
            fg_sig = fresh_signals.get(f"device.foreground:{dev_id}")
            if fg_sig:
                fg_payload = fg_sig.payload
        if not fg_payload:
            # Check any fresh foreground signal
            for k, s in fresh_signals.items():
                if k.startswith("device.foreground:"):
                    fg_payload = s.payload
                    break
        if not fg_payload and dev_id == get_hub_device_id():
            native_win = await asyncio.to_thread(ActivitySignalAdapter.inspect_windows_foreground)
            if native_win:
                fg_payload = native_win

        foreground_activity = fg_payload or {"app_name": "unknown", "window_title": "unknown"}

        # 5. Current Task
        task_sig = fresh_signals.get("task.active")
        current_task = task_sig.payload if task_sig else None

        # 6. Network Context
        net_payload = {}
        if dev_id:
            net_sig = fresh_signals.get(f"device.network:{dev_id}")
            if net_sig:
                net_payload = net_sig.payload
        if not net_payload and dev_id == get_hub_device_id():
            native_net = await asyncio.to_thread(DeviceSignalAdapter.inspect_network_state)
            if native_net.get("is_connected"):
                net_payload = native_net
        network_context = net_payload or {"network_state": "unknown", "is_connected": False}

        # 7. Battery States
        battery_states: Dict[str, Any] = {}
        for k, s in fresh_signals.items():
            if k.startswith("device.battery:"):
                d_id = s.payload.get("device_id") or k.split(":")[-1]
                battery_states[d_id] = {
                    "battery_level": s.payload.get("battery_level"),
                    "is_charging": s.payload.get("is_charging")
                }
        if dev_id == get_hub_device_id() and dev_id not in battery_states:
            try:
                import psutil
                ps_bat = await asyncio.to_thread(psutil.sensors_battery)
                if ps_bat:
                    battery_states[dev_id] = {
                        "battery_level": int(ps_bat.percent),
                        "is_charging": bool(ps_bat.power_plugged)
                    }
            except Exception:
                pass

        # 8. Connected Devices & Online States
        connected_devs = []
        try:
            import hub.main
            online_map = hub.main.CONNECTED_DEVICES
        except Exception:
            online_map = {}

        all_enrolled = await list_devices()
        for d in all_enrolled:
            d_id = d["device_id"]
            is_online = d_id in online_map
            d_state = {
                "device_id": d_id,
                "device_name": d.get("device_name", d_id),
                "role": d.get("role", "sub_admin"),
                "is_online": is_online,
                "last_seen": d.get("last_seen"),
                "battery": battery_states.get(d_id)
            }
            connected_devs.append(d_state)

        # 9. User Presence
        # Derived from interaction recency and foreground freshness
        user_presence = "unknown"
        if self._last_interaction_at:
            try:
                last_dt = datetime.fromisoformat(self._last_interaction_at)
                diff = (now - last_dt).total_seconds()
                if diff < 120:  # within 2 mins
                    user_presence = "active"
                elif diff < 900:  # within 15 mins
                    user_presence = "idle"
                else:
                    user_presence = "away"
            except Exception:
                pass
        if user_presence == "unknown" and fg_payload and fg_payload.get("app_name") != "unknown":
            user_presence = "active"

        # 10. System Telemetry (Phase 8D)
        target_telem_dev = dev_id or get_hub_device_id()
        system_telemetry: Dict[str, Any] = {}
        for k, s in fresh_signals.items():
            if k.startswith("device.") and k.endswith(f":{target_telem_dev}"):
                prop = k.split(":")[0].replace("device.", "")
                system_telemetry[prop] = s.payload.get("value", s.payload.get(prop))

        if target_telem_dev == get_hub_device_id() and os.name == "nt":
            try:
                live_telem = await asyncio.to_thread(DeviceSignalAdapter.inspect_system_telemetry, target_telem_dev)
                for tk, tv in live_telem.items():
                    if tk not in system_telemetry:
                        system_telemetry[tk] = tv
            except Exception as e:
                log.debug(f"Telemetry inspection fallback error: {e}")

        # 11. Aggregate Confidence
        conf_values = [s.confidence for s in fresh_signals.values()]
        agg_conf = sum(conf_values) / len(conf_values) if conf_values else 0.85

        # 12. Workload Context (Phase 8E)
        workload_context = {
            "total_pending": 0,
            "total_overdue": 0,
            "due_today": [],
            "due_tomorrow": [],
            "next_deadline": None,
            "minutes_due_24h": 0,
            "minutes_due_72h": 0,
            "free_time_before_next_commitment": 240.0,
            "current_pressure": 0.0
        }
        for k, s in fresh_signals.items():
            if k.startswith("workload."):
                prop = k.replace("workload.", "")
                workload_context[prop] = s.payload.get("value", s.payload)

        # 13. Routine Context (Phase 8F)
        routine_context = {
            "current_expected": None,
            "next_expected": None,
            "confidence": 0.0
        }
        for k, s in fresh_signals.items():
            if k.startswith("routine."):
                prop = k.replace("routine.", "")
                routine_context[prop] = s.payload.get("value", s.payload)

        # 14. Screen Perception (Phase A2)
        screen_perception = {}
        screen_fg_sig = (
            fresh_signals.get(f"screen.foreground_app:{dev_id}")
            or fresh_signals.get("screen.foreground_app")
        )
        screen_sum_sig = (
            fresh_signals.get(f"screen.summary:{dev_id}")
            or fresh_signals.get("screen.summary")
        )
        screen_last_sig = (
            fresh_signals.get(f"screen.last_observed_at:{dev_id}")
            or fresh_signals.get("screen.last_observed_at")
        )
        if screen_fg_sig or screen_sum_sig or screen_last_sig:
            screen_perception = {
                "foreground_app": screen_fg_sig.payload.get("app") if screen_fg_sig else None,
                "summary": screen_sum_sig.payload.get("summary") if screen_sum_sig else None,
                "last_observed_at": screen_last_sig.payload.get("captured_at") if screen_last_sig else None,
            }

        return WorldState(
            timestamp=now.isoformat(),
            user_presence=user_presence,
            active_device_id=dev_id,
            connected_devices=connected_devs,
            location_context=loc_context,
            calendar_context=cal_context,
            foreground_activity=foreground_activity,
            current_task=current_task,
            network_context=network_context,
            battery_states=battery_states,
            recent_context_events=self._recent_events[-10:],
            confidence=round(agg_conf, 2),
            freshness_metadata=freshness_meta,
            system_telemetry=system_telemetry,
            workload_context=workload_context,
            routine_context=routine_context,
            screen_perception=screen_perception,
            foreground_history=list(self._foreground_history),
        )

    async def get_compact_world_summary(self, active_device_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Assembles a bounded, compact summary for LLM context injection.
        Omits low-level sensor noise; includes situationally relevant facts.

        Phase 8D: Runtime time context is always injected from OS clock.
        """
        state = await self.get_fused_world_state(active_device_id)
        dev_name = state.active_device_id
        for d in state.connected_devices:
            if d.get("device_id") == state.active_device_id:
                dev_name = d.get("device_name", dev_name)
                break

        # Phase 8D: Always inject authoritative runtime time context from OS.
        # This bypasses any LLM guessing about date/time/timezone.
        time_ctx = RuntimeTimeAdapter.get_runtime_time_context()

        compact = {
            "user_presence": state.user_presence,
            "active_device_id": state.active_device_id,
            "active_device_name": dev_name,
            "location": state.location_context.get("label", "unknown"),
            "foreground_app": state.foreground_activity.get("app_name", "unknown"),
            "previous_app": (self.get_previous_foreground() or {}).get("app_name"),
            "previous_window_title": (self.get_previous_foreground() or {}).get("window_title"),
            # Phase 8D: Runtime temporal grounding — source of truth for dates/times/timezone
            "current_datetime": time_ctx["local_datetime"],
            "current_date": time_ctx["local_date"],
            "current_time": time_ctx["local_time_12h"],
            "day_of_week": time_ctx["day_of_week"],
            "tomorrow_date": time_ctx["tomorrow_date"],
            "tomorrow_name": time_ctx["tomorrow_name"],
            "timezone": time_ctx["timezone"],
            "utc_offset": time_ctx["utc_offset"],
        }

        # Calendar summary
        cal = state.calendar_context
        compact["calendar_connected"] = cal.get("connected", False)
        compact["calendar_current_event"] = cal.get("current_event")
        compact["calendar_next_event"] = cal.get("next_event")
        compact["calendar_time_until_next"] = cal.get("time_until_next_event")

        if cal.get("current_event"):
            compact["calendar_status"] = f"Current meeting: '{cal['current_event']['title']}'"
        elif cal.get("next_event"):
            compact["calendar_status"] = f"Next event: '{cal['next_event']['title']}' in {cal.get('time_until_next_event')}"
        elif cal.get("free_busy_window"):
            compact["calendar_status"] = cal["free_busy_window"]

        # Telemetry summary
        if state.system_telemetry:
            compact["system_telemetry"] = {
                "cpu_percent": state.system_telemetry.get("cpu_percent"),
                "ram_percent": state.system_telemetry.get("ram_percent"),
                "ram_used_gb": state.system_telemetry.get("ram_used_gb"),
                "ram_total_gb": state.system_telemetry.get("ram_total_gb"),
                "disk_free_gb": state.system_telemetry.get("disk_free_gb"),
                "disk_total_gb": state.system_telemetry.get("disk_total_gb"),
                "model": state.system_telemetry.get("model"),
                "manufacturer": state.system_telemetry.get("manufacturer"),
                "processor": state.system_telemetry.get("processor"),
                "gpu": state.system_telemetry.get("gpu")
            }

        # Active Task
        if state.current_task:
            compact["active_task"] = state.current_task.get("title", "")

        # Device status for active device
        if state.active_device_id and state.active_device_id in state.battery_states:
            b = state.battery_states[state.active_device_id]
            charging_str = " (charging)" if b.get("is_charging") else ""
            compact["battery"] = f"{b.get('battery_level')}%{charging_str}"

        if state.network_context.get("network_state") != "unknown":
            compact["network"] = state.network_context.get("network_state")

        # Phase 8E: Workload Context summary
        if state.workload_context:
            compact["workload"] = {
                "total_pending": state.workload_context.get("total_pending", 0),
                "total_overdue": state.workload_context.get("total_overdue", 0),
                "current_pressure": state.workload_context.get("current_pressure", 0.0),
                "next_deadline": state.workload_context.get("next_deadline")
            }

        # Phase 8F: Learned Routine Context summary
        if state.routine_context and (state.routine_context.get("current_expected") or state.routine_context.get("next_expected")):
            compact["routine"] = {
                "current_expected": state.routine_context.get("current_expected"),
                "next_expected": state.routine_context.get("next_expected"),
                "confidence": state.routine_context.get("confidence", 0.0)
            }

        return compact

    async def get_device_state(self, device_id: str) -> Dict[str, Any]:
        """
        Track per-device details:
        - online/offline
        - authenticated
        - battery & charging
        - network
        - telemetry (CPU, RAM, Disk, Hardware specs)
        - last_seen
        - foreground app if available
        - interaction recency
        """
        now = datetime.now(timezone.utc)
        dev = await get_device(device_id)
        if not dev:
            return {"error": f"Device '{device_id}' is not enrolled"}

        try:
            import hub.main
            is_online = device_id in hub.main.CONNECTED_DEVICES
        except Exception:
            is_online = False

        # Battery
        bat_sig = self._signals.get(f"device.battery:{device_id}")
        battery_info = bat_sig.payload if (bat_sig and bat_sig.is_fresh(now)) else None
        if not battery_info and device_id == get_hub_device_id():
            try:
                import psutil
                ps_bat = await asyncio.to_thread(psutil.sensors_battery)
                if ps_bat:
                    battery_info = {
                        "device_id": device_id,
                        "battery_level": int(ps_bat.percent),
                        "is_charging": bool(ps_bat.power_plugged)
                    }
            except Exception:
                pass

        # Network
        net_sig = self._signals.get(f"device.network:{device_id}")
        network_info = net_sig.payload if (net_sig and net_sig.is_fresh(now)) else None
        if not network_info and device_id == get_hub_device_id():
            network_info = await asyncio.to_thread(DeviceSignalAdapter.inspect_network_state)

        # Foreground
        fg_sig = self._signals.get(f"device.foreground:{device_id}")
        fg_info = fg_sig.payload if (fg_sig and fg_sig.is_fresh(now)) else None
        if not fg_info and device_id == get_hub_device_id():
            fg_info = await asyncio.to_thread(ActivitySignalAdapter.inspect_windows_foreground)

        # Telemetry
        telem_info = None
        if self._active_device_id == device_id or device_id == get_hub_device_id():
            telem_info = await asyncio.to_thread(DeviceSignalAdapter.inspect_system_telemetry, device_id)

        return {
            "device_id": device_id,
            "device_name": dev.get("device_name", device_id),
            "role": dev.get("role", "sub_admin"),
            "is_online": is_online,
            "is_authenticated": True,  # Enrolled device in DB
            "battery": battery_info,
            "network": network_info,
            "foreground_activity": fg_info,
            "telemetry": telem_info,
            "last_seen": dev.get("last_seen"),
            "interaction_recency": self._last_interaction_at if self._active_device_id == device_id else None
        }

    async def get_recent_events(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Returns the bounded list of recent context events."""
        if self._recent_events:
            return self._recent_events[-limit:]
        events = await get_recent_context_events(limit=limit)
        return events

    @staticmethod
    def format_world_state_summary(state: WorldState) -> str:
        """Helper to format world state into human-friendly description."""
        parts = []
        if state.active_device_id:
            dev_name = state.active_device_id
            for d in state.connected_devices:
                if d.get("device_id") == state.active_device_id:
                    dev_name = d.get("device_name", dev_name)
                    break
            parts.append(f"Active device: {dev_name}")
        if state.foreground_activity.get("app_name") and state.foreground_activity.get("app_name") != "unknown":
            parts.append(f"Foreground: {state.foreground_activity['app_name']}")
        if state.location_context.get("label") and state.location_context.get("label") != "unknown":
            parts.append(f"Location: {state.location_context['label']}")
        else:
            parts.append("Location: unavailable")
        cal = state.calendar_context
        if cal.get("current_event"):
            parts.append(f"Event: {cal['current_event']['title']}")
        elif cal.get("next_event"):
            parts.append(f"Next: {cal['next_event']['title']} ({cal.get('time_until_next_event')})")
        if state.current_task:
            parts.append(f"Task: {state.current_task.get('title')}")
        if state.active_device_id and state.active_device_id in state.battery_states:
            b = state.battery_states[state.active_device_id]
            parts.append(f"Battery: {b.get('battery_level')}%")
        if state.network_context.get("network_state") and state.network_context.get("network_state") != "unknown":
            parts.append(f"Network: {state.network_context['network_state']}")
        if state.system_telemetry and state.system_telemetry.get("cpu_percent") is not None:
            parts.append(f"CPU: {state.system_telemetry['cpu_percent']}%, RAM: {state.system_telemetry.get('ram_percent', 0)}%")
        if state.user_presence and state.user_presence != "unknown":
            parts.append(f"Presence: {state.user_presence}")
        return ", ".join(parts) if parts else "World state is active with no specific highlights."
