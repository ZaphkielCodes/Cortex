"""Advertise actual runtime handlers; no device-name profiles or model proposals."""
from shared.discovery import AdvertisementBatch, describe


def advertisement(device_id, generation=0):
    from .executor import get_capabilities, HANDLERS
    from hub.capability_registry import CanonicalCapabilityRegistry
    registry = CanonicalCapabilityRegistry.get_instance()
    records = []
    for name in sorted(set(get_capabilities())):
        cap = registry.get_capability(name)
        if cap is None or cap.target_type != 'device': continue
        records.append(describe(cap, 'handler_registered' if name in HANDLERS else 'session_handler'))
    return AdvertisementBatch(device_id=device_id, generation=generation, capabilities=records)
