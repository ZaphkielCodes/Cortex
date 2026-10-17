"""Context reaches the model through real context/session/db code; model output is simulated."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from hub.agent_turn import AgentTurn
from hub.intent_schema import ModelResult
from hub.reasoning_context import build_reasoning_context, bounded_context


class RecordingModel:
    locality = "local_device"
    name = "context_contract_probe"

    def __init__(self):
        self.calls = []

    async def generate_result(self, text, context, **kwargs):
        self.calls.append(context)
        return ModelResult(type="RESPONSE", response_text=json.dumps({
            "decision": "RESPOND", "response_text": "Context contract response."}))


def state(model):
    return json.loads(model.calls[-1]["system_prompt"].split("\nSTATE:\n", 1)[1])


def test_context_is_bounded_and_does_not_copy_unknown_or_secret_fields():
    context = {"device": {"id": "source", "name": "Laptop", "secret": "do-not-send"},
        "session": {"references": {"last_app": "Editor", "password": "do-not-send",
            "grounded_target": {"matched_path": "C:/sample", "api_key": "do-not-send"}}},
        "world_state": {"current_date": "2026-10-16", "screenshot_base64": "do-not-send"},
        "memory": [{"key": "long", "value": "x" * 100000, "category": "preference"}] * 100,
        "credentials": "do-not-send"}
    packed = build_reasoning_context(context, lambda *args: True)
    encoded = json.dumps(packed)
    assert len(encoded) < 13000
    assert "do-not-send" not in encoded
    assert packed["snapshot_only"] is True
    assert packed["session_references"]["last_app"] == "Editor"
    assert packed["runtime_time"]["current_date"] == "2026-10-16"
    assert len(bounded_context("x" * 100000)) == 1200


@pytest.mark.asyncio
async def test_saved_preferences_references_and_time_reach_model_after_reload():
    from hub.context import ContextManager
    from hub.database import add_memory_item
    from hub.session_context import SessionContextManager
    await add_memory_item({"id": "preference", "category": "preference", "key": "answer_style",
        "value": "Use concise answers", "confidence": 1, "source": "user"})
    await SessionContextManager.update_session("source", "Explain parsers", {"status": "success", "message": "Earlier answer"})
    await SessionContextManager.set_foreground_app("source", "Editor")
    SessionContextManager._memory_cache.clear()
    with patch("hub.world_state.WorldStateManager.get_compact_world_summary", AsyncMock(return_value={
        "active_device_id": "source", "foreground_app": "Editor", "current_date": "2026-10-16", "timezone": "America/New_York"})):
        context = await ContextManager.get_active_context("source", "Make that shorter")
    model = RecordingModel()
    result = await AgentTurn(SimpleNamespace(connected_devices={}), model).run("Make that shorter", "source", context)
    assert result.status == "success"
    packed = state(model)["context"]
    assert packed["memory"][0]["key"] == "answer_style"  # No lexical overlap required for preferences.
    assert packed["session_references"]["foreground_app"] == "Editor"
    assert packed["runtime_time"]["current_date"] == "2026-10-16"
    assert model.calls[0]["conversation_history"][0]["text"] == "Explain parsers"
    assert "snapshots may be stale" in model.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_cloud_model_does_not_receive_private_optional_context():
    model = RecordingModel()
    model.locality = "cloud_worker"
    context = {"memory": [{"key": "private", "value": "private-note-content", "category": "note"}],
        "session": {"references": {"last_grounded_path": "C:/private-folder"}},
        "world_state": {"current_date": "2026-10-16", "calendar_next_event": {"title": "private-appointment"}}}
    result = await AgentTurn(SimpleNamespace(connected_devices={}), model).run("Hello", "source", context)
    assert result.status == "success"
    packed = state(model)["context"]
    assert packed["runtime_time"]["current_date"] == "2026-10-16"
    assert set(packed["omitted_sections"]) == {"memory", "session_references", "world_snapshot"}
    assert "private-note-content" not in model.calls[0]["system_prompt"]
    assert "private-folder" not in model.calls[0]["system_prompt"]
    assert "private-appointment" not in model.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_stronger_row_annotation_is_preserved_by_privacy_gate():
    model = RecordingModel()
    model.locality = "cloud_worker"
    from hub.privacy import PrivacyPolicyEngine
    policy = PrivacyPolicyEngine.get_instance().policy
    policy.personal_data_trusted_devices_only = False
    context = {"memory": [
        {"key": "style", "value": "brief", "category": "preference"},
        {"key": "confidential", "value": "private-record", "category": "note", "privacy_class": "HIGHLY_SENSITIVE"},
        {"key": "unknown", "value": "unclassified-record", "category": "note", "privacy_class": "INVALID"}]}
    await AgentTurn(SimpleNamespace(connected_devices={}), model).run("Hello", "source", context)
    packed = state(model)["context"]
    assert [m["key"] for m in packed["memory"]] == ["style"]
    assert "private-record" not in model.calls[0]["system_prompt"]
    assert "unclassified-record" not in model.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_source_context_never_becomes_selected_targets_live_evidence():
    model = RecordingModel()
    context = {"requested_device": "worker", "device": {"id": "source", "name": "Laptop"},
        "world_state": {"active_device_id": "source", "foreground_app": "Editor"}}
    await AgentTurn(SimpleNamespace(connected_devices={}), model).run("What was I using?", "source", context)
    sent = state(model)
    assert sent["requested_device"] == "worker"
    assert sent["context"]["world_snapshot"]["active_device_id"] == "source"
    assert sent["context"]["source_device"]["id"] == "source"
    assert sent["tool_results"] == []
