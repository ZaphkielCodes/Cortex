"""
hub/permissions.py
------------------
Role-based permission system for Cortex.

Roles
-----
  admin     — Full control. Can target any device, run any command, enroll
              devices, and approve confirmations.
  sub_admin — Limited. Can only target itself, run a restricted set of
              commands, and cannot enroll or approve.

Command Tiers (from cortex_architecture.md § D)
------------------------------------------------
  Tier 1 — Free rein    : read files, screenshots, open apps
  Tier 2 — Ask first    : install app, change setting, move files
  Tier 3 — Always confirm: delete, wipe, remote shutdown, shell commands
"""

from dataclasses import dataclass
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------

ROLE_PERMISSIONS: dict[str, dict] = {
    "primary_admin": {
        "can_target": ["*"],              # can send commands to any device
        "can_execute": ["*"],             # can run any command
        "can_enroll": True,               # can enroll new devices
        "can_confirm": True,              # can approve Tier 2/3 actions
        "max_tier": 3,                    # can initiate up to Tier 3 commands
    },
    "admin": {
        "can_target": ["*"],              # can send commands to any device
        "can_execute": ["*"],             # can run any command
        "can_enroll": True,               # can enroll new devices
        "can_confirm": True,              # can approve Tier 2/3 actions
        "max_tier": 3,                    # can initiate up to Tier 3 commands
    },
    "trusted_worker": {
        "can_target": ["self"],           # can only run commands on itself
        "can_execute": ["*"],
        "can_execute_on_self": ["*"],
        "can_enroll": False,
        "can_confirm": False,
        "max_tier": 2,
    },
    "sub_admin": {
        "can_target": ["self"],           # can only run commands on itself
        "can_execute": [                  # limited command set
            "screenshot.read",
            "screen.capture",
            "screen.analyze",
            "ui.observe",
            "ui.wait",
            "clipboard.read",
            "app.launch",
            "device.status",
            "ui.notify",
            "fs.list",
            "fs.stat",
        ],
        "can_execute_on_self": [          # additional commands allowed on own device
            "screenshot.read",
            "screen.capture",
            "screen.analyze",
            "ui.observe",
            "ui.wait",
            "clipboard.read",
            "app.launch",
            "fs.move",
            "device.status",
            "ui.notify",
            "fs.list",
            "fs.stat",
        ],
        "can_enroll": False,
        "can_confirm": False,
        "max_tier": 1,                    # Tier 1 only (free-rein actions)
    },
}


# ---------------------------------------------------------------------------
# Command → tier mapping
# ---------------------------------------------------------------------------

COMMAND_TIERS: dict[str, int] = {
    # Tier 1 — Free rein
    "device.info": 1,
    "device.status": 1,
    "system.identity": 1,
    "system.models": 1,
    "system.list_capabilities": 1,
    "system.capability_status": 1,
    "system.list_devices": 1,
    "system.device_status": 1,
    "system.list_integrations": 1,
    "system.integration_status": 1,
    "system.providers": 1,
    "system.explain_capability": 1,
    "app.list_running": 1,
    "list_running_apps": 1,
    "process.list": 1,
    "list_processes": 1,
    "app.launch": 1,
    "open_app": 1,
    "app.close": 1,
    "close_app": 1,
    "window.list": 1,
    "list_windows": 1,
    "window.focus": 1,
    "focus_window": 1,
    "switch_window": 1,
    "window.minimize": 1,
    "window.maximize": 1,
    "window.restore": 1,
    "fs.open": 1,
    "open_file": 1,
    "open_folder": 1,
    "browser.open_url": 1,
    "open_url": 1,
    "web.search": 1,
    "web.fetch": 1,
    "weather.current": 1,
    "weather.forecast": 1,
    "ui.notify": 1,
    "screen.capture": 1,
    "screen.analyze": 1,
    "screenshot.read": 1,
    "ui.screenshot": 1,
    "take_screenshot": 1,
    "capture_screen": 1,
    "intent_screenshot": 1,
    # Phase A10: General Computer Use UI Capabilities
    "ui.observe": 1,
    "ui.click": 1,
    "ui.double_click": 1,
    "ui.right_click": 1,
    "ui.move_pointer": 1,
    "ui.scroll": 1,
    "ui.type": 1,
    "ui.keypress": 1,
    "ui.hotkey": 1,
    "ui.drag": 1,
    "ui.select": 1,
    "ui.wait": 1,
    "clipboard.read": 1,
    "clipboard.write": 1,
    "fs.read": 1,
    "fs.list": 1,
    "fs.stat": 1,
    "fs.shortcut_info": 1,
    "hub.read": 1,
    "task.read": 1,
    "session.resume": 1,
    "memory.write": 1,
    "people.search": 1,
    "people.get": 1,
    "people.remember": 1,
    "people.update": 1,
    "speaker.get_current": 1,
    "speaker.resolve": 1,
    "speaker.link_person": 1,
    "speaker.list_recent": 1,
    "social.context": 1,
    "social.remember_interaction": 1,
    "context.get_world_state": 1,
    "context.update_signal": 1,
    "context.get_device_state": 1,
    "context.get_recent_events": 1,
    "proactive.register_condition": 1,
    "proactive.evaluate": 1,
    "proactive.list_candidates": 1,
    "proactive.get_event": 1,
    "proactive.dismiss": 1,
    "attention.route_event": 1,
    "attention.get_decision": 1,
    "attention.list_pending": 1,
    "attention.reevaluate": 1,
    "attention.cancel": 1,
    "calendar.get_current": 1,
    "calendar.get_next": 1,
    "calendar.get_availability": 1,
    "calendar.status": 1,
    "telemetry.get_system_usage": 1,
    "telemetry.get_hardware_info": 1,
    "workload.add_item": 1,
    "workload.get_item": 1,
    "workload.update_item": 1,
    "workload.complete_item": 1,
    "workload.list_items": 1,
    "workload.get_summary": 1,
    "workload.prioritize": 1,
    "goal.create": 1,
    "goal.get": 1,
    "goal.list": 1,
    "goal.cancel": 1,
    "goal.resume": 1,
    "goal.run": 1,
    "goal.wait": 1,
    "goal.list_waits": 1,
    "goal.cancel_wait": 1,
    "goal.pause_all": 1,
    "goal.resume_all": 1,
    "goal.kill_switch": 1,
    "goal.set_autonomy": 1,
    "goal.get_autonomy": 1,
    "goal.plan": 1,
    "runtime.sleep": 1,
    "runtime.status": 1,

    # Tier 2 — Ask first (low stakes but potentially disruptive)
    "fs.copy": 2,
    "copy_file": 2,
    "fs.move": 2,
    "move_file": 2,
    "fs.rename": 2,
    "rename_file": 2,
    "task.write": 2,
    "settings.write": 2,

    # Tier 3 — Always confirm with specifics shown
    "fs.delete": 3,
    "delete_file": 3,
    "kill_app": 3,
    "shell.run": 3,
    "system.shutdown": 3,
    "system.wipe": 3,
}

# Default tier for unknown commands — treat as high-risk
DEFAULT_TIER = 3


# ---------------------------------------------------------------------------
# Permission check result
# ---------------------------------------------------------------------------

@dataclass
class PermissionResult:
    """Result of a permission check."""
    allowed: bool
    needs_confirmation: bool
    tier: int
    reason: str


# ---------------------------------------------------------------------------
# Core check function
# ---------------------------------------------------------------------------

def get_command_tier(command: str) -> int:
    """Return the tier for a command (defaults to Tier 3 for unknown commands)."""
    return COMMAND_TIERS.get(command, DEFAULT_TIER)


from .database import get_device_capabilities, get_device_permission

async def check_permission(
    sender_role: str,
    sender_id: str,
    target_id: str,
    command: str,
) -> PermissionResult:
    """
    Check whether a device with `sender_role` and `sender_id` is allowed
    to execute `command` (capability) on `target_id`.

    Phase 4: Checks dynamic capabilities and per-device overrides (allow/deny/ask).
    """
    perms = ROLE_PERMISSIONS.get(sender_role)
    if perms is None:
        from .device_handle import normalize_role
        perms = ROLE_PERMISSIONS.get(normalize_role(sender_role))
    if perms is None:
        return PermissionResult(
            allowed=False,
            needs_confirmation=False,
            tier=0,
            reason=f"Unknown role: '{sender_role}'",
        )

    tier = get_command_tier(command)

    # --- 1. Target check (RBAC) ---
    can_target = perms["can_target"]
    targeting_self = (sender_id == target_id)

    if "*" not in can_target:
        if "self" in can_target and not targeting_self:
            return PermissionResult(
                allowed=False,
                needs_confirmation=False,
                tier=tier,
                reason=f"Role '{sender_role}' can only target its own device",
            )
        elif "self" not in can_target and target_id not in can_target:
            return PermissionResult(
                allowed=False,
                needs_confirmation=False,
                tier=tier,
                reason=f"Role '{sender_role}' cannot target device '{target_id}'",
            )

    # --- 2. Capability check (Dynamic) ---
    capabilities = await get_device_capabilities(target_id)
    if command not in capabilities:
        return PermissionResult(
            allowed=False,
            needs_confirmation=False,
            tier=tier,
            reason=f"Target device '{target_id}' does not support capability '{command}'",
        )

    # --- 3. Per-device permission override ---
    override = await get_device_permission(target_id, command)
    
    if override == "deny":
        return PermissionResult(
            allowed=False,
            needs_confirmation=False,
            tier=tier,
            reason=f"Capability '{command}' is explicitly denied on device '{target_id}'",
        )
    
    # --- 4. Role Command check (RBAC) ---
    can_execute = perms["can_execute"]
    can_execute_on_self = perms.get("can_execute_on_self", can_execute)
    allowed_commands = can_execute_on_self if targeting_self else can_execute

    if "*" not in allowed_commands and command not in allowed_commands:
        return PermissionResult(
            allowed=False,
            needs_confirmation=False,
            tier=tier,
            reason=f"Role '{sender_role}' cannot execute '{command}'",
        )

    # --- 5. Tier check (RBAC) ---
    max_tier = perms["max_tier"]
    if tier > max_tier:
        return PermissionResult(
            allowed=False,
            needs_confirmation=False,
            tier=tier,
            reason=f"Command '{command}' is Tier {tier}, but role '{sender_role}' is limited to Tier {max_tier}",
        )

    # --- 6. Resolve final confirmation state ---
    if override == "allow":
        needs_confirmation = False
    elif override == "ask":
        needs_confirmation = True
    else:
        # Fallback to tier logic
        needs_confirmation = (tier >= 2)

    return PermissionResult(
        allowed=True,
        needs_confirmation=needs_confirmation,
        tier=tier,
        reason="Allowed" if not needs_confirmation else "Requires confirmation",
    )
