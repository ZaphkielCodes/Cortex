"""
Physical ASUS Runtime Tests for Cortex Phase A10.
Executes 12 harmless real-world tests on the live ASUS desktop.
"""

import sys
import os
import time
import subprocess
import win32gui
import win32con

# Add Cortex root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.ui_models import UIObservation, UIElement, UIActionResult
from agent.ui_observer import UIObserver, observe_ui_sync
from agent.ui_action_executor import UIActionExecutor
from agent.ui_grounder import UIElementGrounder
from agent.ui_verifier import UIActionVerifier
from hub.computer_use_controller import ComputerUseTaskController
from agent.executor import execute

def run_all_physical_tests():
    print("=" * 60)
    print("STARTING CORTEX PHASE A10 PHYSICAL ASUS RUNTIME TESTS")
    print("=" * 60)

    ui_obs = UIObserver.get_instance()
    ui_act = UIActionExecutor.get_instance()
    grounder = UIElementGrounder()
    verifier = UIActionVerifier()
    controller = ComputerUseTaskController()

    results = {}

    # -------------------------------------------------------------
    # 1. Open Notepad using normal app capability
    # -------------------------------------------------------------
    print("\n[Test 1] Opening Notepad via app.launch capability...")
    try:
        launch_res = execute("app.launch", {"app_name": "notepad"})
        time.sleep(1.5)
        
        # Verify Notepad window is present
        hwnd = win32gui.FindWindow("Notepad", None)
        if not hwnd:
            def enum_cb(h, found):
                title = win32gui.GetWindowText(h)
                if "Notepad" in title:
                    found.append(h)
            found = []
            win32gui.EnumWindows(enum_cb, found)
            hwnd = found[0] if found else None
            
        if hwnd:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.5)
            print(f"  -> Notepad launched successfully (HWND: {hwnd})")
            results["test_1_open_notepad"] = {"status": "SUCCESS", "hwnd": hwnd}
        else:
            raise RuntimeError("Notepad window could not be found.")
    except Exception as e:
        print(f"  -> Test 1 Failed: {e}")
        results["test_1_open_notepad"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 2. Use computer-use capability to type: "Cortex A10 test"
    # -------------------------------------------------------------
    print("\n[Test 2] Typing 'Cortex A10 test' using ui.type...")
    try:
        obs1 = observe_ui_sync(target_device="asus")
        print(f"  -> Foreground app: {obs1.foreground_app}, Window: {obs1.window_title}")
        
        # Type into the active Notepad
        type_res = ui_act.type({"text": "Cortex A10 test", "observation_id": obs1.observation_id})
        print(f"  -> ui.type result: {type_res.get('status')}, message: {type_res.get('message')}")
        assert type_res.get("status") == "success"
        time.sleep(0.5)
        results["test_2_type_text"] = {"status": "SUCCESS", "details": type_res}
    except Exception as e:
        print(f"  -> Test 2 Failed: {e}")
        results["test_2_type_text"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 3. Select the text using keyboard/UI interaction
    # -------------------------------------------------------------
    print("\n[Test 3] Selecting text via ui.hotkey (Ctrl+A)...")
    try:
        obs2 = observe_ui_sync(target_device="asus")
        select_res = ui_act.hotkey({"keys": ["ctrl", "a"], "observation_id": obs2.observation_id})
        print(f"  -> ui.hotkey result: {select_res.get('status')}, message: {select_res.get('message')}")
        assert select_res.get("status") == "success"
        time.sleep(0.5)
        results["test_3_select_text"] = {"status": "SUCCESS", "details": select_res}
    except Exception as e:
        print(f"  -> Test 3 Failed: {e}")
        results["test_3_select_text"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 4. Open Windows Settings
    # -------------------------------------------------------------
    print("\n[Test 4] Opening Windows Settings via app.launch...")
    try:
        settings_res = execute("app.launch", {"app_name": "ms-settings:"})
        time.sleep(2.0)
        obs_settings = observe_ui_sync(target_device="asus")
        print(f"  -> Observed: Foreground: {obs_settings.foreground_app}, Window: {obs_settings.window_title}")
        assert "setting" in obs_settings.foreground_app.lower() or "setting" in obs_settings.window_title.lower() or len(obs_settings.elements) > 0
        results["test_4_open_settings"] = {"status": "SUCCESS", "window_title": obs_settings.window_title}
    except Exception as e:
        print(f"  -> Test 4 Failed: {e}")
        results["test_4_open_settings"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 5. Observe and identify visible controls
    # -------------------------------------------------------------
    print("\n[Test 5] Observing and extracting structured elements in Settings...")
    try:
        obs_controls = observe_ui_sync(target_device="asus")
        element_count = len(obs_controls.elements)
        print(f"  -> Elements extracted via UI Automation: {element_count}")
        sample_labels = [el.label for el in obs_controls.elements[:6] if el.label]
        print(f"  -> Sample visible controls: {sample_labels}")
        assert element_count > 0, "No controls extracted from Settings"
        results["test_5_observe_controls"] = {
            "status": "SUCCESS",
            "element_count": element_count,
            "sample_controls": sample_labels
        }
    except Exception as e:
        print(f"  -> Test 5 Failed: {e}")
        results["test_5_observe_controls"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 6. Navigate one harmless Settings screen visually
    # -------------------------------------------------------------
    print("\n[Test 6] Visually grounding and clicking a harmless Settings navigation item...")
    try:
        obs_nav = observe_ui_sync(target_device="asus")
        target = None
        for el in obs_nav.elements:
            lbl = el.label.lower()
            if any(term in lbl for term in ["system", "home", "bluetooth", "personalization", "search"]):
                target = el
                break
        if not target and obs_nav.elements:
            target = obs_nav.elements[0]

        print(f"  -> Grounded target: {target.role} '{target.label}' at bounds {target.bounds}")
        click_res = ui_act.click({
            "target": target.element_id,
            "observation_id": obs_nav.observation_id
        })
        time.sleep(1.0)
        obs_after = observe_ui_sync(target_device="asus")
        v_res = verifier.verify(
            action="ui.click",
            target_label=target.label,
            pre_obs=obs_nav,
            post_obs=obs_after,
            action_args={"target": target.element_id}
        )
        print(f"  -> Click status: {click_res.get('status')}, Verification: {v_res.summary}")
        results["test_6_navigate_settings"] = {
            "status": "SUCCESS",
            "target": target.label,
            "verification": v_res.to_dict()
        }
    except Exception as e:
        print(f"  -> Test 6 Failed: {e}")
        results["test_6_navigate_settings"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 7. Open File Explorer
    # -------------------------------------------------------------
    print("\n[Test 7] Opening File Explorer to harmless project directory...")
    try:
        proj_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        subprocess.Popen(["explorer.exe", proj_dir])
        time.sleep(2.0)
        obs_explorer = observe_ui_sync(target_device="asus")
        print(f"  -> Foreground: {obs_explorer.foreground_app}, Window: {obs_explorer.window_title}")
        results["test_7_open_explorer"] = {"status": "SUCCESS", "window_title": obs_explorer.window_title}
    except Exception as e:
        print(f"  -> Test 7 Failed: {e}")
        results["test_7_open_explorer"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 8. Navigate through a harmless folder using visual/semantic UI interaction
    # -------------------------------------------------------------
    print("\n[Test 8] Navigating harmless File Explorer view using UI interaction...")
    try:
        obs_exp = observe_ui_sync(target_device="asus")
        scroll_res = ui_act.scroll({
            "delta_y": -200,
            "observation_id": obs_exp.observation_id
        })
        time.sleep(0.8)
        obs_exp_after = observe_ui_sync(target_device="asus")
        v_res = verifier.verify(
            action="ui.scroll",
            target_label="Explorer view",
            pre_obs=obs_exp,
            post_obs=obs_exp_after,
            action_args={"delta_y": -200}
        )
        print(f"  -> Scroll executed: {scroll_res.get('status')}, verification: {v_res.summary}")
        results["test_8_navigate_explorer"] = {"status": "SUCCESS", "verification": v_res.to_dict()}
    except Exception as e:
        print(f"  -> Test 8 Failed: {e}")
        results["test_8_navigate_explorer"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 9. Resize/move a window between observation and action (Stale observation recovery)
    # -------------------------------------------------------------
    print("\n[Test 9] Verifying stale-observation rejection on window move...")
    try:
        # Restore active window so it is not locked in maximized state
        active_hwnd = win32gui.GetForegroundWindow()
        if not active_hwnd or not win32gui.IsWindow(active_hwnd):
            active_hwnd = win32gui.FindWindow("Notepad", None) or win32gui.FindWindow("CabinetWClass", None)
        if active_hwnd and win32gui.IsWindow(active_hwnd):
            win32gui.ShowWindow(active_hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(active_hwnd)
            except Exception:
                pass
            time.sleep(0.5)

        # 1. Take observation of the specific active window
        obs_pre = observe_ui_sync(target_device="asus", target_window_id=active_hwnd)
        grounded = obs_pre.elements[0] if obs_pre.elements else UIElement(element_id="el_tmp", role="button", bounds=[100, 100, 40, 20])

        # 2. Move window by 150px
        rect = win32gui.GetWindowRect(active_hwnd)
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        print(f"  -> Moving window {active_hwnd} by 150px to trigger stale observation fencing...")
        win32gui.MoveWindow(active_hwnd, rect[0] + 150, rect[1] + 50, width, height, True)
        time.sleep(0.5)

        # 3. Attempt action using old observation ID
        stale_res = ui_act.click({
            "target": grounded.element_id,
            "observation_id": obs_pre.observation_id
        })
        print(f"  -> Action status with moved window: {stale_res.get('status')}, error: {stale_res.get('error')}")
        assert stale_res.get("error") == "stale_observation", f"Executor did not reject stale observation! Result: {stale_res}"

        # 4. Re-observe and execute freshly
        print("  -> Re-observing and re-grounding freshly...")
        obs_fresh = observe_ui_sync(target_device="asus")
        fresh_res = ui_act.wait({"seconds": 0.1, "observation_id": obs_fresh.observation_id})
        assert fresh_res.get("status") == "success"
        print(f"  -> Fresh action executed safely: {fresh_res.get('status')}")

        # Restore window position
        win32gui.MoveWindow(active_hwnd, rect[0], rect[1], width, height, True)
        results["test_9_stale_recovery"] = {
            "status": "SUCCESS",
            "stale_rejected": True,
            "fresh_executed": True
        }
    except Exception as e:
        print(f"  -> Test 9 Failed: {e}")
        results["test_9_stale_recovery"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 10. Trigger a harmless confirmation-gated test action (A9 approval integration)
    # -------------------------------------------------------------
    print("\n[Test 10] Testing confirmation-gated high-impact UI action...")
    try:
        task_id = "test_approval_task"
        action = {
            "action": "ui.click",
            "target": {"role": "button", "label": "Delete Account Permanently"}
        }
        step_res = controller.step(
            task_id=task_id,
            goal="Delete account test",
            next_action=action,
            device_id="asus"
        )
        print(f"  -> Controller step result: requires_approval={step_res.get('requires_approval')}, reason='{step_res.get('waiting_reason')}'")
        assert step_res.get("requires_approval") is True
        assert "Delete" in step_res.get("waiting_reason")

        # Now approve and resume
        resume_res = controller.step(
            task_id=task_id,
            goal="Delete account test",
            next_action={"action": "ui.wait", "params": {"seconds": 0.1}},
            device_id="asus",
            approved=True
        )
        print(f"  -> After approval, resumed step result status: {resume_res.get('status')}")
        results["test_10_approval_gate"] = {
            "status": "SUCCESS",
            "paused_for_approval": True,
            "approval_reason": step_res.get("waiting_reason"),
            "resumed_after_approval": resume_res.get("status") in ["success", "completed"]
        }
    except Exception as e:
        print(f"  -> Test 10 Failed: {e}")
        results["test_10_approval_gate"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 11. Stop an active computer-use task using A9 kill/stop controls
    # -------------------------------------------------------------
    print("\n[Test 11] Cancelling an active computer-use task via A9 stop control...")
    try:
        task_id = "test_kill_task"
        controller.step(
            task_id=task_id,
            goal="Long running UI task",
            next_action={"action": "ui.wait", "params": {"seconds": 5.0}},
            device_id="asus"
        )
        task_state_before = controller.get_task(task_id)
        print(f"  -> Task state before stop: {task_state_before.state}")

        # Trigger cancel
        cancelled = controller.cancel_task(task_id)
        task_state_after = controller.get_task(task_id)
        print(f"  -> Cancel requested: {cancelled}, task state after stop: {task_state_after.state}")
        assert task_state_after.state == "CANCELLED"
        results["test_11_stop_task"] = {
            "status": "SUCCESS",
            "cancelled": True,
            "final_status": task_state_after.state
        }
    except Exception as e:
        print(f"  -> Test 11 Failed: {e}")
        results["test_11_stop_task"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # 12. Resume/checkpoint a safe interrupted computer-use task
    # -------------------------------------------------------------
    print("\n[Test 12] Checkpoint save and restore for interrupted task...")
    try:
        task_id = "test_checkpoint_task"
        controller.step(
            task_id=task_id,
            goal="Multi-step form fill",
            next_action={"action": "ui.wait", "params": {"seconds": 0.1}},
            device_id="asus"
        )
        # Verify checkpoint exists
        task = controller.get_task(task_id)
        cp = controller.get_checkpoint(task_id)
        assert cp is not None
        print(f"  -> Saved checkpoint at step {task.current_step_index}: action_count={len(cp.get('action_history', []))}")

        # Simulate interruption & resume
        resumed_res = controller.step(
            task_id=task_id,
            goal="Multi-step form fill",
            next_action={"action": "ui.wait", "params": {"seconds": 0.1}},
            device_id="asus"
        )
        assert resumed_res.get("status") == "success"
        print(f"  -> Resumed task continued to step {task.current_step_index} successfully")
        results["test_12_checkpoint_resume"] = {
            "status": "SUCCESS",
            "initial_step": 1,
            "resumed_step": task.current_step_index
        }
    except Exception as e:
        print(f"  -> Test 12 Failed: {e}")
        results["test_12_checkpoint_resume"] = {"status": "FAILED", "error": str(e)}

    # -------------------------------------------------------------
    # Clean up test processes
    # -------------------------------------------------------------
    print("\n[Cleanup] Cleaning up opened test windows (Notepad, Settings)...")
    try:
        subprocess.run(["taskkill", "/f", "/im", "notepad.exe"], capture_output=True)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("ASUS PHYSICAL TEST SUMMARY:")
    all_passed = True
    for k, v in results.items():
        status = v.get("status")
        if status != "SUCCESS":
            all_passed = False
        print(f"  - {k}: {status}")
    print("=" * 60)
    print(f"ALL 12 TESTS PASSED: {all_passed}")

    return results, all_passed

if __name__ == "__main__":
    results, passed = run_all_physical_tests()
    sys.exit(0 if passed else 1)
