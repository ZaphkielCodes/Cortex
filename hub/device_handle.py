"""
hub/device_handle.py
--------------------
Canonical persistent and live DeviceHandle model for Phase A5:
Distributed Device Handles & Executor Registry.

A device is an execution node with:
- identity
- trust
- permissions
- capabilities
- state
- connectivity
- workload
- transports

ASUS is role = primary_admin, NOT Cortex itself.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional, Set

VALID_DEVICE_TYPES: Set[str] = {
    "desktop",
    "laptop",
    "phone",
    "tablet",
    "server",
    "cloud_worker",
    "unknown",
}

VALID_DEVICE_ROLES: Set[str] = {
    "primary_admin",
    "admin",
    "trusted_worker",
    "standard",
    "restricted",
}

VALID_TRUST_LEVELS: Set[str] = {
    "admin",
    "trusted",
    "standard",
    "restricted",
}

VALID_TRANSPORTS: Set[str] = {
    "websocket",
    "lan",
    "ble",
    "wifi_direct",
    "cloud_relay",
}


def normalize_device_type(dev_type: Optional[str]) -> str:
    if not dev_type:
        return "unknown"
    dt = dev_type.strip().lower()
    return dt if dt in VALID_DEVICE_TYPES else "unknown"


def normalize_role(role: Optional[str]) -> str:
    if not role:
        return "standard"
    r = role.strip().lower()
    if r in ("sub_admin", "trusted"):
        return "trusted_worker"
    return r if r in VALID_DEVICE_ROLES else "standard"


def normalize_trust_level(trust_level: Optional[str], role: Optional[str] = None) -> str:
    if trust_level:
        tl = trust_level.strip().lower()
        if tl in VALID_TRUST_LEVELS:
            return tl
    # Derive conservative default from role if trust_level not explicitly specified
    r = normalize_role(role)
    if r in ("primary_admin", "admin"):
        return "admin"
    if r == "trusted_worker":
        return "trusted"
    if r == "restricted":
        return "restricted"
    return "standard"


def default_transports() -> Dict[str, bool]:
    return {
        "websocket": True,
        "lan": True,
        "ble": False,
        "wifi_direct": False,
        "cloud_relay": False,
    }


def default_workload() -> Dict[str, Any]:
    return {
        "active_tasks": [],
        "active_task_count": 0,
    }


@dataclass
class DeviceHandle:
    """Canonical model for execution nodes across distributed Cortex."""
    device_id: str
    display_name: str
    device_type: str = "unknown"               # desktop, laptop, phone, tablet, server, cloud_worker, unknown
    platform: str = "unknown"                  # windows, linux, macos, android, ios, unknown
    role: str = "standard"                     # primary_admin, admin, trusted_worker, standard, restricted
    trust_level: str = "standard"              # admin, trusted, standard, restricted
    enrolled: bool = True
    authenticated: bool = False
    online: bool = False
    last_seen: Optional[str] = None            # ISO-8601 UTC
    last_heartbeat: Optional[str] = None       # ISO-8601 UTC
    agent_version: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    transports: Dict[str, bool] = field(default_factory=default_transports)
    resource_profile: Dict[str, Any] = field(default_factory=dict)
    workload: Dict[str, Any] = field(default_factory=default_workload)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Phase A7: Executor Privacy Profiles
    locality: str = "local_device"             # local_device, trusted_lan_device, private_server, cloud_worker, third_party_service
    execution_environment: str = "on_premise"  # on_premise, secure_enclave, cloud_hosted, untrusted_runtime
    private_compute_allowed: bool = True
    cloud_processing_allowed: bool = False
    data_retention_class: str = "ephemeral"    # ephemeral, session_only, persistent
    user_approved_scopes: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.device_type = normalize_device_type(self.device_type)
        self.role = normalize_role(self.role)
        self.trust_level = normalize_trust_level(self.trust_level, self.role)
        if not self.transports:
            self.transports = default_transports()
        if not self.workload:
            self.workload = default_workload()

        # Phase A7 Privacy Profile derivation
        if self.device_type == "cloud_worker":
            if self.locality == "local_device":
                self.locality = "cloud_worker"
            if self.execution_environment == "on_premise":
                self.execution_environment = "cloud_hosted"
            self.cloud_processing_allowed = True
            # Cloud worker by default does NOT have private compute allowed unless explicitly enabled
            if self.trust_level not in ("admin", "trusted"):
                self.private_compute_allowed = False
        elif self.role == "restricted" or self.trust_level == "restricted":
            self.private_compute_allowed = False
        elif self.role in ("trusted_worker", "admin"):
            if self.locality == "local_device":
                self.locality = "trusted_lan_device"

    def supports_capability(self, capability: str) -> bool:
        """Check whether this device advertises a specific capability."""
        discovered = self.metadata.get('capability_discovery')
        if discovered:
            import time
            entry = discovered['entries'].get(capability)
            if time.monotonic() >= discovered['expires_mono'] or not entry or not entry['compatible'] or entry['advertisement']['availability'] == 'unavailable':
                return False
        if not capability or not self.capabilities:
            return False
        if capability in self.capabilities:
            return True
        # Check wildcard matching if any, e.g. fs.*
        if capability.startswith("fs.") and ("fs.*" in self.capabilities or "fs" in self.capabilities):
            return True
        if capability.startswith("app.") and ("app.*" in self.capabilities or "app" in self.capabilities):
            return True
        return False

    def is_authorized_for(self, capability: str, tier: int = 1, classification: str = "read") -> bool:
        """
        Enforce trust constraints.
        Restricted devices may NOT execute destructive actions (tier >= 3 or classification == 'destructive')
        even if they advertise the technical capability.
        """
        if self.trust_level == "restricted":
            # Restricted devices only permitted read/telemetry / low risk (tier 1)
            if tier >= 3 or classification == "destructive":
                return False
            if capability.startswith("fs.delete") or capability.startswith("fs.move") or capability.startswith("fs.rename"):
                return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        """Convert handle to JSON-serializable dictionary."""
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceHandle":
        """Construct handle from dictionary."""
        filtered = dict(data)
        # Ensure collections are mutable lists / dicts
        caps = list(filtered.get("capabilities") or [])
        transports = dict(filtered.get("transports") or default_transports())
        resource = dict(filtered.get("resource_profile") or {})
        workload = dict(filtered.get("workload") or default_workload())
        meta = dict(filtered.get("metadata") or {})

        return cls(
            device_id=filtered.get("device_id", ""),
            display_name=filtered.get("display_name") or filtered.get("device_name") or filtered.get("device_id", ""),
            device_type=normalize_device_type(filtered.get("device_type")),
            platform=filtered.get("platform", "unknown"),
            role=normalize_role(filtered.get("role")),
            trust_level=normalize_trust_level(filtered.get("trust_level"), filtered.get("role")),
            enrolled=bool(filtered.get("enrolled", True)),
            authenticated=bool(filtered.get("authenticated", False)),
            online=bool(filtered.get("online", False)),
            last_seen=filtered.get("last_seen"),
            last_heartbeat=filtered.get("last_heartbeat"),
            agent_version=filtered.get("agent_version"),
            capabilities=caps,
            transports=transports,
            resource_profile=resource,
            workload=workload,
            metadata=meta,
            locality=filtered.get("locality") or meta.get("locality", "local_device"),
            execution_environment=filtered.get("execution_environment") or meta.get("execution_environment", "on_premise"),
            private_compute_allowed=bool(filtered.get("private_compute_allowed", meta.get("private_compute_allowed", True))),
            cloud_processing_allowed=bool(filtered.get("cloud_processing_allowed", meta.get("cloud_processing_allowed", False))),
            data_retention_class=filtered.get("data_retention_class") or meta.get("data_retention_class", "ephemeral"),
            user_approved_scopes=list(filtered.get("user_approved_scopes") or meta.get("user_approved_scopes", [])),
        )

    @classmethod
    def from_db_row(
        cls,
        row: Dict[str, Any],
        live_online: bool = False,
        live_capabilities: Optional[List[str]] = None,
    ) -> "DeviceHandle":
        """Construct handle from database row with live runtime overlays."""
        def parse_json_field(val: Any, default_val: Any) -> Any:
            if isinstance(val, (dict, list)):
                return val
            if isinstance(val, str) and val.strip():
                try:
                    return json.loads(val)
                except Exception:
                    pass
            return default_val

        transports = parse_json_field(row.get("transports"), default_transports())
        resource = parse_json_field(row.get("resource_profile"), {})
        workload = parse_json_field(row.get("workload"), default_workload())
        meta = parse_json_field(row.get("metadata"), {})

        # Populate telemetry into resource profile if available from columns
        if "battery_level" in row and row["battery_level"] is not None:
            resource["battery_level"] = row["battery_level"]
        if "is_charging" in row and row["is_charging"] is not None:
            resource["is_charging"] = bool(row["is_charging"])

        role = normalize_role(row.get("role"))
        trust_level = normalize_trust_level(row.get("trust_level"), role)

        caps = live_capabilities if live_capabilities is not None else []

        return cls(
            device_id=row["device_id"],
            display_name=row.get("device_name") or row["device_id"],
            device_type=normalize_device_type(row.get("device_type")),
            platform=row.get("platform", "unknown"),
            role=role,
            trust_level=trust_level,
            enrolled=True,
            authenticated=live_online,
            online=live_online,
            last_seen=row.get("last_seen"),
            last_heartbeat=row.get("last_heartbeat"),
            agent_version=row.get("agent_version"),
            capabilities=caps,
            transports=transports,
            resource_profile=resource,
            workload=workload,
            metadata=meta,
            locality=row.get("locality") or meta.get("locality", "local_device"),
            execution_environment=row.get("execution_environment") or meta.get("execution_environment", "on_premise"),
            private_compute_allowed=bool(row.get("private_compute_allowed", meta.get("private_compute_allowed", True))),
            cloud_processing_allowed=bool(row.get("cloud_processing_allowed", meta.get("cloud_processing_allowed", False))),
            data_retention_class=row.get("data_retention_class") or meta.get("data_retention_class", "ephemeral"),
            user_approved_scopes=list(parse_json_field(row.get("user_approved_scopes") or meta.get("user_approved_scopes"), [])),
        )
