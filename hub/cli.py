"""
hub/cli.py
----------
Cortex Hub CLI — interactive terminal for sending commands to connected agents (Phase 3).

How it works
------------
- Connects to the hub's /ws-cli WebSocket channel.
- Authenticates with device_id + secret (Phase 3 requirement).
- Receives live events (device_connected, device_disconnected, result,
  confirmation_request, etc.) on a background asyncio task.
- The foreground loop reads your input, parses it, and sends a JSON command
  to the hub which then forwards it to the target agent.
- Reconnects automatically with exponential backoff if the connection drops.

Usage
-----
    python cli.py --hub ws://localhost:8000 --device asus

    # Secret is loaded automatically from ~/.cortex/cli_secret
    # Override with --secret only for testing:
    python cli.py --hub ws://localhost:8000 --device asus --secret <hex>

Supported commands (typed at the prompt)
-----------------------------------------
    take_screenshot
    open_app <app_name>
    move_file                  ← will prompt for src and dst separately

Meta commands
-------------
    devices    — list currently connected devices (with roles)
    logs       — print the 20 most recent action logs from the hub
    bootstrap  — first-run: create admin device
    enroll     — register a new device (requires admin secret)
    rotate     — rotate a device's secret (admin only)
    pending    — list pending confirmations
    approve <id> — approve a pending confirmation
    deny <id>    — deny a pending confirmation
    help       — show this help
    exit / quit
"""

import asyncio
import json
import os
import sys
import argparse
from pathlib import Path
from typing import Optional

try:
    import websockets
except ImportError:
    print("ERROR: 'websockets' package not found. Run: pip install websockets")
    sys.exit(1)

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

VALID_COMMANDS = {"take_screenshot", "open_app", "move_file"}

# CLI secret storage location
CLI_SECRET_DIR = Path.home() / ".cortex"
CLI_SECRET_FILE = CLI_SECRET_DIR / "device_secret"

# Reconnection constants
INITIAL_BACKOFF = 2
MAX_BACKOFF = 60
AUTH_TIMEOUT = 10

# ANSI colours — work on Windows 10+ with ENABLE_VIRTUAL_TERMINAL_PROCESSING
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_YELLOW = "\033[93m"
_DIM    = "\033[2m"
_MAGENTA = "\033[95m"


# ---------------------------------------------------------------------------
# Secret loading
# ---------------------------------------------------------------------------
def _load_cli_secret(cli_secret_arg: str | None) -> str:
    """Load CLI secret from --secret flag, env var, or ~/.cortex/cli_secret.

    Priority:
      1. --secret <hex>  (explicit testing override)
      2. CORTEX_SECRET environment variable
      3. ~/.cortex/device_secret file  (default for normal operation)
    """
    # 1. CLI argument
    if cli_secret_arg:
        return cli_secret_arg.strip()

    # 2. Environment variable
    env_secret = os.environ.get("CORTEX_SECRET", "").strip()
    if env_secret:
        return env_secret

    # 3. Protected local storage
    if CLI_SECRET_FILE.exists():
        content = CLI_SECRET_FILE.read_text().strip()
        if content:
            return content

    print(
        f"{_RED}ERROR: No device secret found.{_RESET}\n"
        f"  Store your secret in: {_YELLOW}{CLI_SECRET_FILE}{_RESET}\n"
        f"  Or pass --secret <hex> for testing.\n"
        f"\n"
        f"  To create the secret file:\n"
        f"    mkdir {CLI_SECRET_DIR}\n"
        f"    echo YOUR_SECRET_HEX > {CLI_SECRET_FILE}\n"
    )
    sys.exit(1)


def _save_cli_secret(secret: str) -> None:
    """Save a secret to ~/.cortex/device_secret with user-only permissions."""
    CLI_SECRET_DIR.mkdir(parents=True, exist_ok=True)
    CLI_SECRET_FILE.write_text(secret)

    # Set user-only permissions on Windows
    if sys.platform == "win32":
        try:
            import subprocess
            # Remove inherited permissions and grant only current user
            subprocess.run(
                ["icacls", str(CLI_SECRET_FILE), "/inheritance:r",
                 "/grant:r", f"{os.environ.get('USERNAME', 'User')}:(R,W)"],
                capture_output=True, check=False,
            )
        except Exception:
            pass  # Best-effort permission setting


def _print_banner():
    print(f"\n{_BOLD}{_CYAN}╔══════════════════════════════════╗")
    print(f"║      Cortex Hub CLI  v0.3        ║")
    print(f"╚══════════════════════════════════╝{_RESET}")
    print(f"{_DIM}Type 'help' for available commands.{_RESET}\n")


def _print_help():
    print(f"""
{_BOLD}Agent commands:{_RESET}
  {_CYAN}take_screenshot{_RESET}          — capture the agent's screen
  {_CYAN}open_app <name>{_RESET}          — open an application by name
  {_CYAN}move_file{_RESET}                — move a file (prompts for src + dst)

{_BOLD}Device management:{_RESET}
  {_CYAN}bootstrap{_RESET}                — first-run: create admin device
  {_CYAN}enroll{_RESET}                   — register a new device
  {_CYAN}rotate{_RESET}                   — rotate a device's secret (admin only)
  {_CYAN}devices{_RESET}                  — list enrolled devices + online status

{_BOLD}Confirmations (Tier 2/3):{_RESET}
  {_CYAN}pending{_RESET}                  — list pending confirmations
  {_CYAN}approve <id>{_RESET}             — approve a pending confirmation
  {_CYAN}deny <id>{_RESET}               — deny a pending confirmation

{_BOLD}Other:{_RESET}
  {_CYAN}logs{_RESET}                     — show 20 most recent action logs
  {_CYAN}help{_RESET}                     — show this message
  {_CYAN}exit{_RESET} / {_CYAN}quit{_RESET}               — disconnect and exit
""")


def _input_prompt(label: str) -> str:
    """Blocking input with a styled prompt. Returns stripped string."""
    try:
        return input(f"  {_DIM}{label}:{_RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


# ---------------------------------------------------------------------------
# Authentication handshake
# ---------------------------------------------------------------------------
async def _authenticate_cli(ws, device_id: str, secret: str) -> str | None:
    """Authenticate the CLI with the hub.

    Returns the authenticated role on success, or None on failure.
    """
    try:
        # Wait for auth_challenge
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
        data = json.loads(raw)

        if data.get("type") != "auth_challenge":
            print(f"{_RED}Expected auth_challenge, got: {data.get('type')}{_RESET}")
            return None

        # Send auth response
        auth_response = {
            "type": "auth_response",
            "device_id": device_id,
            "response": secret,
        }
        await ws.send(json.dumps(auth_response))

        # Wait for auth result
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
        result = json.loads(raw)

        if result.get("type") != "auth_result":
            print(f"{_RED}Expected auth_result, got: {result.get('type')}{_RESET}")
            return None

        if result.get("success"):
            role = result.get("role", "unknown")
            print(f"{_GREEN}✓ Authenticated as '{device_id}' (role={role}){_RESET}")
            return role
        else:
            print(f"{_RED}✗ Authentication failed: {result.get('message', '?')}{_RESET}")
            return None

    except asyncio.TimeoutError:
        print(f"{_RED}Auth handshake timed out.{_RESET}")
        return None
    except Exception as exc:
        print(f"{_RED}Auth error: {exc}{_RESET}")
        return None


async def _listen(ws, stop_event: asyncio.Event) -> None:
    """Background coroutine — prints events received from the hub."""
    try:
        async for raw in ws:
            if stop_event.is_set():
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event = msg.get("event", "")

            if event == "device_list":
                devices = msg.get("devices", [])
                if devices:
                    print(f"\n{_GREEN}● Enrolled devices:{_RESET}")
                    for d in devices:
                        status = f"{_GREEN}online{_RESET}" if d.get("online") else f"{_DIM}offline{_RESET}"
                        role = d.get("role", "?")
                        role_color = _MAGENTA if role == "admin" else _CYAN
                        name = d.get("device_name", d.get("device_id", "?"))
                        print(
                            f"  {_BOLD}{d.get('device_id', '?')}{_RESET} "
                            f"({name}) [{role_color}{role}{_RESET}] — {status}"
                        )
                else:
                    print(f"\n{_YELLOW}● No devices enrolled yet. Use 'bootstrap' to create admin.{_RESET}")

            elif event == "device_connected":
                role = msg.get("role", "?")
                print(f"\n{_GREEN}[+]{_RESET} Device connected: {_BOLD}{msg['device_id']}{_RESET} [{role}]")

            elif event == "device_disconnected":
                print(f"\n{_RED}[-]{_RESET} Device disconnected: {_BOLD}{msg['device_id']}{_RESET}")

            elif event == "result":
                icon = f"{_GREEN}✓{_RESET}" if msg["status"] == "success" else f"{_RED}✗{_RESET}"
                cmd_id = msg.get("command_id", "")
                id_str = f" (id={cmd_id[:8]}…)" if cmd_id else ""
                print(
                    f"\n{icon} [{_BOLD}{msg['device_id']}{_RESET}] "
                    f"{msg['command']} → {msg['status']}: {_DIM}{msg['message']}{_RESET}{id_str}"
                )

            elif event == "sent":
                cmd_id = msg.get("command_id", "")
                id_str = f" (id={cmd_id[:8]}…)" if cmd_id else ""
                print(
                    f"  {_DIM}→ Command dispatched to "
                    f"{msg['device_id']}: {msg['command']} {msg.get('args', [])}{id_str}{_RESET}"
                )

            elif event == "confirmation_request":
                cmd_id = msg.get("command_id", "")[:8]
                tier = msg.get("tier", "?")
                timeout = msg.get("timeout_seconds", 60)
                print(f"\n{_YELLOW}{'━' * 60}")
                print(f"  ⚠  CONFIRMATION REQUIRED  (Tier {tier})")
                print(f"{'━' * 60}{_RESET}")
                print(f"  {_BOLD}Command:{_RESET}  {msg.get('command', '?')}")
                print(f"  {_BOLD}Target:{_RESET}   {msg.get('target_device', '?')}")
                print(f"  {_BOLD}Args:{_RESET}     {msg.get('args', [])}")
                print(f"  {_BOLD}From:{_RESET}     {msg.get('requested_by', '?')}")
                print(f"  {_BOLD}ID:{_RESET}       {cmd_id}")
                print(f"  {_BOLD}Timeout:{_RESET}  {timeout}s")
                print(f"\n  {_CYAN}approve {cmd_id}{_RESET}  or  {_CYAN}deny {cmd_id}{_RESET}")
                print(f"{_YELLOW}{'━' * 60}{_RESET}")

            elif event == "confirmed_and_sent":
                cmd_id = msg.get("command_id", "")[:8]
                print(
                    f"\n{_GREEN}✓ Approved{_RESET} — {msg.get('command')} sent to "
                    f"{_BOLD}{msg.get('device_id')}{_RESET} (id={cmd_id}…)"
                )

            elif event == "confirmation_denied":
                cmd_id = msg.get("command_id", "")[:8]
                print(
                    f"\n{_RED}✗ Denied{_RESET} — {msg.get('command')} to "
                    f"{_BOLD}{msg.get('device_id')}{_RESET} (id={cmd_id}…)"
                )

            elif event == "confirmation_expired":
                cmd_id = msg.get("command_id", "")[:8]
                print(
                    f"\n{_YELLOW}⏰ Expired{_RESET} — {msg.get('command')} to "
                    f"{_BOLD}{msg.get('device_id')}{_RESET} (id={cmd_id}…): {msg.get('message', '')}"
                )

            elif event == "error":
                print(f"\n{_RED}[ERROR]{_RESET} {msg.get('message', '?')}")

    except Exception:
        if not stop_event.is_set():
            print(f"\n{_RED}[WARN]{_RESET} Lost connection to hub.")
        stop_event.set()


async def _fetch_logs(hub_http: str, device_id: str, secret: str) -> None:
    """Fetch and print recent logs via HTTP GET /logs."""
    if not _HAS_HTTPX:
        print(f"{_YELLOW}Install httpx for the 'logs' command: pip install httpx{_RESET}")
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{hub_http}/logs",
                headers={"X-Cortex-Device": device_id, "X-Cortex-Secret": secret},
                timeout=5,
            )
            if resp.status_code == 401:
                print(f"{_RED}Authentication required for /logs.{_RESET}")
                return
            rows = resp.json().get("logs", [])
            if not rows:
                print(f"  {_DIM}No logs yet.{_RESET}")
                return
            print(f"\n{_BOLD}{'Timestamp':26} {'Device':15} {'Cmd ID':10} {'Command':20} {'Result':10} Message{_RESET}")
            print("─" * 100)
            for r in rows:
                result_col = (
                    f"{_GREEN}{r['result']}{_RESET}"
                    if r["result"] == "success"
                    else f"{_RED}{r['result']}{_RESET}"
                )
                cmd_id = r.get("command_id", "")[:8]
                print(
                    f"  {r['timestamp'][:19]:24}  "
                    f"{r['device_id']:15} {cmd_id:10} {r['command']:20} {result_col:20} {_DIM}{r['message'][:40]}{_RESET}"
                )
    except Exception as exc:
        print(f"{_RED}Could not fetch logs: {exc}{_RESET}")


async def _fetch_devices(hub_http: str, device_id: str, secret: str) -> None:
    """Fetch and print enrolled devices via HTTP GET /devices."""
    if not _HAS_HTTPX:
        print(f"{_YELLOW}Install httpx for the 'devices' command: pip install httpx{_RESET}")
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{hub_http}/devices",
                headers={"X-Cortex-Device": device_id, "X-Cortex-Secret": secret},
                timeout=5,
            )
            if resp.status_code == 401:
                print(f"{_RED}Authentication required for /devices.{_RESET}")
                return
            devices = resp.json().get("devices", [])
            if not devices:
                print(f"  {_DIM}No devices enrolled. Use 'bootstrap' to create admin.{_RESET}")
                return
            print(f"\n{_BOLD}{'Device ID':20} {'Name':20} {'Role':12} {'Online':8} Last Seen{_RESET}")
            print("─" * 80)
            for d in devices:
                status = f"{_GREEN}yes{_RESET}" if d.get("online") else f"{_DIM}no{_RESET}"
                role = d.get("role", "?")
                role_col = f"{_MAGENTA}{role}{_RESET}" if role == "admin" else f"{_CYAN}{role}{_RESET}"
                last_seen = d.get("last_seen", "")[:19] or "never"
                print(
                    f"  {d['device_id']:20} {d.get('device_name', ''):20} "
                    f"{role_col:22} {status:18} {_DIM}{last_seen}{_RESET}"
                )
    except Exception as exc:
        print(f"{_RED}Could not fetch devices: {exc}{_RESET}")


async def _fetch_pending(hub_http: str, device_id: str, secret: str) -> None:
    """Fetch and print pending confirmations via HTTP GET /pending."""
    if not _HAS_HTTPX:
        print(f"{_YELLOW}Install httpx for the 'pending' command: pip install httpx{_RESET}")
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{hub_http}/pending",
                headers={"X-Cortex-Device": device_id, "X-Cortex-Secret": secret},
                timeout=5,
            )
            if resp.status_code == 401:
                print(f"{_RED}Authentication required for /pending.{_RESET}")
                return
            rows = resp.json().get("pending", [])
            if not rows:
                print(f"  {_DIM}No pending confirmations.{_RESET}")
                return
            print(f"\n{_BOLD}{'ID':10} {'Command':20} {'Target':15} {'Tier':5} {'Requested By':15} Requested At{_RESET}")
            print("─" * 85)
            for r in rows:
                cmd_id = r.get("command_id", "")[:8]
                print(
                    f"  {cmd_id:10} {r.get('command', ''):20} {r.get('target_device', ''):15} "
                    f"{r.get('tier', ''):5} {r.get('requested_by', ''):15} "
                    f"{_DIM}{r.get('requested_at', '')[:19]}{_RESET}"
                )
    except Exception as exc:
        print(f"{_RED}Could not fetch pending confirmations: {exc}{_RESET}")


async def _do_bootstrap(hub_http: str) -> None:
    """Create the first admin device via POST /bootstrap."""
    if not _HAS_HTTPX:
        print(f"{_YELLOW}Install httpx: pip install httpx{_RESET}")
        return
    device_id = _input_prompt("Admin device ID (e.g. asus)")
    if not device_id:
        print(f"  {_YELLOW}Cancelled.{_RESET}")
        return
    device_name = _input_prompt("Friendly name (e.g. ASUS Laptop)") or device_id

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{hub_http}/bootstrap",
                json={"device_id": device_id, "device_name": device_name},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                print(f"\n{_GREEN}✓ Admin device enrolled!{_RESET}")
                print(f"  {_BOLD}Device ID:{_RESET}  {data['device_id']}")
                print(f"  {_BOLD}Name:{_RESET}      {data['device_name']}")
                print(f"  {_BOLD}Role:{_RESET}      {_MAGENTA}{data['role']}{_RESET}")
                print(f"\n  {_BOLD}{_RED}SECRET (save this — shown only once):{_RESET}")
                print(f"  {_YELLOW}{data['secret']}{_RESET}")
                print(f"\n  {_DIM}Store in ~/.cortex/device_secret{_RESET}")

                # Offer to save automatically
                save = _input_prompt("Save to ~/.cortex/device_secret? (y/n)").lower()
                if save in ("y", "yes"):
                    _save_cli_secret(data["secret"])
                    print(f"  {_GREEN}✓ Saved to {CLI_SECRET_FILE}{_RESET}")
            else:
                detail = resp.json().get("detail", resp.text)
                print(f"\n{_RED}Bootstrap failed:{_RESET} {detail}")
    except Exception as exc:
        print(f"{_RED}Bootstrap error: {exc}{_RESET}")


async def _do_enroll(hub_http: str, admin_device_id: str, admin_secret: str) -> None:
    """Enroll a new device via POST /enroll (requires admin auth)."""
    if not _HAS_HTTPX:
        print(f"{_YELLOW}Install httpx: pip install httpx{_RESET}")
        return

    print(f"\n{_BOLD}Enroll a new device{_RESET}")

    new_device_id = _input_prompt("New device ID (e.g. lenovo)")
    if not new_device_id:
        print(f"  {_YELLOW}Cancelled.{_RESET}")
        return
    new_device_name = _input_prompt("Friendly name (e.g. Lenovo Laptop)") or new_device_id
    role = _input_prompt("Role (admin/sub_admin)") or "sub_admin"
    if role not in ("admin", "sub_admin"):
        print(f"  {_YELLOW}Invalid role. Use 'admin' or 'sub_admin'.{_RESET}")
        return

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{hub_http}/enroll",
                json={
                    "device_id": new_device_id,
                    "device_name": new_device_name,
                    "role": role,
                },
                headers={
                    "X-Cortex-Device": admin_device_id,
                    "X-Cortex-Secret": admin_secret,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                role_color = _MAGENTA if data["role"] == "admin" else _CYAN
                print(f"\n{_GREEN}✓ Device enrolled!{_RESET}")
                print(f"  {_BOLD}Device ID:{_RESET}  {data['device_id']}")
                print(f"  {_BOLD}Name:{_RESET}      {data['device_name']}")
                print(f"  {_BOLD}Role:{_RESET}      {role_color}{data['role']}{_RESET}")
                print(f"\n  {_BOLD}{_RED}SECRET (save this — shown only once):{_RESET}")
                print(f"  {_YELLOW}{data['secret']}{_RESET}")
                print(f"\n  {_DIM}Store in agent/.cortex_secret on the target machine{_RESET}")
            else:
                detail = resp.json().get("detail", resp.text)
                print(f"\n{_RED}Enrollment failed:{_RESET} {detail}")
    except Exception as exc:
        print(f"{_RED}Enroll error: {exc}{_RESET}")


async def _do_rotate(hub_http: str, admin_device_id: str, admin_secret: str) -> None:
    """Rotate a device's secret via POST /rotate-secret (admin only)."""
    if not _HAS_HTTPX:
        print(f"{_YELLOW}Install httpx: pip install httpx{_RESET}")
        return

    target_id = _input_prompt("Target device ID to rotate")
    if not target_id:
        print(f"  {_YELLOW}Cancelled.{_RESET}")
        return

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{hub_http}/rotate-secret",
                json={"target_device_id": target_id},
                headers={
                    "X-Cortex-Device": admin_device_id,
                    "X-Cortex-Secret": admin_secret,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                print(f"\n{_GREEN}✓ Secret rotated for '{data['device_id']}'!{_RESET}")
                print(f"\n  {_BOLD}{_RED}NEW SECRET (save this — shown only once):{_RESET}")
                print(f"  {_YELLOW}{data['new_secret']}{_RESET}")
                print(f"\n  {_DIM}Update agent/.cortex_secret or ~/.cortex/cli_secret on the target machine{_RESET}")

                # If rotating own secret, offer to update local storage
                if target_id == admin_device_id:
                    save = _input_prompt("Update your local ~/.cortex/cli_secret? (y/n)").lower()
                    if save in ("y", "yes"):
                        _save_cli_secret(data["new_secret"])
                        print(f"  {_GREEN}✓ Updated {CLI_SECRET_FILE}{_RESET}")
                        print(f"  {_YELLOW}⚠ Restart the CLI to use the new secret.{_RESET}")
            else:
                detail = resp.json().get("detail", resp.text)
                print(f"\n{_RED}Rotation failed:{_RESET} {detail}")
    except Exception as exc:
        print(f"{_RED}Rotate error: {exc}{_RESET}")


# ---------------------------------------------------------------------------
# Single CLI session
# ---------------------------------------------------------------------------
async def _cli_session(
    hub_ws_url: str,
    hub_http_url: str,
    device_id: str,
    secret: str,
) -> bool:
    """Run one CLI session. Returns True if we should reconnect, False to exit."""
    try:
        async with websockets.connect(hub_ws_url, ping_interval=20, ping_timeout=30) as ws:
            # Authenticate
            role = await _authenticate_cli(ws, device_id, secret)
            if role is None:
                return False  # Don't reconnect on auth failure

            stop_event = asyncio.Event()
            listener = asyncio.create_task(_listen(ws, stop_event))

            # Give listener a moment to print the initial device_list event
            await asyncio.sleep(0.3)

            while not stop_event.is_set():
                try:
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: input(f"\n{_BOLD}{_CYAN}cortex>{_RESET} ").strip(),
                    )
                except (EOFError, KeyboardInterrupt):
                    return False  # User wants to quit

                if not user_input:
                    continue

                parts = user_input.split(maxsplit=1)
                cmd_name = parts[0].lower()

                # ── Meta commands ─────────────────────────────────────────
                if cmd_name in ("exit", "quit"):
                    return False
                if cmd_name == "help":
                    _print_help()
                    continue
                if cmd_name == "devices":
                    await _fetch_devices(hub_http_url, device_id, secret)
                    continue
                if cmd_name == "logs":
                    await _fetch_logs(hub_http_url, device_id, secret)
                    continue
                if cmd_name == "bootstrap":
                    await _do_bootstrap(hub_http_url)
                    continue
                if cmd_name == "enroll":
                    await _do_enroll(hub_http_url, device_id, secret)
                    continue
                if cmd_name == "rotate":
                    await _do_rotate(hub_http_url, device_id, secret)
                    continue
                if cmd_name == "pending":
                    await _fetch_pending(hub_http_url, device_id, secret)
                    continue

                # ── Approve / Deny ────────────────────────────────────────
                if cmd_name == "approve":
                    cmd_id = parts[1].strip() if len(parts) > 1 else ""
                    if not cmd_id:
                        cmd_id = _input_prompt("Command ID (first 8 chars)")
                    if not cmd_id:
                        print(f"  {_YELLOW}Cancelled.{_RESET}")
                        continue
                    await ws.send(json.dumps({
                        "type": "confirmation_response",
                        "command_id": cmd_id,
                        "decision": "approve",
                    }))
                    continue

                if cmd_name == "deny":
                    cmd_id = parts[1].strip() if len(parts) > 1 else ""
                    if not cmd_id:
                        cmd_id = _input_prompt("Command ID (first 8 chars)")
                    if not cmd_id:
                        print(f"  {_YELLOW}Cancelled.{_RESET}")
                        continue
                    await ws.send(json.dumps({
                        "type": "confirmation_response",
                        "command_id": cmd_id,
                        "decision": "deny",
                    }))
                    continue

                # ── Real commands ─────────────────────────────────────────
                if cmd_name not in VALID_COMMANDS:
                    print(
                        f"  {_YELLOW}Unknown command '{cmd_name}'. "
                        f"Type 'help' for available commands.{_RESET}"
                    )
                    continue

                # Pick target device
                target_device_id = _input_prompt("Target device ID")
                if not target_device_id:
                    print(f"  {_YELLOW}No device specified, cancelling.{_RESET}")
                    continue

                # Build args list per command
                args: list[str] = []

                if cmd_name == "take_screenshot":
                    args = []

                elif cmd_name == "open_app":
                    app_name = (parts[1].strip() if len(parts) > 1 else "").strip()
                    if not app_name:
                        app_name = _input_prompt("App name (e.g. notepad)")
                    if not app_name:
                        print(f"  {_YELLOW}App name required, cancelling.{_RESET}")
                        continue
                    args = [app_name]

                elif cmd_name == "move_file":
                    src = _input_prompt("Source path")
                    if not src:
                        print(f"  {_YELLOW}Source path required, cancelling.{_RESET}")
                        continue
                    dst = _input_prompt("Destination path")
                    if not dst:
                        print(f"  {_YELLOW}Destination path required, cancelling.{_RESET}")
                        continue
                    args = [src, dst]

                # Send to hub
                payload = {
                    "device_id": target_device_id,
                    "command": cmd_name,
                    "args": args,
                }
                await ws.send(json.dumps(payload))

            stop_event.set()
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass

    except (OSError, ConnectionRefusedError) as exc:
        print(f"\n{_RED}Connection lost: {exc}{_RESET}")
        return True  # Reconnect
    except Exception as exc:
        print(f"\n{_RED}Unexpected error: {exc}{_RESET}")
        return True  # Reconnect

    return False


# ---------------------------------------------------------------------------
# Main entry with reconnection
# ---------------------------------------------------------------------------
async def run_cli(
    hub_ws_url: str,
    hub_http_url: str,
    device_id: str,
    secret: str,
) -> None:
    _print_banner()
    backoff = INITIAL_BACKOFF

    while True:
        print(f"Connecting to hub at {_CYAN}{hub_ws_url}{_RESET} …\n")
        should_reconnect = await _cli_session(hub_ws_url, hub_http_url, device_id, secret)

        if not should_reconnect:
            break

        print(f"{_YELLOW}Reconnecting in {backoff}s…{_RESET}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)

    print(f"\n{_DIM}Goodbye.{_RESET}\n")


def main():
    parser = argparse.ArgumentParser(description="Cortex Hub CLI (Phase 3)")
    parser.add_argument(
        "--hub",
        default="ws://localhost:8000",
        help="Hub WebSocket base URL (default: ws://localhost:8000)",
    )
    parser.add_argument(
        "--device",
        required=True,
        help="Your device ID for authentication (e.g. asus)",
    )
    parser.add_argument(
        "--secret",
        default=None,
        help="Device secret (testing override — normally loaded from ~/.cortex/device_secret)",
    )
    args = parser.parse_args()

    secret = _load_cli_secret(args.secret)

    hub_ws_url = args.hub.rstrip("/") + "/ws-cli"
    hub_http_url = args.hub.replace("ws://", "http://").replace("wss://", "https://")

    asyncio.run(run_cli(hub_ws_url, hub_http_url, args.device, secret))


if __name__ == "__main__":
    main()
