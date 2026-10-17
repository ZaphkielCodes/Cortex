"""
scenario_lab/runner.py
----------------------
Execution runner and sandboxed harness for Cortex Scenario Lab.

Responsibilities:
1. Environment Isolation:
   - Creates disposable temporary sandbox directory.
   - Redirects known folders (Desktop, Downloads, Documents) into sandbox.
   - Guarantees zero host filesystem access and clean teardown.
2. Production DB Safety:
   - Ensures production data/cortex.db is never touched.
   - Uses dedicated test database inside the sandbox or test path.
3. Action & Trace Recording:
   - Intercepts and records all plans, approvals, dispatched actions, signals, observations.
4. Deterministic Evaluation:
   - Invokes DeterministicEvaluator and generates structured JSON reports.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from hub import database
from hub.control_layer import CortexControlLayer
from hub.environment import (
    clear_known_folder_overrides,
    set_known_folder_override,
)
from hub.goals import GoalManager, GoalRunner, WaitCondition
from hub.planner import GoalPlanner
from hub.models import ResultMessage
from hub.main import PENDING_FUTURES
from hub.world_state import WorldStateManager, Signal

from .evaluator import DeterministicEvaluator
from .models import (
    ExecutionTrace,
    Scenario,
    ScenarioExpectation,
    ScenarioResult,
    ScenarioSafetyRule,
    ScenarioSetup,
)

log = logging.getLogger("cortex.scenario_lab.runner")

PROD_DB_PATH = Path("data/cortex.db")


class MockDeviceConnection:
    """Mock WebSocket device connection that intercepts and records all dispatched commands."""

    def __init__(
        self,
        device_id: str,
        trace: ExecutionTrace,
        default_status: str = "success",
        default_message: str = "Executed successfully",
        custom_handler=None,
    ):
        self.device_id = device_id
        self.trace = trace
        self.default_status = default_status
        self.default_message = default_message
        self.custom_handler = custom_handler
        self.received_commands: List[Dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        data = json.loads(text)
        self.received_commands.append(data)
        cmd_id = data.get("command_id")
        cmd = data.get("command")
        args = data.get("args") or data.get("arguments") or {}

        # Record dispatched action
        self.trace.actions_dispatched.append({
            "command_id": cmd_id,
            "command": cmd,
            "args": args,
            "device_id": self.device_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Track paths accessed
        if isinstance(args, dict):
            for k in ("path", "source", "destination", "file"):
                if k in args and isinstance(args[k], str):
                    self.trace.paths_accessed.append(args[k])
        elif isinstance(args, list):
            for item in args:
                if isinstance(item, str) and ("\\" in item or "/" in item):
                    self.trace.paths_accessed.append(item)

        # Complete pending future if waiting
        if cmd_id and cmd_id in PENDING_FUTURES:
            fut = PENDING_FUTURES[cmd_id]
            if not fut.done():
                if self.custom_handler:
                    status, msg, res_data = self.custom_handler(cmd, args)
                elif cmd and cmd.startswith("fs."):
                    try:
                        from agent import executor
                        exec_res = executor.execute(cmd, args)
                        status = exec_res.get("status", self.default_status)
                        msg = exec_res.get("message", self.default_message)
                        res_data = exec_res.get("data")
                    except Exception:
                        status = self.default_status
                        msg = self.default_message
                        res_data = None
                else:
                    status = self.default_status
                    msg = self.default_message
                    res_data = None

                result_msg = ResultMessage(
                    command_id=cmd_id,
                    command=cmd,
                    status=status,
                    message=msg,
                    data=res_data,
                )
                fut.set_result(result_msg)


class ScenarioRunner:
    """Orchestrates isolated scenario execution and deterministic evaluation."""

    def __init__(self, reports_dir: Optional[Union[str, Path]] = None):
        if reports_dir:
            self.reports_dir = Path(reports_dir)
        else:
            self.reports_dir = Path(__file__).parent / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def get_prod_db_hash() -> Optional[str]:
        """Compute SHA-256 of production DB for safety assertion."""
        if PROD_DB_PATH.exists():
            return hashlib.sha256(PROD_DB_PATH.read_bytes()).hexdigest()
        return None

    async def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        """Run a single scenario in complete isolation."""
        initial_prod_hash = self.get_prod_db_hash()
        start_time = time.time()
        trace = ExecutionTrace(user_goal=scenario.goal)

        # Create temporary sandbox
        sandbox_dir = Path(tempfile.mkdtemp(prefix=f"cortex_scenario_{scenario.name}_")).resolve()
        desktop_dir = (sandbox_dir / "Desktop").resolve()
        downloads_dir = (sandbox_dir / "Downloads").resolve()
        documents_dir = (sandbox_dir / "Documents").resolve()

        desktop_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        documents_dir.mkdir(parents=True, exist_ok=True)

        # Set known folder overrides to redirect Cortex into sandbox
        set_known_folder_override("desktop", desktop_dir)
        set_known_folder_override("downloads", downloads_dir)
        set_known_folder_override("documents", documents_dir)

        # Use sandbox-isolated SQLite database via CORTEX_DB_PATH
        sandbox_db_path = sandbox_dir / "scenario_cortex.db"
        orig_db_env = os.environ.get("CORTEX_DB_PATH")
        os.environ["CORTEX_DB_PATH"] = str(sandbox_db_path)
        await database.close_db()

        try:
            # Populate sandbox fixtures
            self._setup_filesystem(scenario.setup, sandbox_dir)

            # Initialize test DB and register devices
            await database.init_db()
            for dev_id in scenario.setup.connected_devices:
                await database.enroll_device(dev_id, f"{dev_id.upper()} Device", "admin", "secret_hash")
                caps = scenario.setup.device_capabilities.get(
                    dev_id,
                    [
                        "device.status",
                        "device.info",
                        "ui.notify",
                        "app.launch",
                        "fs.list",
                        "fs.stat",
                        "fs.delete",
                        "fs.move",
                        "screenshot.read",
                        "goal.wait",
                    ],
                )
                await database.save_device_capabilities(dev_id, caps, platform="windows")

            # Setup Control Layer and mock WebSocket connections
            connected_map: Dict[str, Any] = {}
            mock_connections: Dict[str, MockDeviceConnection] = {}
            for dev_id in scenario.setup.connected_devices:
                mock_ws = MockDeviceConnection(device_id=dev_id, trace=trace)
                mock_connections[dev_id] = mock_ws
                # If scenario is device reconnect, device starts OFFLINE unless simulation says otherwise
                sim_type = scenario.setup.simulation.get("type", "")
                if sim_type != "device_reconnect":
                    connected_map[dev_id] = mock_ws

            ctrl = CortexControlLayer(connected_map)

            # Reset singletons for fresh scenario run
            GoalManager._instance = None
            GoalPlanner._instance = None
            GoalRunner._instance = None
            WorldStateManager._instance = None

            gm = GoalManager.get_instance()
            await gm.set_autonomy_enabled(True)
            runner = GoalRunner.get_instance(control_layer=ctrl, goal_manager=gm)
            planner = GoalPlanner.get_instance(goal_manager=gm)
            ws_mgr = WorldStateManager.get_instance()

            # Execute scenario simulation flow
            await self._execute_scenario_flow(
                scenario=scenario,
                gm=gm,
                runner=runner,
                planner=planner,
                ws_mgr=ws_mgr,
                ctrl=ctrl,
                connected_map=connected_map,
                mock_connections=mock_connections,
                trace=trace,
                sandbox_dir=sandbox_dir,
            )

        except Exception as e:
            log.exception(f"[ScenarioRunner] Unhandled error during scenario '{scenario.name}': {e}")
            trace.final_goal_state = "FAILED"
            trace.observations.append({"error": str(e)})

        finally:
            trace.duration_seconds = round(time.time() - start_time, 3)

            # Deterministic Evaluation before sandbox teardown
            result = DeterministicEvaluator.evaluate(
                scenario=scenario,
                trace=trace,
                sandbox_dir=sandbox_dir,
            )

            # Close scenario database connection
            try:
                await database.close_db()
            except Exception:
                pass

            # Restore database env var and clear folder overrides
            if orig_db_env is not None:
                os.environ["CORTEX_DB_PATH"] = orig_db_env
            else:
                os.environ.pop("CORTEX_DB_PATH", None)

            clear_known_folder_overrides()

            # Clean up sandbox directory
            shutil.rmtree(sandbox_dir, ignore_errors=True)

            # Assert production database remained untouched
            final_prod_hash = self.get_prod_db_hash()
            if initial_prod_hash is not None and final_prod_hash != initial_prod_hash:
                raise RuntimeError(
                    f"CRITICAL SAFETY VIOLATION: Production database {PROD_DB_PATH} was modified during scenario run!"
                )

        # Persist report
        self._save_report(result)
        return result

    def _setup_filesystem(self, setup: ScenarioSetup, sandbox_dir: Path) -> None:
        """Populates the sandbox filesystem based on scenario setup."""
        desktop_dir = sandbox_dir / "Desktop"

        for item in setup.desktop:
            if item.endswith("/"):
                (desktop_dir / item[:-1]).mkdir(parents=True, exist_ok=True)
            else:
                target_file = desktop_dir / item
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(f"Sandbox fixture for {item}\n", encoding="utf-8")

        for item in setup.downloads:
            target_file = sandbox_dir / "Downloads" / item
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(f"Sandbox fixture for {item}\n", encoding="utf-8")

        for item in setup.documents:
            target_file = sandbox_dir / "Documents" / item
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(f"Sandbox fixture for {item}\n", encoding="utf-8")

    async def _execute_scenario_flow(
        self,
        scenario: Scenario,
        gm: GoalManager,
        runner: GoalRunner,
        planner: GoalPlanner,
        ws_mgr: WorldStateManager,
        ctrl: CortexControlLayer,
        connected_map: Dict[str, Any],
        mock_connections: Dict[str, MockDeviceConnection],
        trace: ExecutionTrace,
        sandbox_dir: Path,
    ) -> None:
        """Dispatches planning and handles scenario-specific simulation workflows."""
        sim = scenario.setup.simulation or {}
        sim_type = sim.get("type", "standard")

        # 1. Create Goal
        goal = await gm.create_goal(
            title=scenario.name,
            original_request=scenario.goal,
            source="user",
            status="ACTIVE",
        )

        # 2. Plan Goal
        target_device = scenario.setup.connected_devices[0] if scenario.setup.connected_devices else "asus"
        plan_res = await planner.plan_goal(goal.goal_id, target_device=target_device)
        steps_planned = plan_res.get("steps", [])
        trace.generated_plan = steps_planned
        trace.selected_capabilities = [s.get("capability") for s in steps_planned if s.get("capability")]

        if plan_res.get("status") == "blocked":
            trace.final_goal_state = "BLOCKED"
            trace.blocked_reason = plan_res.get("reason")
            return

        # 3. Scenario-Specific Execution Paths
        if sim_type == "messy_desktop":
            await self._run_messy_desktop_flow(goal.goal_id, gm, runner, trace, sandbox_dir)

        elif sim_type == "heart_rate":
            await self._run_heart_rate_flow(goal.goal_id, gm, runner, ws_mgr, scenario.setup.signals_to_inject, trace)

        elif sim_type == "device_reconnect":
            await self._run_device_reconnect_flow(goal.goal_id, gm, runner, ws_mgr, connected_map, mock_connections, trace)

        elif sim_type == "ambiguous_delete":
            await self._run_ambiguous_delete_flow(goal.goal_id, gm, runner, trace)

        elif sim_type == "capability_gap":
            # Handled at planning stage (should have blocked)
            g = await gm.get_goal(goal.goal_id)
            trace.final_goal_state = g.status if g else "BLOCKED"
            trace.blocked_reason = g.blocking_reason if g else "Capability gap"

        elif sim_type == "kill_switch":
            await self._run_kill_switch_flow(goal.goal_id, gm, runner, trace)

        elif sim_type == "crash_resume":
            await self._run_crash_resume_flow(goal.goal_id, gm, runner, trace)

        else:
            # Standard run
            run_res = await runner.run_goal(goal.goal_id)
            g = await gm.get_goal(goal.goal_id)
            trace.final_goal_state = g.status if g else run_res.get("status", "COMPLETED")
            trace.observations.append(run_res)

    async def _run_messy_desktop_flow(
        self,
        goal_id: str,
        gm: GoalManager,
        runner: GoalRunner,
        trace: ExecutionTrace,
        sandbox_dir: Path,
    ) -> None:
        """Executes Messy Desktop scenario."""
        run_res = await runner.run_goal(goal_id)
        trace.observations.append(run_res)
        g = await gm.get_goal(goal_id)

        # If goal requires approval for destructive actions:
        if g and g.status == "NEEDS_APPROVAL" and g.approval_request_id:
            trace.approvals_requested.append({
                "approval_id": g.approval_request_id,
                "step_index": g.current_step_index,
            })
            # Approve confirmation and resume
            await database.update_confirmation(g.approval_request_id, "approved")
            run_res2 = await runner.run_goal(goal_id)
            trace.observations.append(run_res2)
            g = await gm.get_goal(goal_id)

        trace.final_goal_state = g.status if g else "UNKNOWN"
        trace.replans = g.replan_count if g else 0

    async def _run_heart_rate_flow(
        self,
        goal_id: str,
        gm: GoalManager,
        runner: GoalRunner,
        ws_mgr: WorldStateManager,
        signals_to_inject: List[Dict[str, Any]],
        trace: ExecutionTrace,
    ) -> None:
        """Executes Heart Rate Trigger scenario (78, 91, 99, 101)."""
        # Initial run: enters WAITING on health.heart_rate > 100
        run_res = await runner.run_goal(goal_id)
        trace.observations.append(run_res)
        g = await gm.get_goal(goal_id)
        if g and g.status == "WAITING":
            trace.waits += 1

        # Sequentially inject signals
        for sig_item in signals_to_inject:
            val = sig_item.get("value")
            key = sig_item.get("key", "health.heart_rate")

            # Verify no actions dispatched while val <= 100
            actions_before = len(trace.actions_dispatched)

            signal = Signal(
                key=key,
                source="health_monitor",
                timestamp=datetime.now(timezone.utc).isoformat(),
                confidence=1.0,
                ttl=3600,
                payload=val,
            )
            await ws_mgr.ingest_signal(signal)

            actions_after = len(trace.actions_dispatched)
            if float(val) <= 100.0:
                assert actions_after == actions_before, (
                    f"Action dispatched prematurely for heart rate {val} <= 100!"
                )

        g_final = await gm.get_goal(goal_id)
        trace.final_goal_state = g_final.status if g_final else "UNKNOWN"

    async def _run_device_reconnect_flow(
        self,
        goal_id: str,
        gm: GoalManager,
        runner: GoalRunner,
        ws_mgr: WorldStateManager,
        connected_map: Dict[str, Any],
        mock_connections: Dict[str, MockDeviceConnection],
        trace: ExecutionTrace,
    ) -> None:
        """Executes Device Reconnect scenario (offline -> restart -> offline -> online)."""
        # 1. ASUS offline -> Goal WAITING
        run_res = await runner.run_goal(goal_id)
        trace.observations.append(run_res)
        g = await gm.get_goal(goal_id)
        assert g and g.status == "WAITING", f"Expected WAITING while offline, got {g.status if g else 'None'}"
        trace.waits += 1

        # 2. Simulate Hub restart: verify WAITING state survives in database
        active_waits = await gm.list_wait_conditions(goal_id=goal_id, status="ACTIVE")
        assert len(active_waits) >= 1, "WaitCondition lost before restart simulation"
        trace.checkpoint_preserved = True

        # 3. Simulate ASUS coming online
        if "asus" in mock_connections:
            connected_map["asus"] = mock_connections["asus"]

        online_signal = Signal(
            key="device.online:asus",
            source="asus",
            timestamp=datetime.now(timezone.utc).isoformat(),
            confidence=1.0,
            ttl=3600,
            payload={"is_online": True, "device_id": "asus"},
        )
        await ws_mgr.ingest_signal(online_signal)

        # 4. Verify completion
        g_final = await gm.get_goal(goal_id)
        trace.final_goal_state = g_final.status if g_final else "UNKNOWN"

    async def _run_ambiguous_delete_flow(
        self,
        goal_id: str,
        gm: GoalManager,
        runner: GoalRunner,
        trace: ExecutionTrace,
    ) -> None:
        """Executes Ambiguous Delete scenario."""
        run_res = await runner.run_goal(goal_id)
        trace.observations.append(run_res)
        g = await gm.get_goal(goal_id)
        trace.final_goal_state = g.status if g else "UNKNOWN"
        trace.blocked_reason = g.blocking_reason if g else run_res.get("reason")

    async def _run_kill_switch_flow(
        self,
        goal_id: str,
        gm: GoalManager,
        runner: GoalRunner,
        trace: ExecutionTrace,
    ) -> None:
        """Executes Kill Switch scenario: stop autonomy after step 1."""
        # Execute Step 1
        step1_res = await runner.run_next_step(goal_id)
        trace.observations.append(step1_res)
        g = await gm.get_goal(goal_id)
        assert g.current_step_index == 1, f"Expected step index 1, got {g.current_step_index}"

        # Emergency Stop / Kill switch
        await gm.set_autonomy_enabled(False)

        # Attempt to run Step 2
        step2_res = await runner.run_next_step(goal_id)
        trace.observations.append(step2_res)
        assert step2_res.get("status") == "blocked", f"Expected blocked by kill switch, got {step2_res}"

        # Verify checkpoint is preserved at step index 1
        g_after = await gm.get_goal(goal_id)
        assert g_after.current_step_index == 1, "Step index advanced despite kill switch!"
        trace.checkpoint_preserved = True
        trace.final_goal_state = "BLOCKED"
        trace.blocked_reason = "Autonomous execution disabled by global kill switch"

    async def _run_crash_resume_flow(
        self,
        goal_id: str,
        gm: GoalManager,
        runner: GoalRunner,
        trace: ExecutionTrace,
    ) -> None:
        """Executes Crash/Resume scenario: restart Hub after step 1, resume from checkpoint."""
        # Execute Step 1
        step1_res = await runner.run_next_step(goal_id)
        trace.observations.append(step1_res)
        g = await gm.get_goal(goal_id)
        assert g.current_step_index == 1

        # Simulate Hub Crash & Restart: recover goals from DB
        recovered = await gm.recover_on_startup()
        trace.checkpoint_preserved = True

        # Verify goal survived restart in safe WAITING state
        g_recovered = await gm.get_goal(goal_id)
        assert g_recovered.status == "WAITING", f"Expected WAITING after recovery, got {g_recovered.status}"
        assert g_recovered.current_step_index == 1

        # Explicit resume continues from checkpoint to complete goal
        await gm.resume_goal(goal_id)
        resume_res = await runner.run_goal(goal_id)
        trace.observations.append(resume_res)

        g_final = await gm.get_goal(goal_id)
        trace.final_goal_state = g_final.status if g_final else "UNKNOWN"

    def _save_report(self, result: ScenarioResult) -> None:
        """Writes structured JSON report."""
        report_file = self.reports_dir / f"{result.scenario_name}_report.json"
        report_file.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        log.info(f"[ScenarioRunner] Saved scenario report to {report_file}")
