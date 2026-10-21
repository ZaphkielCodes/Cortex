"""App-independent recovery evidence and trusted observation/target binding."""
from datetime import datetime, timezone
import hashlib
import json


RECOVERY_LIMITS = {"NO_PROGRESS": 3, "TARGET_CONTEXT_MISMATCH": 3, "TARGET_DISAPPEARED": 2,
                   "CAPABILITY_REJECTED": 3, "ARGUMENT_SCHEMA_REJECTED": 3,
                   "GROUNDING_AMBIGUOUS": 3, "VERIFICATION_FAILED": 3}


class RecoveryExhausted(ValueError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def process_key(value):
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1].casefold().removesuffix(".exe")


def observation_identity(observation):
    provenance = observation.get("provenance") or {}
    identity = provenance.get("window_identity") or {}
    return {"device_id": observation.get("device_id"), "window_id": observation.get("window_id"),
            "pid": observation.get("pid") or identity.get("pid"),
            "process_name": observation.get("process_name") or observation.get("foreground_app"),
            "app_name": observation.get("foreground_app"),
            "observation_id": observation.get("observation_id"), "timestamp": observation.get("timestamp")}


def semantic_ui_hash(observation):
    # IDs, clocks, focus, bounds and animation are not proof of semantic progress.
    elements = [{"role": e.get("role"), "label": e.get("label"), "text": e.get("text"),
                 "enabled": e.get("enabled"), "identity": (e.get("metadata") or {}).get("automation_id"),
                 "state": {key: e.get("metadata", {}).get(key) for key in ("selected", "toggle_state", "expand_state")}}
                for e in observation.get("elements", [])]
    return hashlib.sha256(json.dumps(elements, sort_keys=True, default=str).encode()).hexdigest()[:24]


def bind_receipt(data, device, *, previous=None):
    previous = previous or {}
    return {"device_id": device, "window_id": data.get("window_id"),
            "pid": data.get("pid"),
            "process_name": data.get("process_name") or data.get("foreground_app") or previous.get("process_name"),
            "app_name": data.get("app_name") or previous.get("app_name"),
            "host_pid": data.get("host_pid")}


def observation_problem(observation, expected, device, *, require_window=False):
    identity = observation_identity(observation)
    if (require_window or identity["device_id"] is not None) and identity["device_id"] != device:
        return "TARGET_CONTEXT_MISMATCH"
    # Metadata-only legacy results cannot ground UI and remain usable for app queries.
    if not identity["window_id"] or not identity["observation_id"]:
        return "TARGET_DISAPPEARED" if expected or require_window else None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(identity["timestamp"])).total_seconds()
        if not 0 <= age <= 30:
            return "VERIFICATION_FAILED"
    except (TypeError, ValueError):
        return "VERIFICATION_FAILED"
    if observation.get("provenance", {}).get("identity_consistent") is False:
        return "TARGET_CONTEXT_MISMATCH"
    if expected:
        if expected.get("window_id") and identity["window_id"] != expected["window_id"]:
            return "TARGET_CONTEXT_MISMATCH"
        ownership = (observation.get("provenance") or {}).get("window_identity") or {}
        hosted_upgrade = (ownership.get("identity_source") == "hosted_content_process" and
            ownership.get("host_pid") == expected.get("pid") and expected.get("window_id") == identity["window_id"] and
            process_key(ownership.get("host_app")) == process_key(expected.get("process_name")))
        if expected.get("pid") and identity["pid"] != expected["pid"] and not hosted_upgrade:
            return "TARGET_CONTEXT_MISMATCH"
        if expected.get("process_name") and process_key(identity["process_name"]) != process_key(expected["process_name"]) and not hosted_upgrade:
            return "TARGET_CONTEXT_MISMATCH"
    return None


def reacquisition_arguments(expected, *, by_process=False):
    if not expected:
        return None
    if not by_process and expected.get("window_id"):
        result = {"window_id": expected["window_id"]}
        if expected.get("pid"):
            result["pid"] = expected["pid"]
        return result
    if expected.get("process_name"):
        return {"process": expected["process_name"]}
    if expected.get("pid"):
        return {"pid": expected["pid"]}
    return None
