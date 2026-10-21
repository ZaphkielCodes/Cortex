"""Authenticated, structured runtime facts. No natural-language interpretation."""
async def runtime_snapshot(source_device):
    from .database import get_device, list_task_executions, list_transfer_sessions, get_tasks
    from .executor_registry import ExecutorRegistry
    from .privacy import PrivacyPolicyEngine
    from dataclasses import asdict
    from .system_registry import SystemRegistry
    device = await get_device(source_device)
    handle = ExecutorRegistry.get_instance().get_device_handle(source_device)
    if not device and not handle:
        return {"status": "denied", "message": "The requesting device is not enrolled."}
    role = device.get("role") if device else handle.role
    admin = role in {"admin", "primary_admin"}
    executions = await list_task_executions()
    executions = [row for row in executions if admin or source_device in (
        row.get("owner_device_id"), row.get("executor_device_id"))]
    execution_keys = ("task_execution_id", "goal_id", "owner_device_id", "executor_device_id", "generation",
        "execution_state", "required_capabilities", "required_resources", "required_data_locations", "metadata")
    transfers = await list_transfer_sessions()
    transfers = [row for row in transfers if admin or source_device in (
        row.get("source_device_id"), row.get("destination_device_id"))]
    transfer_keys = ("transfer_id", "source_device_id", "destination_device_id", "source_path", "destination_path",
        "state", "transferred_bytes", "total_bytes", "selected_transport", "last_error")
    tasks = [row for row in await get_tasks() if admin or row.get("assigned_device") == source_device]
    return {"status": "success", "message": "Current runtime state retrieved.", "data": {
        "source_device": source_device,
        "devices": [row for row in await SystemRegistry.get_instance().list_devices() if admin or row.get("device_id")==source_device],
        "tasks": [{k: row.get(k) for k in ("id", "title", "status", "assigned_device", "last_error")} for row in tasks[:20]],
        "executions": [{k: row.get(k) for k in execution_keys} for row in executions[:20]],
        "transfers": [{k: row.get(k) for k in transfer_keys} for row in transfers[:20]],
        "privacy_policy": asdict(PrivacyPolicyEngine.get_instance().policy),
        "automatic_action_replay": False}}
