"""Actionable catalogs must not offer legacy declarations without executors."""
import pytest

from hub.capability_registry import CanonicalCapabilityRegistry


@pytest.mark.parametrize("name", ["calendar.get_current", "calendar.get_next", "calendar.get_availability"])
def test_unimplemented_calendar_declarations_remain_inspectable_but_cannot_execute(name):
    registry = CanonicalCapabilityRegistry.get_instance()
    cap = registry.get_capability(name)
    assert cap is not None and not cap.availability
    assert cap.provider_status["reason"] == "unimplemented_legacy_capability"
    assert name in registry.get_all()
    assert name not in {c.name for c in registry.list_capabilities()}
    assert name not in {c.name for c in registry.get_available_capabilities()}
    valid, _ = registry.validate_call(name, {})
    assert not valid
    # Existing service capability keeps its real argument and permission gates.
    calendar = registry.get_capability("calendar.list")
    assert calendar.target_type == "hub" and calendar.permission_tier == 1
    assert {"time_min", "time_max"}.issubset(calendar.required_arguments)
