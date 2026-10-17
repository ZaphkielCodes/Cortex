import logging
import uuid
import time
from typing import Optional, List, Dict, Any

from .database import get_device, get_device_capabilities, list_devices, create_confirmation
from .permissions import check_permission
from .models import CommandMessage
# We need to dispatch to connected devices. We'll pass a reference to the main dict or a callback.

log = logging.getLogger("cortex.control")

class CortexControlLayer:
    def __init__(self, connected_devices_map: Optional[dict] = None):
        self.connected_devices = connected_devices_map if connected_devices_map is not None else {}
        
    async def resolve_target_device(self, source_device_id: str, target_reference: Optional[str]) -> Optional[str]:
        if not target_reference:
            return source_device_id
            
        ref = target_reference.lower()
        if ref in ("this device", "here", "this laptop", "my phone", "this", "this computer", "my desktop", "current", "local"):
            return source_device_id
            
        all_devices = await list_devices()
        
        for d in all_devices:
            name = (d.get("device_name") or "").lower()
            if ref == name or ref == d["device_id"].lower():
                return d["device_id"]
                
        for d in all_devices:
            role = (d.get("role") or "").lower()
            name = (d.get("device_name") or "").lower()
            if (role and ref in role) or (name and ref in name):
                return d["device_id"]
                
        return None

    async def resolve_executor(
        self,
        required_capability: str,
        requested_device: Optional[str] = None,
        source_device_id: str = "asus",
        owning_device: Optional[str] = None,
        permission_tier: int = 1,
        classification: str = "read",
    ) -> Any:
        from .executor_selector import ExecutorSelector
        selector = ExecutorSelector()
        return selector.select_executor(
            required_capability=required_capability,
            requested_device=requested_device,
            source_device=source_device_id,
            owning_device=owning_device,
            permission_tier=permission_tier,
            classification=classification,
        )

    async def validate_target_and_capability(self, target_device: str, required_cap: str) -> dict:
        from .permissions import COMMAND_TIERS
        if required_cap.startswith("system.") or required_cap.startswith("hub.") or required_cap.startswith("task.") or required_cap.startswith("memory.") or required_cap.startswith("people.") or required_cap.startswith("speaker.") or required_cap.startswith("social.") or required_cap.startswith("context.") or required_cap.startswith("proactive.") or required_cap.startswith("attention.") or required_cap.startswith("calendar.") or required_cap.startswith("telemetry.") or required_cap.startswith("workload.") or required_cap.startswith("goal.") or required_cap.startswith("web.") or required_cap.startswith("weather."):
            if required_cap in COMMAND_TIERS:
                return {"valid": True, "reason": "ok", "role": "admin"}
            return {"valid": False, "reason": "missing_capability", "caps": list(COMMAND_TIERS.keys())}

        device = await get_device(target_device)
        from .executor_registry import ExecutorRegistry
        er = ExecutorRegistry.get_instance()
        handle = er.get_device_handle(target_device)

        if not device and not handle:
            return {"valid": False, "reason": "not_enrolled"}

        is_online = (target_device in self.connected_devices) or er.is_device_online(target_device)
        if not is_online:
            return {"valid": False, "reason": "offline"}

        db_caps = await get_device_capabilities(target_device)
        caps = list(set((handle.capabilities if handle and handle.capabilities else []) + (db_caps or [])))
        if handle:
            for c in (db_caps or []):
                if c not in handle.capabilities:
                    handle.capabilities.append(c)

        if not any(c == required_cap or c == "*" for c in caps):
            return {"valid": False, "reason": "missing_capability", "caps": caps}

        role = handle.role if handle else device["role"]
        return {"valid": True, "reason": "ok", "role": role}

    async def check_permissions(self, source_device_id: str, target_device: str, required_cap: str) -> Any:
        if required_cap.startswith("hub.") or required_cap.startswith("task.") or required_cap.startswith("memory.") or required_cap.startswith("people.") or required_cap.startswith("speaker.") or required_cap.startswith("social.") or required_cap.startswith("context.") or required_cap.startswith("proactive.") or required_cap.startswith("attention.") or required_cap.startswith("calendar.") or required_cap.startswith("telemetry.") or required_cap.startswith("workload.") or required_cap.startswith("goal.") or required_cap.startswith("web.") or required_cap.startswith("weather."):
            from .permissions import get_command_tier, PermissionResult
            tier = get_command_tier(required_cap)
            return PermissionResult(allowed=True, needs_confirmation=(tier > 1), tier=tier, reason="Hub internal capability")
        caller = await get_device(source_device_id)
        if not caller:
            return None
        return await check_permission(
            sender_role=caller["role"],
            sender_id=source_device_id,
            target_id=target_device,
            command=required_cap
        )

    async def request_approval(self, target_device: str, action: str, args: dict, source_device_id: str, tier: int) -> str:
        cmd_id = uuid.uuid4().hex
        args_list = [str(v) for v in args.values()] if isinstance(args, dict) else [str(x) for x in args]
        await create_confirmation(
            command_id=cmd_id,
            target_device=target_device,
            command=action,
            args=args_list,
            requested_by=source_device_id,
            tier=tier
        )
        return cmd_id

    async def execute_command(self, target_device: str, action: str, args: dict, latency_metrics: dict = None, cmd_id: str = None, is_autonomous: bool = False) -> str:
        if is_autonomous:
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            if not await gm.is_autonomy_enabled():
                raise RuntimeError("Autonomous execution rejected by global kill switch")

        if latency_metrics is None:
            latency_metrics = {}
        latency_metrics["control_layer_start"] = time.monotonic()
        
        if not cmd_id:
            cmd_id = uuid.uuid4().hex

        # Internal goal capabilities do not require device agent websocket
        if action.startswith("goal.") or action == "wait":
            return cmd_id

        ws = self.connected_devices.get(target_device)
        if not ws:
            try:
                import hub.main as main_mod
                ws = getattr(main_mod, "CONNECTED_DEVICES", {}).get(target_device)
            except Exception:
                ws = None
        if not ws:
            raise Exception("Device is offline")
            
        if isinstance(args, dict):
            canonical_args = dict(args)
        elif isinstance(args, list):
            if len(args) == 1 and isinstance(args[0], str) and args[0].strip().startswith(("{", "[")):
                try:
                    import json
                    parsed = json.loads(args[0])
                    canonical_args = parsed if isinstance(parsed, dict) else args
                except Exception:
                    canonical_args = args
            else:
                canonical_args = args
        elif args is None:
            canonical_args = {}
        else:
            canonical_args = {"arg": args}
        
        latency_metrics["command_dispatched"] = time.monotonic()
        command_msg = CommandMessage(
            command_id=cmd_id,
            command=action,
            args=canonical_args,
            latency_metrics=latency_metrics
        )
        
        await ws.send_text(command_msg.model_dump_json())
        return cmd_id

    async def execute_and_observe(
        self,
        target_device: str,
        action: Optional[Any] = None,
        args: Optional[dict] = None,
        timeout: float = 15.0,
        cmd_id: Optional[str] = None,
        is_autonomous: bool = False,
        **kwargs,
    ) -> dict:
        """
        Execute command on target device and wait for physical/result response.
        Observes the actual ResultMessage returned by the device agent.
        """
        if action is None or isinstance(action, dict):
            actual_args = action if isinstance(action, dict) else (args or {})
            actual_action = target_device
            target_device = kwargs.get("device_id") or "asus"
            action = actual_action
            args = actual_args
        if args is None:
            args = {}

        if is_autonomous:
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            if not await gm.is_autonomy_enabled():
                return {
                    "status": "failure",
                    "message": "Autonomous execution rejected by global kill switch",
                    "error": "Autonomy disabled",
                }

        # Internal goal capabilities handled in Hub
        if action in ("goal.wait", "wait"):
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            args_dict = args if isinstance(args, dict) else {}
            cond_type = args_dict.get("condition_type", "world_state")
            cond_key = args_dict.get("condition_key") or args_dict.get("device_id") or args_dict.get("key", "")
            op = args_dict.get("operator", "==")
            exp_val = args_dict.get("expected_value", "")
            is_satisfied = await gm.is_condition_satisfied(
                condition_type=cond_type,
                condition_key=cond_key,
                operator=op,
                expected_value=exp_val,
                metadata=args_dict,
            )
            if is_satisfied:
                return {
                    "status": "success",
                    "message": f"Condition '{cond_type}:{cond_key}' satisfied",
                    "data": {"condition_type": cond_type, "condition_key": cond_key},
                }
            else:
                return {
                    "status": "waiting",
                    "message": args_dict.get("reason") or f"Waiting for {cond_type} ({cond_key})",
                    "condition": {
                        "condition_type": cond_type,
                        "condition_key": cond_key,
                        "operator": op,
                        "expected_value": exp_val,
                        "metadata": args_dict,
                    },
                }
        elif action == "goal.list_waits":
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            args_dict = args if isinstance(args, dict) else {}
            waits = await gm.list_wait_conditions(goal_id=args_dict.get("goal_id"), status=args_dict.get("status"))
            return {
                "status": "success",
                "message": f"Found {len(waits)} wait conditions",
                "data": [w.to_dict() for w in waits],
            }
        elif action == "goal.cancel_wait":
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            args_dict = args if isinstance(args, dict) else {}
            wait_id = args_dict.get("wait_id", "")
            cancelled = await gm.cancel_wait_condition(wait_id)
            return {
                "status": "success" if cancelled else "failure",
                "message": f"Wait condition '{wait_id}' cancelled" if cancelled else "Wait condition not found",
            }
        elif action == "goal.pause_all":
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            args_dict = args if isinstance(args, dict) else {}
            reason = args_dict.get("reason", "Paused by user")
            paused = await gm.pause_all_goals(reason=reason)
            return {
                "status": "success",
                "message": f"Paused {len(paused)} active goals",
                "data": {"paused_goal_ids": paused},
            }
        elif action == "goal.resume_all":
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            resumed = await gm.resume_all_goals()
            return {
                "status": "success",
                "message": f"Resumed {len(resumed)} paused goals",
                "data": {"resumed_goal_ids": resumed},
            }
        elif action in ("goal.kill_switch", "goal.set_autonomy"):
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            args_dict = args if isinstance(args, dict) else {}
            enabled = bool(args_dict.get("enabled", False))
            await gm.set_autonomy_enabled(enabled)
            return {
                "status": "success",
                "message": f"Global autonomy set to {enabled}",
                "data": {"autonomy_enabled": enabled},
            }
        elif action == "goal.get_autonomy":
            from .goals import GoalManager
            gm = GoalManager.get_instance()
            enabled = await gm.is_autonomy_enabled()
            return {
                "status": "success",
                "message": f"Global autonomy is {'enabled' if enabled else 'disabled'}",
                "data": {"autonomy_enabled": enabled},
            }
        elif action == "goal.plan":
            from .planner import GoalPlanner
            planner = GoalPlanner.get_instance(control_layer=self)
            args_dict = args if isinstance(args, dict) else {}
            goal_id = args_dict.get("goal_id", "")
            target_dev = args_dict.get("target_device")
            plan_res = await planner.plan_goal(goal_id=goal_id, target_device=target_dev)
            return {
                "status": "success" if plan_res.get("status") == "planned" else "failure",
                "message": plan_res.get("reason") or f"Planned {plan_res.get('step_count', 0)} steps",
                "data": plan_res,
            }
        elif action == "runtime.sleep":
            import os
            import urllib.request
            supervisor_port = int(os.environ.get("CORTEX_SUPERVISOR_PORT", "8008"))
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{supervisor_port}/sleep",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=2.0)
                return {
                    "status": "success",
                    "message": "Runtime transition to DORMANT initiated",
                    "data": {"state": "STOPPING"},
                }
            except Exception as exc:
                return {
                    "status": "failure",
                    "message": f"Could not contact supervisor: {exc}",
                    "data": {},
                }
        elif action == "system.identity":
            from .system_registry import SystemRegistry
            ident = SystemRegistry.get_instance().get_identity(current_node=target_device)
            return {"status": "success", "message": f"{ident['product_name']} v{ident['version']}", "data": ident}
        elif action == "system.models":
            from .system_registry import SystemRegistry
            models = SystemRegistry.get_instance().get_models()
            return {"status": "success", "message": f"Active models: {len(models)} introspected.", "data": models}
        elif action == "system.list_capabilities":
            from .system_registry import SystemRegistry
            caps = SystemRegistry.get_instance().list_capabilities()
            return {"status": "success", "message": f"{len(caps)} capabilities registered.", "data": caps}
        elif action == "system.capability_status":
            from .system_registry import SystemRegistry
            args_dict = args if isinstance(args, dict) else {}
            cap = args_dict.get("capability") or (args[0] if isinstance(args, list) and args else "")
            stat = SystemRegistry.get_instance().get_capability_status(cap.split(",") if "," in cap else cap)
            return {"status": "success", "message": "Capability status retrieved.", "data": stat}
        elif action == "system.list_devices":
            from .system_registry import SystemRegistry
            devices = await SystemRegistry.get_instance().list_devices(current_node=target_device, connected_devices_map=self.connected_devices)
            return {"status": "success", "message": f"{len(devices)} devices in registry.", "data": devices}
        elif action == "system.device_status":
            from .system_registry import SystemRegistry
            args_dict = args if isinstance(args, dict) else {}
            d_id = args_dict.get("device_id") or (args[0] if isinstance(args, list) and args else (target_device or "asus"))
            stat = await SystemRegistry.get_instance().get_device_status(d_id, current_node=target_device, connected_devices_map=self.connected_devices)
            return {"status": "success" if stat else "failure", "message": f"Device {d_id} status retrieved." if stat else f"Device {d_id} not found.", "data": stat}
        elif action == "system.list_integrations":
            from .system_registry import SystemRegistry
            integs = await SystemRegistry.get_instance().list_integrations()
            return {"status": "success", "message": f"{len(integs)} integrations introspected.", "data": integs}
        elif action == "system.integration_status":
            from .system_registry import SystemRegistry
            args_dict = args if isinstance(args, dict) else {}
            integ_name = args_dict.get("integration") or (args[0] if isinstance(args, list) and args else "")
            stat = await SystemRegistry.get_instance().get_integration_status(integ_name)
            return {"status": "success", "message": f"Integration {integ_name} status retrieved.", "data": stat}
        elif action == "system.providers":
            from .system_registry import SystemRegistry
            provs = SystemRegistry.get_instance().get_providers()
            return {"status": "success", "message": f"{len(provs)} providers reported.", "data": provs}
        elif action == "system.explain_capability":
            from .system_registry import SystemRegistry
            args_dict = args if isinstance(args, dict) else {}
            query = args_dict.get("query") or (args[0] if isinstance(args, list) and args else "")
            exp = await SystemRegistry.get_instance().explain_capability(query)
            return {"status": "success", "message": exp.get("explanation", ""), "data": exp}
        elif action == "web.search":
            from .search_provider import get_search_provider
            provider = get_search_provider()
            if not provider:
                return {
                    "status": "failure",
                    "message": "Live search provider is unavailable.",
                    "error": "Provider unavailable",
                }
            args_dict = args if isinstance(args, dict) else {}
            query = args_dict.get("query", "")
            max_res = int(args_dict.get("max_results", 5))
            recency = args_dict.get("recency_days")
            domains = args_dict.get("domains")
            try:
                res = await provider.search(query=query, max_results=max_res, recency_days=recency, domains=domains)
                return {
                    "status": "success",
                    "message": f"Search returned {len(res.get('results', []))} results.",
                    "data": res,
                }
            except Exception as exc:
                return {"status": "failure", "message": f"Search error: {exc}", "error": str(exc)}
        elif action == "web.fetch":
            from .web_fetch import web_fetch
            args_dict = args if isinstance(args, dict) else {}
            url = args_dict.get("url", "")
            max_chars = int(args_dict.get("max_chars", 5000))
            res = await web_fetch(url=url, max_chars=max_chars)
            return {
                "status": res["status"],
                "message": res["message"],
                "data": res,
            }
        elif action == "weather.current":
            from .weather_provider import get_weather_provider
            provider = get_weather_provider()
            if not provider:
                return {
                    "status": "failure",
                    "message": "Live weather provider is unavailable.",
                    "error": "Provider unavailable",
                }
            args_dict = args if isinstance(args, dict) else {}
            loc = args_dict.get("location", "")
            units = args_dict.get("units", "metric")
            try:
                res = await provider.get_current(location=loc, units=units)
                return {
                    "status": "success",
                    "message": f"Weather in {res.get('location')}: {res.get('temperature')}{res.get('temperature_unit')}, {res.get('conditions')}",
                    "data": res,
                }
            except Exception as exc:
                return {"status": "failure", "message": f"Weather lookup failed: {exc}", "error": str(exc)}
        elif action == "weather.forecast":
            from .weather_provider import get_weather_provider
            provider = get_weather_provider()
            if not provider:
                return {
                    "status": "failure",
                    "message": "Live weather provider is unavailable.",
                    "error": "Provider unavailable",
                }
            args_dict = args if isinstance(args, dict) else {}
            loc = args_dict.get("location", "")
            units = args_dict.get("units", "metric")
            try:
                res = await provider.get_forecast(location=loc, units=units)
                return {
                    "status": "success",
                    "message": f"Forecast retrieved for {res.get('location')}",
                    "data": res,
                }
            except Exception as exc:
                return {"status": "failure", "message": f"Weather forecast failed: {exc}", "error": str(exc)}
        elif action in ("telemetry.get_system_usage", "telemetry_get_system_usage"):
            from .world_state import DeviceSignalAdapter
            target_d = (args if isinstance(args, dict) else {}).get("target_device") or target_device or "asus"
            telem = DeviceSignalAdapter.inspect_system_telemetry(target_d)
            msg = f"CPU: {telem['cpu_percent']}%, RAM: {telem['ram_used_gb']}/{telem['ram_total_gb']} GB ({telem['ram_percent']}%), Disk: {telem['disk_free_gb']} GB free out of {telem['disk_total_gb']} GB ({telem['disk_percent']}% used)."
            return {"status": "success", "message": msg, "data": telem}
        elif action in ("telemetry.get_hardware_info", "telemetry_get_hardware_info"):
            from .world_state import DeviceSignalAdapter
            target_d = (args if isinstance(args, dict) else {}).get("target_device") or target_device or "asus"
            telem = DeviceSignalAdapter.inspect_system_telemetry(target_d)
            msg = f"Hardware info: {telem['manufacturer']} {telem['model']}, CPU: {telem['processor']}, GPU: {telem['gpu']}, Hostname: {telem['hostname']}, OS: {telem['os']}."
            return {"status": "success", "message": msg, "data": telem}
        elif action in ("calendar.status", "calendar_status"):
            from .world_state import CalendarSignalAdapter
            conn = await CalendarSignalAdapter.get_connection_status()
            st = conn.get("status", "disconnected")
            msg = f"Google Calendar is connected (account: {conn.get('account_id')})." if conn.get("connected") else f"Google Calendar is currently {st}."
            return {"status": "success", "message": msg, "data": conn}
        elif action in ("browser.open_url", "open_url"):
            args_dict = args if isinstance(args, dict) else {}
            url = args_dict.get("url", "")
            if target_device and target_device in self.connected_devices:
                pass  # Dispatches via websocket below
            elif target_device and target_device not in ("asus", "local", "this_device", "test_dev"):
                return {
                    "status": "failure",
                    "message": f"Target device '{target_device}' is not connected.",
                    "error": "device_offline",
                }
            else:
                from agent.executor import handle_browser_open_url
                res = handle_browser_open_url([url])
                return {
                    "status": res.get("status", "success"),
                    "message": res.get("message", ""),
                    "data": res,
                }
        elif action in ("screen.capture", "take_screenshot", "screenshot.read", "intent_screenshot"):
            if target_device and target_device in self.connected_devices:
                pass  # Dispatches via websocket below
            elif target_device and target_device not in ("asus", "local", "this_device", "test_dev"):
                return {
                    "status": "failure",
                    "message": f"Target device '{target_device}' is not connected.",
                    "error": "device_offline",
                }
            else:
                from agent.executor import handle_screen_capture
                res = handle_screen_capture(args)
                return {
                    "status": res.get("status", "success"),
                    "message": res.get("message", ""),
                    "data": res.get("data") or res,
                }
        elif action in (
            "app.list_running", "list_running_apps",
            "process.list", "list_processes",
            "window.list", "list_windows",
            "window.focus", "focus_window", "switch_window",
            "window.minimize", "window.maximize", "window.restore",
            "app.launch", "open_app",
            "app.close", "close_app", "kill_app",
            "fs.open", "open_file", "open_folder",
            "fs.copy", "copy_file",
            "fs.move", "move_file",
            "fs.rename", "rename_file",
            "fs.delete", "delete_file",
            "fs.list", "fs.stat", "fs.shortcut_info",
            # Phase A10 UI Actions
            "ui.observe", "ui.click", "ui.double_click", "ui.right_click",
            "ui.move_pointer", "ui.scroll", "ui.type", "ui.keypress",
            "ui.hotkey", "ui.drag", "ui.select", "ui.wait",
            "clipboard.read", "clipboard.write",
        ):
            if target_device and target_device in self.connected_devices:
                pass  # Dispatches via websocket below
            elif target_device and target_device not in ("asus", "local", "this_device", "test_dev"):
                return {
                    "status": "failure",
                    "message": f"Target device '{target_device}' is not connected.",
                    "error": "device_offline",
                }
            else:
                from agent.executor import execute
                res = execute(action, args)
                return dict(res, data=res.get("data") or res)

        elif action == "screen.analyze":
            from .vision_provider import get_vision_provider
            provider = get_vision_provider()
            if not provider or not getattr(provider, "is_available", lambda: True)():
                return {
                    "status": "failure",
                    "message": "screen analysis provider unavailable",
                    "error": "screen analysis provider unavailable",
                }
            args_dict = args if isinstance(args, dict) else {}
            img_ref = args_dict.get("screenshot") or args_dict.get("image_ref") or ""
            question = args_dict.get("question")
            mode = args_dict.get("mode", "describe")

            if not img_ref:
                from .screen_perception import ScreenPerceptionManager
                spm = ScreenPerceptionManager.get_instance()
                latest = spm.get_latest_observation(target_device)
                if latest and latest.source_image_reference:
                    img_ref = latest.source_image_reference

            try:
                obs = await provider.analyze_image(
                    image_ref=img_ref,
                    question=question,
                    mode=mode,
                    device_id=target_device or "local",
                )
                from .screen_perception import ScreenPerceptionManager
                spm = ScreenPerceptionManager.get_instance()
                spm.store_observation(obs)
                await spm.publish_world_state_signals(obs)
                return {
                    "status": "success",
                    "message": obs.summary,
                    "data": obs.to_dict(),
                }
            except Exception as exc:
                return {"status": "failure", "message": f"screen.analyze failed: {exc}", "error": str(exc)}

        import asyncio
        from .main import PENDING_FUTURES

        if not cmd_id:
            cmd_id = uuid.uuid4().hex

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        PENDING_FUTURES[cmd_id] = fut

        try:
            await self.execute_command(target_device, action, args, cmd_id=cmd_id)
            res = await asyncio.wait_for(fut, timeout=timeout)
            # Persist screenshot reference if action captured a screen
            msg_str = getattr(res, "message", "") if not isinstance(res, dict) else res.get("message", "")
            res_data = getattr(res, "data", None) if not isinstance(res, dict) else res.get("data")
            img_ref = None
            if isinstance(res_data, dict) and res_data.get("image_ref"):
                img_ref = res_data["image_ref"]
            elif "Screenshot saved: " in msg_str:
                img_ref = msg_str.split("Screenshot saved: ", 1)[1].strip()
            elif "Screenshot taken: " in msg_str:
                img_ref = msg_str.split("Screenshot taken: ", 1)[1].strip()
            if img_ref:
                try:
                    from .session_context import SessionContextManager
                    await SessionContextManager.set_screenshot_ref(target_device or "asus", img_ref)
                except Exception:
                    pass

            if hasattr(res, "model_dump"):
                return res.model_dump()
            elif hasattr(res, "to_dict"):
                return res.to_dict()
            elif isinstance(res, dict):
                return res
            return {
                "status": getattr(res, "status", "success"),
                "message": getattr(res, "message", ""),
                "command": getattr(res, "command", action),
                "command_id": getattr(res, "command_id", cmd_id),
                "data": getattr(res, "data", None),
            }
        except asyncio.TimeoutError:
            return {"status": "failure", "message": f"Execution timed out waiting for device response ({timeout}s)"}
        except Exception as exc:
            return {"status": "failure", "message": f"Execution error: {exc}"}
        finally:
            PENDING_FUTURES.pop(cmd_id, None)
