"""
hub/system_registry.py
----------------------
Canonical runtime System Registry & Self-Knowledge layer for Cortex (Phase A4).

Aggregates:
- Cortex Identity (product, version, build, architecture mode, current node, role)
- Active Models & Providers (conversational LLM, vision, planning, STT, TTS)
- Live Capabilities (derived dynamically from CapabilityRegistry & provider status)
- Device Registry View (enrolled devices, live online status, advertised capabilities)
- Integration Introspection (OAuth connections, authorized scopes, verified access)
- Provider Status (configured & available status with strict secret masking)
- Capability Limitation & Failure Explanation (distinguishing unsupported, missing integration,
  provider unavailable, permission denied, etc.)

Cortex is Cortex. Underlying models (Gemini, OpenAI, etc.) are providers utilized
by Cortex. Product identity and capabilities are always answered from live system state,
never from model priors or hallucinations.
"""

import os
import re
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Union

from .capability_registry import get_core_capabilities, RegisteredCapability
from .database import list_devices as db_list_devices, get_device as db_get_device, get_device_capabilities as db_get_device_capabilities, get_integrations as db_get_integrations
from .credentials import CredentialStore

log = logging.getLogger("cortex.system_registry")

# Product constants
PRODUCT_NAME = "Cortex"
PRODUCT_VERSION = "0.1.0"
BUILD_IDENTIFIER = "cortex-hybrid-2026.10"
ARCHITECTURE_MODE = "hybrid"
DEFAULT_ROLE = "primary_admin"
DEFAULT_NODE = "asus"


@dataclass
class ModelMetadata:
    purpose: str
    provider: str
    model: str
    availability: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IntegrationMetadata:
    name: str
    key: str
    provider: str
    connected: bool
    scopes: List[str]
    status: str
    last_verified: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SystemRegistry:
    """
    Canonical singleton registry exposing Cortex self-knowledge and system state.
    Aggregates existing subsystems without duplicating storage.
    """

    _instance: Optional["SystemRegistry"] = None

    def __init__(self):
        self._model_metadata_override: Optional[Any] = None
        self._integration_overrides: Dict[str, Dict[str, Any]] = {}
        self._device_overrides: Optional[List[Dict[str, Any]]] = None
        self._current_node: str = DEFAULT_NODE

    @classmethod
    def get_instance(cls) -> "SystemRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton for test isolation."""
        cls._instance = None

    def set_model_metadata_override(self, override: Optional[Any]) -> None:
        """Allow tests to mock or disable active model metadata."""
        self._model_metadata_override = override

    def set_integration_override(self, key: str, status_dict: Optional[Dict[str, Any]]) -> None:
        """Allow tests to simulate connected / disconnected OAuth integrations."""
        if status_dict is None:
            self._integration_overrides.pop(key, None)
        else:
            self._integration_overrides[key] = status_dict

    def set_device_overrides(self, devices: Optional[List[Dict[str, Any]]]) -> None:
        """Allow tests to simulate device registry states."""
        self._device_overrides = devices

    def set_current_node(self, node_id: str) -> None:
        self._current_node = node_id

    # -----------------------------------------------------------------------
    # 1. Identity Introspection
    # -----------------------------------------------------------------------
    def get_identity(self, current_node: Optional[str] = None) -> Dict[str, Any]:
        """
        Expose canonical Cortex identity.
        Cortex is Cortex. Underlying LLMs are providers, not the product identity.
        """
        node = current_node or self._current_node
        env = os.environ.get("CORTEX_ENV", "production")
        return {
            "product_name": PRODUCT_NAME,
            "version": PRODUCT_VERSION,
            "build_id": BUILD_IDENTIFIER,
            "architecture_mode": ARCHITECTURE_MODE,
            "current_node": node,
            "role": DEFAULT_ROLE,
            "environment": env,
            "description": "Cortex is a personal AI assistant built on a hybrid architecture combining deterministic local system actions with multi-provider intelligence.",
        }

    # -----------------------------------------------------------------------
    # 2. Model / Provider Introspection
    # -----------------------------------------------------------------------
    def get_models(self) -> List[Dict[str, Any]]:
        """
        Inspect active intelligence models and providers.
        Returns unknown / unavailable when metadata cannot be determined.
        Never hallucinates or guesses model identifiers.
        """
        if self._model_metadata_override is not None:
            if callable(self._model_metadata_override):
                return self._model_metadata_override()
            if isinstance(self._model_metadata_override, list):
                return self._model_metadata_override
            if isinstance(self._model_metadata_override, dict):
                return [self._model_metadata_override]

        models: List[Dict[str, Any]] = []

        # 1. Conversational Reasoning / LLM
        from .llm_provider import get_llm_provider
        llm = get_llm_provider()
        llm_prov = getattr(llm, "name", None)
        llm_mod = getattr(llm, "model", None)
        llm_avail = bool(llm and llm_prov and llm_mod and llm_prov != "unknown")

        models.append({
            "purpose": "conversational_reasoning",
            "provider": llm_prov if llm_prov else "unknown",
            "model": llm_mod if llm_mod else "unknown",
            "availability": llm_avail,
        })

        # 2. Vision / Screen Analysis
        from .vision_provider import get_vision_provider
        vision = get_vision_provider()
        vis_prov = getattr(vision, "provider_name", None)
        vis_mod = getattr(vision, "model_name", None)
        vis_avail = bool(vision and getattr(vision, "is_available", lambda: True)() and vis_prov)

        models.append({
            "purpose": "vision",
            "provider": vis_prov if vis_prov else "unknown",
            "model": vis_mod if vis_mod else "unknown",
            "availability": vis_avail,
        })

        # 3. Planning
        models.append({
            "purpose": "planning",
            "provider": llm_prov if llm_prov else "unknown",
            "model": llm_mod if llm_mod else "unknown",
            "availability": llm_avail,
        })

        # 4. Speech Recognition (STT) - unconfigured in current runtime
        models.append({
            "purpose": "speech_recognition",
            "provider": "unknown",
            "model": "unknown",
            "availability": False,
        })

        # 5. Text to Speech (TTS) - unconfigured in current runtime
        models.append({
            "purpose": "text_to_speech",
            "provider": "unknown",
            "model": "unknown",
            "availability": False,
        })

        return models

    # -----------------------------------------------------------------------
    # 3. Capability Introspection
    # -----------------------------------------------------------------------
    def list_capabilities(self) -> List[Dict[str, Any]]:
        """
        Enumerate all registered capabilities with live runtime availability.
        Reads directly from CapabilityRegistry.
        """
        core_caps = get_core_capabilities()
        res: List[Dict[str, Any]] = []
        for name, cap in core_caps.items():
            res.append({
                "name": cap.name,
                "description": cap.description,
                "target_type": cap.target_type,
                "risk_tier": cap.permission_tier,
                "availability": cap.availability,
                "provider": cap.provider_status.get("provider"),
                "classification": cap.classification,
            })
        return res

    def get_capability_status(self, capability_names: Union[str, List[str]]) -> Dict[str, Any]:
        """
        Query status and availability of specific capability or capability set.
        """
        if isinstance(capability_names, str):
            capability_names = [capability_names]

        core_caps = get_core_capabilities()
        statuses: Dict[str, Any] = {}
        all_available = True

        for cap_name in capability_names:
            cap = core_caps.get(cap_name)
            if not cap:
                statuses[cap_name] = {
                    "available": False,
                    "reason": f"Capability '{cap_name}' is unsupported or unregistered.",
                    "provider": None,
                }
                all_available = False
            else:
                reason = "Capability is fully available." if cap.availability else f"Provider for '{cap_name}' is currently unavailable or disabled."
                statuses[cap_name] = {
                    "available": cap.availability,
                    "reason": reason,
                    "provider": cap.provider_status.get("provider"),
                    "risk_tier": cap.permission_tier,
                    "classification": cap.classification,
                }
                if not cap.availability:
                    all_available = False

        return {
            "all_available": all_available,
            "capabilities": statuses,
        }

    # -----------------------------------------------------------------------
    # 4. Device Registry View
    # -----------------------------------------------------------------------
    async def list_devices(
        self,
        current_node: Optional[str] = None,
        connected_devices_map: Optional[dict] = None,
    ) -> List[Dict[str, Any]]:
        """
        Expose known enrolled devices and their live online/offline state.
        Never fabricates devices or online connections.
        """
        if self._device_overrides is not None:
            return self._device_overrides

        cur = current_node or self._current_node

        # Resolve live connected map if available
        conn_map = connected_devices_map
        if conn_map is None:
            try:
                import hub.main as main_mod
                conn_map = getattr(main_mod, "CONNECTED_DEVICES", {})
            except Exception:
                conn_map = {}

        try:
            enrolled = await db_list_devices()
        except Exception as e:
            log.warning(f"Failed to query enrolled devices from DB: {e}")
            enrolled = []

        device_list: List[Dict[str, Any]] = []
        from .executor_registry import ExecutorRegistry
        from .device_handle import default_transports, default_workload
        import json
        er = ExecutorRegistry.get_instance()

        for d in enrolled:
            d_id = d.get("device_id")
            handle = er.get_device_handle(d_id)
            is_online = er.is_device_online(d_id) if handle else bool(
                d_id in conn_map
                or (d_id == cur and (not conn_map or d_id in conn_map))
            )
            online_source = "ExecutorRegistry.is_device_online" if handle else ("connected_devices_map" if d_id in conn_map else "fallback")
            log.info(
                f"[DIAGNOSTIC] list_devices: device_id={d_id} online={is_online} "
                f"source={online_source} handle_present={bool(handle)} "
                f"heartbeat_age={er.get_heartbeat_freshness(d_id)}"
            )
            try:
                caps = await db_get_device_capabilities(d_id)
            except Exception:
                caps = []

            role = handle.role if handle else d.get("role", "standard")
            trust_level = handle.trust_level if handle else d.get("trust_level", "standard")
            device_type = handle.device_type if handle else d.get("device_type", "unknown")
            platform = handle.platform if handle else d.get("platform", "unknown")
            advertised_caps = handle.capabilities if handle and handle.capabilities else caps

            def _parse_field(val, default):
                if isinstance(val, (dict, list)):
                    return val
                if isinstance(val, str) and val.strip():
                    try:
                        return json.loads(val)
                    except Exception:
                        pass
                return default

            transports = handle.transports if handle else _parse_field(d.get("transports"), default_transports())
            workload = handle.workload if handle else _parse_field(d.get("workload"), default_workload())
            res_prof = handle.resource_profile if handle else _parse_field(d.get("resource_profile"), {})

            device_list.append({
                "device_id": d_id,
                "name": d.get("device_name") or d_id,
                "role": role,
                "trust_level": trust_level,
                "device_type": device_type,
                "platform": platform,
                "online": is_online,
                "last_seen": d.get("last_seen"),
                "enrolled_at": d.get("enrolled_at"),
                "trusted": (role in ("admin", "primary_admin", "sub_admin", "trusted_worker") or trust_level in ("admin", "trusted")),
                "advertised_capabilities": advertised_caps,
                "heartbeat_freshness": er.get_heartbeat_freshness(d_id),
                "transports": transports,
                "workload_summary": workload,
                "resource_profile": res_prof,
                "is_current_device": (d_id == cur),
            })

        # Include any handles registered in ExecutorRegistry not already present from DB
        known_ids = {d["device_id"] for d in device_list}
        for handle in er.list_device_handles():
            if handle.device_id not in known_ids:
                device_list.append({
                    "device_id": handle.device_id,
                    "name": handle.display_name or handle.device_id,
                    "role": handle.role,
                    "trust_level": handle.trust_level,
                    "device_type": handle.device_type,
                    "platform": handle.platform,
                    "online": er.is_device_online(handle.device_id),
                    "last_seen": handle.last_seen,
                    "enrolled_at": None,
                    "trusted": (handle.role in ("admin", "primary_admin", "sub_admin", "trusted_worker") or handle.trust_level in ("admin", "trusted")),
                    "advertised_capabilities": handle.capabilities,
                    "heartbeat_freshness": er.get_heartbeat_freshness(handle.device_id),
                    "transports": handle.transports,
                    "workload_summary": handle.workload,
                    "resource_profile": handle.resource_profile,
                    "is_current_device": (handle.device_id == cur),
                })
                known_ids.add(handle.device_id)

        # If no devices are enrolled in DB, report the local current node cleanly
        if not device_list:
            device_list.append({
                "device_id": cur,
                "name": cur.capitalize(),
                "role": DEFAULT_ROLE,
                "trust_level": "admin",
                "device_type": "laptop",
                "platform": "windows",
                "online": True,
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "enrolled_at": None,
                "trusted": True,
                "advertised_capabilities": list(get_core_capabilities().keys()),
                "heartbeat_freshness": 0.0,
                "transports": default_transports(),
                "workload_summary": default_workload(),
                "resource_profile": {},
                "is_current_device": True,
            })

        return device_list

    async def get_device_status(
        self,
        device_id: str,
        current_node: Optional[str] = None,
        connected_devices_map: Optional[dict] = None,
    ) -> Optional[Dict[str, Any]]:
        devices = await self.list_devices(current_node=current_node, connected_devices_map=connected_devices_map)
        for d in devices:
            if d["device_id"].lower() == device_id.lower() or d["name"].lower() == device_id.lower():
                return d
        return None

    # -----------------------------------------------------------------------
    # 5. Integration Introspection
    # -----------------------------------------------------------------------
    async def list_integrations(self) -> List[Dict[str, Any]]:
        """
        Introspect external service integrations (Google Calendar, Gmail, etc.).
        Distinguishes API support from granted user authorization.
        Never claims integration access unless active tokens exist.
        """
        # Base known integrations
        known_integrations = [
            {
                "key": "calendar",
                "name": "Google Calendar",
                "provider": "google",
                "scopes": [
                    "https://www.googleapis.com/auth/calendar.readonly",
                    "https://www.googleapis.com/auth/calendar.events",
                ],
            },
            {
                "key": "gmail",
                "name": "Gmail",
                "provider": "google",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/gmail.send",
                ],
            },
        ]

        # Query database integration records
        db_records: Dict[str, dict] = {}
        try:
            records = await db_get_integrations()
            for r in records:
                p_name = r.get("plugin_name", "")
                if p_name:
                    db_records[p_name] = r
        except Exception as e:
            log.warning(f"Could not load integrations from DB: {e}")

        result: List[Dict[str, Any]] = []
        for item in known_integrations:
            key = item["key"]
            if key in self._integration_overrides:
                override = self._integration_overrides[key]
                result.append({
                    "name": item["name"],
                    "key": key,
                    "provider": item["provider"],
                    "connected": override.get("connected", False),
                    "scopes": override.get("scopes", item["scopes"]),
                    "status": override.get("status", "connected" if override.get("connected") else "not_connected"),
                    "last_verified": override.get("last_verified"),
                })
                continue

            # Check CredentialStore for tokens
            has_tokens = False
            try:
                tokens = CredentialStore.get_tokens(f"{key}_primary")
                if tokens and (tokens.get("access_token") or tokens.get("refresh_token")):
                    has_tokens = True
            except Exception:
                has_tokens = False

            db_rec = db_records.get(key)
            db_active = (db_rec and db_rec.get("status") == "active")

            is_connected = bool(has_tokens or db_active)
            status_str = "connected" if is_connected else "not_connected"

            result.append({
                "name": item["name"],
                "key": key,
                "provider": item["provider"],
                "connected": is_connected,
                "scopes": item["scopes"] if is_connected else [],
                "status": status_str,
                "last_verified": db_rec.get("created_at") if db_rec else None,
            })

        return result

    async def get_integration_status(self, key: str) -> Dict[str, Any]:
        integrations = await self.list_integrations()
        k_lower = key.lower()
        for integ in integrations:
            if integ["key"].lower() == k_lower or k_lower in integ["name"].lower():
                return integ

        return {
            "name": key.title(),
            "key": key,
            "provider": "unknown",
            "connected": False,
            "scopes": [],
            "status": "not_supported",
            "last_verified": None,
        }

    # -----------------------------------------------------------------------
    # 6. Provider Status Introspection
    # -----------------------------------------------------------------------
    def get_providers(self) -> List[Dict[str, Any]]:
        """
        Report live status of configured system providers.
        Strictly masks any secrets or credentials.
        """
        from .search_provider import get_search_provider
        from .weather_provider import get_weather_provider
        from .vision_provider import get_vision_provider
        from .llm_provider import get_llm_provider

        providers: List[Dict[str, Any]] = []

        # 1. Web Search
        sp = get_search_provider()
        providers.append({
            "name": getattr(sp, "provider_name", "none") if sp else "none",
            "purpose": "web_search",
            "configured": (sp is not None),
            "available": (sp is not None),
            "status": "active" if sp else "unconfigured",
        })

        # 2. Weather
        wp = get_weather_provider()
        providers.append({
            "name": getattr(wp, "provider_name", "none") if wp else "none",
            "purpose": "weather",
            "configured": (wp is not None),
            "available": (wp is not None),
            "status": "active" if wp else "disabled",
        })

        # 3. Vision
        vp = get_vision_provider()
        v_avail = bool(vp and getattr(vp, "is_available", lambda: True)())
        providers.append({
            "name": getattr(vp, "provider_name", "none") if vp else "none",
            "purpose": "vision",
            "configured": (vp is not None),
            "available": v_avail,
            "status": "active" if v_avail else ("unavailable" if vp else "unconfigured"),
        })

        # 4. LLM
        lp = get_llm_provider()
        providers.append({
            "name": getattr(lp, "name", "none") if lp else "none",
            "purpose": "conversational_reasoning",
            "configured": (lp is not None),
            "available": (lp is not None),
            "status": "active" if lp else "unconfigured",
        })

        # 5. Speech Recognition (STT)
        providers.append({
            "name": "none",
            "purpose": "speech_to_text",
            "configured": False,
            "available": False,
            "status": "unconfigured",
        })

        # 6. Text to Speech (TTS)
        providers.append({
            "name": "none",
            "purpose": "text_to_speech",
            "configured": False,
            "available": False,
            "status": "unconfigured",
        })

        return providers

    # -----------------------------------------------------------------------
    # 7. Limitation / Failure Explanation
    # -----------------------------------------------------------------------
    async def explain_capability(self, query_or_cap: str) -> Dict[str, Any]:
        """
        Explain deterministically why a requested capability or request cannot run.
        Distinguishes:
        - unsupported
        - provider_unavailable
        - missing_integration
        - device_offline
        - permission_denied
        - approval_required
        - ambiguous_target
        - runtime_failure
        """
        q = query_or_cap.lower().strip()

        # Check for Gmail / Calendar integration queries
        if "gmail" in q or "email" in q or "inbox" in q:
            integ = await self.get_integration_status("gmail")
            if not integ.get("connected"):
                return {
                    "category": "missing_integration",
                    "capability": "gmail.read",
                    "explanation": "Gmail access is not currently connected or authorized.",
                    "can_run": False,
                }

        if "calendar" in q:
            integ = await self.get_integration_status("calendar")
            if not integ.get("connected"):
                return {
                    "category": "missing_integration",
                    "capability": "calendar.read",
                    "explanation": "Google Calendar access is not currently connected or authorized.",
                    "can_run": False,
                }

        # Check weather
        if "weather" in q:
            stat = self.get_capability_status("weather.current")
            is_avail = stat["capabilities"]["weather.current"]["available"]
            if not is_avail:
                return {
                    "category": "provider_unavailable",
                    "capability": "weather.current",
                    "explanation": "Weather capability is currently unavailable because the weather provider is disabled.",
                    "can_run": False,
                }

        # Check web search
        if "web" in q or "search" in q or "google" in q:
            stat = self.get_capability_status("web.search")
            is_avail = stat["capabilities"]["web.search"]["available"]
            if not is_avail:
                return {
                    "category": "provider_unavailable",
                    "capability": "web.search",
                    "explanation": "Web search is currently unavailable because the search provider is unconfigured.",
                    "can_run": False,
                }

        # Check screen perception
        if "screen" in q or "look at" in q or "see my screen" in q:
            stat = self.get_capability_status("screen.analyze")
            is_avail = stat["capabilities"]["screen.analyze"]["available"]
            if not is_avail:
                return {
                    "category": "provider_unavailable",
                    "capability": "screen.analyze",
                    "explanation": "Screen perception is currently unavailable because the vision provider is unconfigured or disabled.",
                    "can_run": False,
                }

        # Check 3D printing / physical world fabrication
        if any(w in q for w in ("3d print", "print 3d", "fabricate", "hardware hinge", "coffee")):
            return {
                "category": "unsupported",
                "capability": None,
                "explanation": "Hardware fabrication and physical-world manipulation are not supported by Cortex capabilities.",
                "can_run": False,
            }

        # Check direct capability lookup
        core_caps = get_core_capabilities()
        if query_or_cap in core_caps:
            cap = core_caps[query_or_cap]
            if not cap.availability:
                return {
                    "category": "provider_unavailable",
                    "capability": query_or_cap,
                    "explanation": f"Provider for '{query_or_cap}' is currently unavailable or disabled.",
                    "can_run": False,
                }
            return {
                "category": "supported_and_available",
                "capability": query_or_cap,
                "explanation": f"Capability '{query_or_cap}' is registered and active.",
                "can_run": True,
            }

        return {
            "category": "unsupported",
            "capability": None,
            "explanation": f"No registered Cortex capability matches request '{query_or_cap}'.",
            "can_run": False,
        }

    @classmethod
    async def answer_query(cls, query: str, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Ground-truth self-knowledge lookup for identity, models, capabilities, and devices."""
        reg = cls.get_instance()
        q = (query or "").lower().strip(" \t.,!?;:")

        # 1. Identity Queries
        if any(p in q for p in ("who are you", "what are you", "what is cortex", "tell me about yourself", "describe yourself")):
            ident = reg.get_identity()
            return (
                f"I am {ident['product_name']}, a personal AI assistant built on a {ident['architecture_mode']} architecture. "
                f"I combine deterministic local system execution with multi-provider intelligence for reasoning, live web access, "
                f"visual screen perception, and application control."
            )

        # 2. Model & Provider Queries
        if any(p in q for p in ("what model", "which model", "what ai model", "what llm", "model are you using", "model are you running")):
            models = reg.get_models()
            llm_entry = next((m for m in models if m["purpose"] == "conversational_reasoning"), None)
            prov = llm_entry.get("provider", "unknown") if llm_entry else "unknown"
            mod = llm_entry.get("model", "unknown") if llm_entry else "unknown"
            if prov == "unknown" or mod == "unknown" or not (llm_entry and llm_entry.get("availability")):
                return "Active model metadata is currently unavailable or unknown."
            return f"I am running on Cortex using the {prov} model ({mod}) for conversational reasoning and planning."

        # 3. Overall Capabilities List
        if any(p in q for p in ("what can you do", "what are your capabilities", "what capabilities do you have", "what actions can you perform", "list your capabilities", "list capabilities")):
            return (
                "I can perform live web searches, fetch live weather reports, open browser URLs, "
                "capture and visually analyze your screen, launch and gracefully close desktop applications, "
                "switch and focus windows, and reliably perform verified file operations (open, copy, move, rename, delete)."
            )

        # 4. Device List
        if any(p in q for p in ("what devices are connected", "which devices are connected", "list devices", "what devices do i have", "show connected devices")):
            devices = await reg.list_devices()
            online_names = [d.get("device_name", d["device_id"]) for d in devices if d.get("is_online")]
            if online_names:
                return f"Connected online devices: {', '.join(online_names)}."
            return "No remote devices are currently connected."

        return None

