"""Bounded, source-labelled facts for model reasoning, subject to existing privacy policy."""
import json


_SECRET_KEYS = {"password", "secret", "token", "apikey", "authorization", "cookie",
                "credentials", "headers", "imagebase64", "screenshotbase64", "imagedata"}


def bounded_context(value, depth=0):
    if depth > 4:
        return "[omitted: depth limit]"
    if isinstance(value, dict):
        return {str(key): bounded_context(item, depth + 1) for key, item in list(value.items())[:24]
                if str(key).lower().replace("_", "").replace("-", "") not in _SECRET_KEYS}
    if isinstance(value, list):
        return [bounded_context(item, depth + 1) for item in value[:12]]
    if isinstance(value, str):
        return value[:1200]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[omitted: unsupported value]"


def build_reasoning_context(context, permits):
    """Select facts by schema, not by the wording of a particular request.

    `permits` checks each optional section against the actual reasoning provider.
    Denied sections contribute only their name, never their contents.
    """
    result = {"snapshot_only": True}
    omitted = []
    remaining = 12000

    def add(name, value, capability, privacy_class="INTERNAL"):
        nonlocal remaining
        if not value:
            return
        if not permits(capability, value if isinstance(value, dict) else {"items": value}, privacy_class):
            omitted.append(name)
            return
        value = bounded_context(value)
        size = len(json.dumps(value, ensure_ascii=True))
        if size > remaining:
            omitted.append(name)
            return
        result[name] = value
        remaining -= size

    device = context.get("device") or {}
    add("source_device", {k: device[k] for k in ("id", "name", "role") if k in device}, "device.info")
    world = context.get("world_state") or {}
    add("runtime_time", {k: world[k] for k in ("current_datetime", "current_date", "current_time",
        "day_of_week", "tomorrow_date", "tomorrow_name", "timezone", "utc_offset") if k in world}, "time.get")

    session = context.get("session") or {}
    references = session.get("references") or context.get("references") or {}
    reference_keys = ("last_app", "last_closed_app", "last_action", "last_intent", "last_capability",
        "last_action_target", "last_target", "last_confirmed_app", "foreground_app", "current_app",
        "last_grounded_path", "grounded_target", "pending_action")
    add("session_references", {k: references[k] for k in reference_keys if k in references},
        "fs.read", "SENSITIVE")

    memories = []
    for row in (context.get("memory") or [])[:15]:
        if not isinstance(row, dict):
            continue
        selected = {k: row[k] for k in ("key", "value", "category", "confidence") if k in row}
        if permits("notes.list", selected, row.get("privacy_class") or "PERSONAL"):
            memories.append(selected)
        elif "memory" not in omitted:
            omitted.append("memory")
    # The per-row check above preserves stronger annotations; this checks the section budget too.
    add("memory", memories[:8], "notes.list", "PERSONAL")

    add("world_snapshot", {k: world[k] for k in ("active_device_id", "active_device_name",
        "foreground_app", "previous_app", "previous_window_title", "location", "calendar_connected",
        "calendar_current_event", "calendar_next_event", "calendar_time_until_next", "calendar_status")
        if k in world}, "screen.analyze", "SENSITIVE")
    task = context.get("task") or {}
    add("active_task", {k: task[k] for k in ("id", "title", "status") if k in task}, "notes.list", "PERSONAL")
    if omitted:
        result["omitted_sections"] = sorted(set(omitted))
    return result
