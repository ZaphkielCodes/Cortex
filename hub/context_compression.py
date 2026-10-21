"""Local, deterministic prompt packing. No inferred facts or extra model calls."""
import json
import os


def context_budget(name, default):
    try:
        return max(1000, min(100000, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def byte_size(value):
    return len(compact_json(value).encode("utf-8"))


def excerpt(text, budget):
    """Explicit head/tail excerpt; never pretend truncated text is complete."""
    if len(text) <= budget:
        return text
    marker = "\n[...excerpt: middle omitted...]\n"
    available = max(0, budget - len(marker))
    return text[:available * 2 // 3] + marker + text[-(available - available * 2 // 3):] if available else marker[:budget]


def history_messages(turns):
    """Preserve raw history for privacy classification before any lossy packing."""
    messages = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        if "request" in turn:
            messages.extend([{"role": "user", "text": str(turn["request"])},
                {"role": "assistant", "text": str(turn.get("response_message") or "")}])
        else:
            message = {"role": turn.get("role", "user"),
                "text": str(turn.get("text") or turn.get("content") or "")}
            omitted = turn.get("earlier_messages_omitted")
            if isinstance(omitted, int) and omitted > 0:
                message["earlier_messages_omitted"] = omitted
            messages.append(message)
    return messages


def compress_history(turns, max_chars=None, max_messages=8):
    """Prefer recent turns; preserve roles, order and the immediate follow-up context."""
    max_chars = context_budget("CORTEX_HISTORY_MAX_CHARS", 4000) if max_chars is None else max_chars
    messages = history_messages(turns)
    selected, remaining = [], max_chars
    for message in reversed(messages[-max_messages:]):
        if remaining <= 0:
            break
        # Keep the last exchange at highest fidelity, older messages as excerpts.
        allowance = min(remaining, max_chars * 2 // 3) if not selected else remaining if len(selected) == 1 else min(800, remaining)
        text = excerpt(message["text"], allowance)
        selected.append(dict(message, text=text))
        remaining -= len(text)
    selected.reverse()
    if selected and len(messages) > len(selected):
        selected[0]["earlier_messages_omitted"] = selected[0].get("earlier_messages_omitted", 0) + len(messages) - len(selected)
    return selected


def pack_catalog(entries):
    """Losslessly intern repeated argument definitions and device lists."""
    definitions, devices, rows = [], [], []
    def intern(items, value):
        if value not in items:
            items.append(value)
        return items.index(value)
    for entry in entries:
        rows.append([entry["name"], entry.get("description", ""),
            {name: intern(definitions, spec) for name, spec in entry.get("arguments", {}).items()},
            intern(devices, entry.get("devices", []))])
    return {"argument_definitions": definitions, "device_sets": devices, "rows": rows}


# Keep source, verification and failure metadata before bulk contents.
_PRIORITY = ("status", "observation_id", "element_id", "device_id", "target_device",
             "command_id", "error", "verified", "text_entry_verified", "input_attempted", "provenance", "omitted_items")
_OMITTED = {"omitted": True, "reason": "prompt budget; request fresh focused evidence if needed"}


def budget_evidence(value, max_bytes=10000):
    """Keep complete records where possible; visibly mark omitted payloads.

    This is model context only, never the authoritative action/verification data.
    It does not synthesize an observation, success, authorization or provenance.
    """
    if byte_size(value) <= max_bytes:
        return value
    if isinstance(value, dict):
        result = {}
        keys = [k for k in _PRIORITY if k in value] + [k for k in value if k not in _PRIORITY]
        for key in keys:
            space = max_bytes - byte_size(result) - len(str(key).encode("utf-8")) - 96
            if space <= 128:
                result["_compression"] = _OMITTED
                break
            item = budget_evidence(value[key], space)
            result[key] = item
        return result
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            if byte_size(result + [item]) + 100 > max_bytes:
                result.append({"omitted_items": len(value) - index,
                    "reason": "prompt budget; remaining records not shown"})
                break
            result.append(item)
        return result
    # No partial paths, IDs, text inputs or evidence strings masquerading as facts.
    return _OMITTED


def compress_evidence(evidence, max_bytes=None):
    max_bytes = context_budget("CORTEX_TOOL_CONTEXT_BYTES", 6000) if max_bytes is None else max_bytes
    if not evidence or byte_size(evidence) <= max_bytes:
        return evidence
    # Older observations can be stale; receipts retain action outcomes, not trees.
    receipts = []
    for entry in evidence[:-1]:
        result = entry.get("result", {})
        receipts.append({"capability": entry.get("capability"), "result": {
            k: result[k] for k in ("status", "command_id", "error", "verified", "text_entry_verified") if k in result},
            "evidence_omitted": True})
    receipts = budget_evidence(receipts, max(256, max_bytes // 4))
    receipt_bytes = byte_size(receipts)
    latest = budget_evidence(evidence[-1], max(256, max_bytes - receipt_bytes - 64))
    return receipts + [latest]
