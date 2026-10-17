"""
cortex.py
---------
Cortex Unified Launcher & Lifecycle Management (Phase 9F).
Provides CLI management commands:
  cortex start           - Wake Cortex runtime (Hub + Agent + Voice)
  cortex sleep           - Sleep Cortex runtime (returns to lightweight DORMANT state)
  cortex status          - Check supervisor and runtime status
  cortex restart         - Restart Cortex runtime
  cortex shutdown        - Fully terminate supervisor, heavy runtime, and wake listener
  cortex supervisor      - Run the lightweight supervisor / wake listener process
  cortex install-startup - Register supervisor to launch automatically on Windows login
  cortex uninstall-startup - Remove supervisor from Windows login
  cortex                 - (default) Launch the Cortex Desktop GUI
"""

import argparse
import json
import os
import sys
import time

from hub.supervisor import (
    Supervisor,
    SupervisorClient,
    register_windows_startup,
    unregister_windows_startup,
    is_windows_startup_registered,
)


def cmd_status(args):
    client = SupervisorClient(port=args.port)
    info = client.status()
    if not info:
        print("[CORTEX] Supervisor is NOT running (SHUTDOWN).")
        print("Run 'python cortex.py supervisor' or log in to Windows to start it.")
        return 1

    print("\n--- Cortex Runtime Status ---")
    print(f"State:               {info.get('state', 'UNKNOWN')}")
    print(f"Device ID:           {info.get('device_id', 'unknown')}")
    print(f"Hub Status:          {'READY (PID ' + str(info.get('hub_pid')) + ')' if info.get('hub_ready') else 'OFFLINE'}")
    print(f"Agent Status:        {'CONNECTED (PID ' + str(info.get('agent_pid')) + ')' if info.get('agent_connected') else 'DISCONNECTED'}")
    print(f"Hub Restarts:        {info.get('hub_restarts', 0)}")
    print(f"Agent Restarts:      {info.get('agent_restarts', 0)}")
    uptime = info.get('uptime_seconds', 0)
    print(f"Uptime:              {uptime:.1f}s")
    if info.get('last_acknowledgment'):
        print(f"Last Ack:            \"{info.get('last_acknowledgment')}\"")
    print("-----------------------------\n")
    return 0


def cmd_start(args):
    client = SupervisorClient(port=args.port)
    info = client.status()
    if not info:
        print("[CORTEX] Supervisor is not running. Starting supervisor in background...")
        import subprocess
        creationflags = 0
        if sys.platform == "windows":
            creationflags = subprocess.CREATE_NO_WINDOW
        cmd = [sys.executable, __file__, "supervisor"]
        subprocess.Popen(cmd, creationflags=creationflags)
        # Wait for supervisor to start
        for _ in range(20):
            time.sleep(0.3)
            info = client.status()
            if info:
                break

    print("[CORTEX] Requesting runtime activation (wake)...")
    if client.start():
        print("[CORTEX] Wake initiated. Waiting for ACTIVE state...")
        for _ in range(40):
            time.sleep(0.5)
            st = client.status()
            if st and st.get("state") == "ACTIVE":
                print("[CORTEX] Cortex is ACTIVE! ('I'm awake.')")
                return 0
        print("[CORTEX] Startup initiated (check 'cortex status').")
        return 0
    else:
        print("[CORTEX] Failed to contact supervisor.")
        return 1


def cmd_sleep(args):
    client = SupervisorClient(port=args.port)
    info = client.status()
    if not info:
        print("[CORTEX] Supervisor is not running.")
        return 1
    print("[CORTEX] Transitioning runtime to DORMANT (sleep)...")
    if client.sleep():
        print("[CORTEX] Sleep initiated. Cortex is returning to lightweight wake listener mode.")
        return 0
    else:
        print("[CORTEX] Failed to send sleep request.")
        return 1


def cmd_restart(args):
    client = SupervisorClient(port=args.port)
    if not client.status():
        print("[CORTEX] Supervisor is not running.")
        return 1
    print("[CORTEX] Restarting Cortex runtime...")
    if client.restart():
        print("[CORTEX] Restart initiated.")
        return 0
    return 1


def cmd_shutdown(args):
    client = SupervisorClient(port=args.port)
    if not client.status():
        print("[CORTEX] Supervisor is already stopped.")
        return 0
    print("[CORTEX] Terminating supervisor and heavy runtime...")
    if client.shutdown():
        print("[CORTEX] Cortex shutdown complete.")
        return 0
    return 1


def cmd_supervisor(args):
    print("[CORTEX] Starting Cortex Supervisor process...")
    sup = Supervisor.get_instance(
        hub_port=args.hub_port,
        ipc_port=args.port,
        device_id=args.device,
        use_mock_voice=args.mock_voice,
    )
    sup.run_forever()
    return 0


def cmd_install_startup(args):
    res = register_windows_startup()
    print(f"[CORTEX] Windows Startup: {res.get('message', res)}")
    return 0 if res.get("status") == "success" else 1


def cmd_uninstall_startup(args):
    res = unregister_windows_startup()
    print(f"[CORTEX] Windows Startup: {res.get('message', res)}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Cortex Unified Launcher & Lifecycle Management")
    subparsers = parser.add_subparsers(dest="command", help="Management command")

    p_status = subparsers.add_parser("status", help="Show supervisor & runtime state")
    p_status.add_argument("--port", type=int, default=8008, help="Supervisor IPC port")

    p_start = subparsers.add_parser("start", help="Wake Cortex runtime (Hub + Agent)")
    p_start.add_argument("--port", type=int, default=8008, help="Supervisor IPC port")

    p_sleep = subparsers.add_parser("sleep", help="Sleep heavy runtime and return to DORMANT")
    p_sleep.add_argument("--port", type=int, default=8008, help="Supervisor IPC port")

    p_restart = subparsers.add_parser("restart", help="Restart runtime")
    p_restart.add_argument("--port", type=int, default=8008, help="Supervisor IPC port")

    p_shutdown = subparsers.add_parser("shutdown", help="Shut down supervisor and runtime")
    p_shutdown.add_argument("--port", type=int, default=8008, help="Supervisor IPC port")

    p_sup = subparsers.add_parser("supervisor", help="Run supervisor process")
    p_sup.add_argument("--port", type=int, default=8008, help="Supervisor IPC port")
    p_sup.add_argument("--hub-port", type=int, default=8000, help="Hub HTTP port")
    p_sup.add_argument("--device", type=str, default=None, help="Device ID")
    p_sup.add_argument("--mock-voice", action="store_true", help="Use mock voice providers")

    p_inst = subparsers.add_parser("install-startup", help="Register supervisor on Windows login")
    p_uninst = subparsers.add_parser("uninstall-startup", help="Unregister supervisor from Windows login")

    # If no subcommand given, or --ui given: launch desktop GUI
    if len(sys.argv) == 1 or sys.argv[1] in ("--ui", "--gui"):
        from agent.ui.main import main as ui_main
        ui_main()
        return

    args = parser.parse_args()

    dispatch = {
        "status": cmd_status,
        "start": cmd_start,
        "sleep": cmd_sleep,
        "restart": cmd_restart,
        "shutdown": cmd_shutdown,
        "supervisor": cmd_supervisor,
        "install-startup": cmd_install_startup,
        "uninstall-startup": cmd_uninstall_startup,
    }

    fn = dispatch.get(args.command)
    if fn:
        sys.exit(fn(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
