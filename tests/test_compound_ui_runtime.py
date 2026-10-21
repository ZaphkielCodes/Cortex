"""Provider-independent mechanics. Invented UIs; no inference/generalization claims."""
import asyncio
from datetime import datetime, timezone, timedelta
import json

import pytest

from hub.agent_turn import AgentTurn
from hub.compound_ui import EvidenceCondition, evaluate
from hub.database import get_task
from test_agent_turn_runtime import ScriptedModel, call, control


def condition(label="Outcome", value="Ready"):
    return [{"source":"ui", "selector":{"label":label}, "path":["text"], "operator":"equals", "value":value}]


def element(identifier, label, text="", role="button", automation_id=None):
    return {"element_id":identifier, "role":role, "label":label, "text":text, "enabled":True,
            "bounds":[10,10,80,25], "metadata":{"automation_id":automation_id or label,
                "runtime_id":[42, len(label)], "editable":role=="input", "value_available":True}}


def scene(identifier, *elements, device="asus", window=42):
    return {"status":"success", "data":{"observation_id":identifier, "device_id":device,
        "window_id":window, "foreground_app":"Invented Quasar Studio", "window_title":"Workspace",
        "timestamp":datetime.now(timezone.utc).isoformat(), "screen_size":{"width":800,"height":600},
        "elements":list(elements)}}


def action(capability, *, goal=None, effect=None, **arguments):
    return dict(call(capability, **arguments), success_condition=goal or condition(), expected_effect=effect or condition())


async def snapshot(result):
    row=await get_task(result.task_id)
    return json.loads(row["context"])["compound_ui"]


@pytest.mark.asyncio
async def test_launch_observe_click_observe_select_verify_semantic_completion():
    goal=condition()
    opened=[{"source":"ui","selector":{"label":"Cobalt"},"operator":"exists"}]
    decisions=[{"decision":"PLAN","goal":"Prepare the workspace", "success_condition":goal,
        "plan_steps":[{"capability":"app.launch","arguments":{"app":"Quasar Studio"}}]},
        call("ui.observe"), action("ui.click", effect=opened,target="el_menu",observation_id="o1"),
        call("ui.observe"), action("ui.select",target="el_choice",value="Cobalt",observation_id="o2"),
        call("ui.observe"), {"decision":"RESPOND","response_text":"The workspace is ready."}]
    ctrl=control([{"status":"success","data":{"window_id":42}}, scene("o1",element("el_menu","Phases")),
        {"status":"success","data":{"input_attempted":True}},scene("o2",element("el_choice","Cobalt",role="listitem")),
        {"status":"success","data":{"input_attempted":True}},scene("o3",element("el_done","Outcome","Ready",role="text"))])
    result=await AgentTurn(ctrl, ScriptedModel(decisions)).run("Prepare the workspace", "asus", {})
    assert result.status=="success", result
    state=await snapshot(result)
    assert state["current_stage"]=="COMPLETED" and state["verification_result"]["verified"]
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list]==["app.launch","ui.observe","ui.click","ui.observe","ui.select","ui.observe"]
    stages={event["next_state"] for event in state["operational_trace"]}
    assert {"OBSERVING","REASONING","GROUNDING","EXECUTING","VERIFYING","COMPLETED"} <= stages
    assert all(event["task_id"]==result.task_id for event in state["operational_trace"])


@pytest.mark.asyncio
async def test_launch_observe_type_exact_readback_and_no_submit():
    goal=condition("Composer","private draft canary")
    before=element("el_input","Composer",role="input")
    after=element("el_fresh","Composer","private draft canary",role="input")
    model=ScriptedModel([dict(call("app.launch",app="Nebula Canvas"),continue_after_result=True,success_condition=goal),
        call("ui.observe"), action("ui.type",goal=goal,target="el_input",text="private draft canary",observation_id="o1"),
        call("ui.observe"),{"decision":"RESPOND","response_text":"The draft is present."}])
    ctrl=control([{"status":"success","data":{"window_id":42}},scene("o1",before),
        {"status":"success","data":{"input_attempted":True}},scene("o2",after)])
    result=await AgentTurn(ctrl,model).run("Prepare my draft without sending", "asus", {})
    assert result.status=="success", result
    state=await snapshot(result)
    assert state["verification_result"]["verified"]
    assert "private draft canary" not in json.dumps(state["operational_trace"])
    assert all(c.args[1] not in {"ui.keypress","ui.hotkey"} for c in ctrl.execute_and_observe.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("error",["stale_observation","target_not_found"])
async def test_no_input_failure_reobserves_and_replans(error):
    goal=condition()
    model=ScriptedModel([call("ui.observe"),action("ui.click",target="el_a",observation_id="o1"),
        call("ui.observe"),action("ui.click",target="el_b",observation_id="o2"),call("ui.observe"),
        {"decision":"RESPOND","response_text":"Ready."}])
    ctrl=control([scene("o1",element("el_a","Attempt A")),{"status":"failure","error":error,"data":{"input_attempted":False}},
        scene("o2",element("el_b","Alternative B")),{"status":"success","data":{"input_attempted":True}},
        scene("o3",element("el_status","Outcome","Ready"))])
    result=await AgentTurn(ctrl,model).run("Prepare the unknown panel", "asus", {})
    assert result.status=="success", result
    assert [c.args[2].get("target") for c in ctrl.execute_and_observe.await_args_list if c.args[1]=="ui.click"]==["el_a","el_b"]


@pytest.mark.asyncio
async def test_observation_change_and_model_done_are_not_semantic_completion():
    model=ScriptedModel([call("ui.observe"),action("ui.click",target="el_a",observation_id="o1"),
        call("ui.observe"),{"decision":"RESPOND","response_text":"Done"},
        {"decision":"FAIL_TRUTHFULLY","response_text":"The final state is unverified."}])
    ctrl=control([scene("o1",element("el_a","Attempt A")),{"status":"success"},
        scene("o2",element("el_unrelated","Different layout","Not ready"))])
    result=await AgentTurn(ctrl,model).run("Prepare the unknown panel", "asus", {})
    assert result.status!="success"
    assert not (await snapshot(result))["verification_result"].get("verified")


@pytest.mark.asyncio
async def test_equivalent_action_cannot_replay_after_ids_change():
    model=ScriptedModel([call("ui.observe"),action("ui.click",target="el_a",observation_id="o1"),
        call("ui.observe"),action("ui.click",target="el_99",observation_id="o2")])
    ctrl=control([scene("o1",element("el_a","Same control")),{"status":"success","data":{"input_attempted":True}},
        scene("o2",element("el_99","Same control"))])
    result=await AgentTurn(ctrl,model).run("Prepare the unknown panel", "asus", {})
    assert result.status=="error"
    assert sum(c.args[1]=="ui.click" for c in ctrl.execute_and_observe.await_args_list)==1


@pytest.mark.asyncio
async def test_ambiguous_target_replans_without_guessing_first_match():
    model=ScriptedModel([call("ui.observe"),action("ui.click",target="Duplicate",observation_id="o1"),
        {"decision":"ASK_CLARIFICATION","response_text":"Which duplicate control should I use?"}])
    ctrl=control([scene("o1",element("el_a","Duplicate"),element("el_b","Duplicate"))])
    result=await AgentTurn(ctrl,model).run("Choose the desired control", "asus", {})
    assert result.status=="clarification_needed"
    assert ctrl.execute_and_observe.await_count==1


@pytest.mark.asyncio
async def test_provider_fails_after_action_receipt_then_explicit_resume_observes_without_replay():
    from hub.llm_provider import ReasoningProviderUnavailable
    goal=condition()
    ctrl=control([scene("o1",element("el_a","Activate")),{"status":"success","command_id":"cmd-once","data":{"input_attempted":True}},
        scene("o2",element("el_done","Outcome","Ready"))])
    model=ScriptedModel([call("ui.observe"),action("ui.click",target="el_a",observation_id="o1"),
        ReasoningProviderUnavailable("PROVIDER_COOLDOWN")])
    first=await AgentTurn(ctrl,model).run("Prepare the unknown panel", "asus", {})
    state=await snapshot(first)
    assert first.status=="error" and state["current_stage"]=="WAITING_FOR_PROVIDER"
    assert state["execution_receipt"]["command_id"]=="cmd-once"
    assert state["execution_receipt"]["side_effects_possible"] and not state["execution_receipt"]["retry_safe"]
    resumed=await AgentTurn(ctrl,ScriptedModel([call("ui.observe"),{"decision":"RESPOND","response_text":"Ready."}])).resume(first.task_id,"asus")
    assert resumed.status=="success",resumed
    assert resumed.task_id==first.task_id
    assert sum(c.args[1]=="ui.click" for c in ctrl.execute_and_observe.await_args_list)==1
    denied=await AgentTurn(ctrl,ScriptedModel([])).resume(first.task_id,"other-device")
    assert denied.status=="denied"


@pytest.mark.asyncio
async def test_cancel_halfway_persists_inflight_receipt_and_terminal_state():
    ready=asyncio.Event()
    ctrl=control([])
    async def execute(device,capability,args):
        if capability=="ui.observe": return scene("o1",element("el_a","Activate"))
        ready.set()
        await asyncio.Event().wait()
    ctrl.execute_and_observe.side_effect=execute
    agent=AgentTurn(ctrl,ScriptedModel([call("ui.observe"),action("ui.click",target="el_a",observation_id="o1")]))
    agent.ACK_SECONDS=.001
    pending=await agent.run("Prepare the unknown panel","asus",{})
    await asyncio.wait_for(ready.wait(),2)
    job=AgentTurn._running[pending.task_id]
    AgentTurn.cancel_task(pending.task_id)
    final=await job
    await asyncio.gather(*list(AgentTurn._running.values()),return_exceptions=True)
    state=await snapshot(final)
    assert state["current_stage"]=="CANCELLED"
    assert state["execution_receipt"]["ran"]=="unverified"
    assert not state["execution_receipt"]["retry_safe"]


def test_condition_requires_unambiguous_trustworthy_fields_not_status_prose():
    with pytest.raises(ValueError): EvidenceCondition(source="capability",capability="ui.click",path=["status"],value="success")
    obs=scene("o1",element("el_a","Outcome","Ready"),element("el_b","Outcome","Ready"))["data"]
    assert not evaluate(condition(),obs,[],"asus")["verified"]


def test_native_freshness_expiry_foreground_and_window_identity(monkeypatch):
    from agent.ui_observer import UIObserver
    from agent.ui_models import UIObservation
    observer=UIObserver()
    data=scene("o1",element("el_a","Activate"))["data"]
    observation=UIObservation.from_dict(data)
    observer._store_observation(observation)
    monkeypatch.setattr(observer,"_get_active_window_info",lambda *args: {"window_id":42,"foreground_app":observation.foreground_app,"window_title":"Workspace"})
    assert observer.is_observation_fresh("o1")[0]
    observation.timestamp=(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()
    assert not observer.is_observation_fresh("o1")[0]
    observation.timestamp=datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(observer,"_get_active_window_info",lambda *args: {"window_id":99,"foreground_app":observation.foreground_app})
    assert not observer.is_observation_fresh("o1")[0]


def test_control_movement_is_checked_without_using_layout_count(monkeypatch):
    from agent.ui_observer import UIObserver
    from agent.ui_models import UIObservation, UIElement
    observer=UIObserver()
    obs=UIObservation.from_dict(scene("o1",element("el_a","Activate"))["data"])
    moved=UIElement.from_dict(element("el_new","Activate"))
    moved.bounds=[100,100,80,25]
    monkeypatch.setattr(observer,"_extract_accessibility_elements",lambda *args:[moved])
    assert not observer.element_is_current(obs,obs.elements[0])


@pytest.mark.asyncio
async def test_launch_dispatch_and_model_done_cannot_complete_ui_goal():
    model=ScriptedModel([{"decision":"PLAN","success_condition":condition(),"plan_steps":[
        {"capability":"app.launch","arguments":{"app":"Aurora Builder"}}]},
        {"decision":"RESPOND","response_text":"Done"},
        {"decision":"FAIL_TRUTHFULLY","response_text":"The final workspace was not observed."}])
    ctrl=control([{"status":"success","data":{"opened":True,"window_id":42}}])
    result=await AgentTurn(ctrl,model).run("Prepare the workspace","asus",{})
    assert result.status!="success"
    assert ctrl.execute_and_observe.await_count==1


@pytest.mark.asyncio
async def test_executor_disappears_after_observation_reconnect_requires_new_observation():
    from hub.executor_registry import ExecutorRegistry
    ctrl=control([])
    registry=ExecutorRegistry.get_instance()
    ready=asyncio.Event()
    async def execute(device,capability,arguments):
        if capability=="ui.observe":
            registry.set_device_online("asus",False)
            ready.set()
            return scene("o1",element("el_a","Activate"))
        raise AssertionError("No stale UI input may be dispatched after reconnect")
    ctrl.execute_and_observe.side_effect=execute
    agent=AgentTurn(ctrl,ScriptedModel([call("ui.observe"),action("ui.click",target="el_a",observation_id="o1"),
        {"decision":"ASK_CLARIFICATION","response_text":"The device changed; please confirm the current screen."}]))
    agent.ACK_SECONDS=.001
    pending=await agent.run("Prepare the unknown panel","asus",{})
    await asyncio.wait_for(ready.wait(),2)
    for _ in range(100):
        row=await get_task(pending.task_id)
        if json.loads(row["context"]).get("compound_ui",{}).get("current_stage")=="WAITING_FOR_EXECUTOR": break
        await asyncio.sleep(.01)
    assert json.loads(row["context"])["compound_ui"]["current_stage"]=="WAITING_FOR_EXECUTOR"
    registry.set_device_online("asus",True)
    final=await AgentTurn._running[pending.task_id]
    await asyncio.gather(*list(AgentTurn._running.values()),return_exceptions=True)
    assert final.status=="clarification_needed"
    assert ctrl.execute_and_observe.await_count==1


@pytest.mark.asyncio
async def test_approval_wait_persists_parent_identity_and_blocks_input():
    from hub.control_layer import CortexControlLayer
    from hub.main import PENDING_FUTURES
    from hub.database import get_pending_confirmations
    ctrl=control([scene("o1",element("el_d","Delete workspace")),{"status":"success"},
        scene("o2",element("el_done","Outcome","Ready"))])
    ctrl.request_approval=CortexControlLayer().request_approval
    agent=AgentTurn(ctrl,ScriptedModel([call("ui.observe"),action("ui.click",target="el_d",observation_id="o1"),
        call("ui.observe"),{"decision":"RESPOND","response_text":"Ready."}]))
    agent.ACK_SECONDS=.001
    pending=await agent.run("Change the workspace","asus",{"_request_task_id":"parent-fixture"})
    for _ in range(100):
        approvals=await get_pending_confirmations()
        if approvals: break
        await asyncio.sleep(.01)
    assert approvals
    state=await snapshot(pending)
    assert state["current_stage"]=="WAITING_FOR_APPROVAL" and state["parent_task_id"]=="parent-fixture"
    assert ctrl.execute_and_observe.await_count==1
    PENDING_FUTURES[approvals[0]["command_id"]].set_result("approve")
    final=await AgentTurn._running[pending.task_id]
    await asyncio.gather(*list(AgentTurn._running.values()),return_exceptions=True)
    assert final.status=="success",final
    state=await snapshot(final)
    assert all(event["parent_task_id"]=="parent-fixture" for event in state["operational_trace"])


@pytest.mark.asyncio
async def test_recovery_marks_inflight_unverified_and_resume_does_not_replay():
    from dataclasses import asdict
    from hub.compound_ui import CompoundState
    from hub.database import add_task
    from hub.task_orchestrator import TaskOrchestrator
    state=CompoundState(task_id="recovered-ui",goal="Prepare workspace",source_device="asus",active=True,
        success_condition=condition(),expected_effect=condition(),current_stage="EXECUTING",
        execution_receipt={"capability":"ui.click","status":"in_flight","side_effects_possible":True,
            "retry_safe":False,"verification_required":True,"ran":"unverified"})
    await add_task({"id":"recovered-ui","title":"Prepare workspace","type":"agent_turn","status":"RUNNING",
        "assigned_device":"asus","command":"agent_turn","context":{"request":"Prepare workspace","compound_ui":asdict(state)}})
    await TaskOrchestrator().reload_active_tasks()
    row=await get_task("recovered-ui")
    saved=json.loads(row["context"])
    assert saved["compound_ui"]["fresh_required"] and not saved["compound_ui"]["verification_result"]["verified"]
    ctrl=control([scene("fresh",element("el_done","Outcome","Ready"))])
    result=await AgentTurn(ctrl,ScriptedModel([call("ui.observe"),{"decision":"RESPOND","response_text":"Ready."}])).resume("recovered-ui","asus")
    assert result.status=="success",result
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list]==["ui.observe"]


def test_native_select_never_blindly_types_or_presses_enter(monkeypatch):
    from unittest.mock import Mock
    from agent.ui_action_executor import UIActionExecutor
    from agent.ui_observer import UIObserver
    from agent.ui_models import UIObservation
    observer=UIObserver.get_instance()
    observer._store_observation(UIObservation.from_dict(scene("o1",element("el_combo","Chooser",role="combobox"))["data"]))
    executor=UIActionExecutor()
    monkeypatch.setattr(executor,"_check_fencing",lambda *args:(True,None))
    monkeypatch.setattr(executor,"_ensure_desktop",lambda:None)
    executor._send_unicode_text=Mock()
    executor._send_vk_key=Mock()
    result=executor.select_item({"target":"el_combo","value":"Unobserved option","observation_id":"o1"})
    assert result["status"]=="failure" and not result["data"]["input_attempted"]
    executor._send_unicode_text.assert_not_called()
    executor._send_vk_key.assert_not_called()


@pytest.mark.asyncio
async def test_provider_cooldown_stages_are_operational_only():
    from hub.compound_ui import CompoundRuntime, CompoundState, provider_wait
    from hub.database import add_task
    await add_task({"id":"provider-wait","title":"Work","type":"agent_turn","status":"RUNNING","assigned_device":"asus","command":"agent_turn","context":{}})
    runtime=CompoundRuntime(CompoundState(task_id="provider-wait",goal="Work",source_device="asus"))
    await provider_wait("provider-wait",True)
    assert runtime.state.current_stage=="WAITING_FOR_PROVIDER"
    await provider_wait("provider-wait",False)
    assert runtime.state.current_stage=="REASONING"
    assert [event["next_state"] for event in runtime.state.operational_trace]==["WAITING_FOR_PROVIDER","REASONING"]


def test_absence_cannot_be_proven_from_partial_or_missing_observation():
    claim=[{"source":"ui","selector":{"label":"Busy"},"operator":"absent"}]
    obs=scene("o1",element("el_a","Outcome","Ready"))["data"]
    assert not evaluate(claim,obs,[],"asus")["verified"]
    obs["provenance"]={"elements_complete":True}
    assert evaluate(claim,obs,[],"asus")["verified"]


@pytest.mark.asyncio
async def test_resume_pending_text_verifies_its_field_before_next_field_without_replay():
    from hub.llm_provider import ReasoningProviderUnavailable
    goal=condition("Alpha", "first") + condition("Beta", "second")
    alpha=element("el_alpha", "Alpha", role="input")
    beta=element("el_beta", "Beta", role="input")
    written=element("el_alpha2", "Alpha", "first", role="input")
    final_beta=element("el_beta2", "Beta", "second", role="input")
    ctrl=control([scene("before",alpha,beta), {"status":"success","data":{"input_attempted":True}},
        scene("resume",written,beta), {"status":"success","data":{"input_attempted":True}},
        scene("final",written,final_beta)])
    first=await AgentTurn(ctrl,ScriptedModel([call("ui.observe"),
        dict(action("ui.type",goal=goal,target="el_alpha",text="first",clear_existing=True,observation_id="before"),expected_effect=[]),
        ReasoningProviderUnavailable("PROVIDER_COOLDOWN")])).run("Fill the two draft fields without submitting", "asus", {})
    stored=await snapshot(first)
    assert first.status=="error" and stored["pending_text_verification"]["asus"]["arguments"]["text"]=="first"
    assert stored["expected_effect"]==[]
    resumed=await AgentTurn(ctrl,ScriptedModel([call("ui.observe"),
        action("ui.type",goal=goal,target="el_beta",text="second",clear_existing=True,observation_id="resume"),
        call("ui.observe"),{"decision":"RESPOND","response_text":"Both fields are filled; nothing was submitted."}])).resume(first.task_id,"asus")
    assert resumed.status=="success",resumed
    assert resumed.task_id==first.task_id
    typed=[c.args[2]["text"] for c in ctrl.execute_and_observe.await_args_list if c.args[1]=="ui.type"]
    assert typed==["first","second"]
    state=await snapshot(resumed)
    assert state["pending_text_verification"]=={}
    assert "first" not in json.dumps(state["operational_trace"])


def test_restore_rejects_forged_owner_and_primary_target_never_uses_latest_observation(monkeypatch):
    from dataclasses import asdict
    from hub.compound_ui import CompoundState
    from hub.computer_use_controller import ComputerUseTaskController
    from agent.ui_action_executor import UIActionExecutor
    from agent.ui_observer import UIObserver
    state=asdict(CompoundState(task_id="original",goal="draft",source_device="asus"))
    with pytest.raises(ValueError,match="identity"):
        ComputerUseTaskController().agent_runtime("different","draft","asus",snapshot=state)
    observer=UIObserver.get_instance()
    monkeypatch.setattr(observer,"get_observation",lambda _:None)
    monkeypatch.setattr(observer,"get_latest_observation",lambda: (_ for _ in ()).throw(AssertionError("fallback forbidden")))
    grounded,obs=UIActionExecutor()._resolve_target("el_missing","expired")
    assert not grounded.success and obs is None


@pytest.mark.asyncio
async def test_duplicate_post_action_observation_and_changed_goal_fail_closed():
    from hub.compound_ui import CompoundRuntime,CompoundState
    runtime=CompoundRuntime(CompoundState(task_id="fixture",goal="draft",source_device="asus"))
    from unittest.mock import AsyncMock
    runtime.save=AsyncMock()
    runtime.declare(condition())
    with pytest.raises(ValueError,match="cannot change"):
        runtime.declare(condition(value="Other"))
    runtime.state.current_observation_id="same"
    runtime.state.execution_receipt={"verification_required":True}
    assert not await runtime.observed(scene("same",element("el_a","Outcome","Ready"))["data"])
    assert runtime.state.fresh_required and runtime.state.recovery_counts["VERIFICATION_FAILED"]==1
    with pytest.raises(ValueError):
        await runtime.transition("COMPLETED")



def test_exists_needs_unique_control_and_absence_needs_correct_device():
    duplicated=scene("o1",element("el_a","Busy"),element("el_b","Busy"))["data"]
    assert not evaluate([{"source":"ui","selector":{"label":"Busy"},"operator":"exists"}],duplicated,[],"asus")["verified"]
    other=scene("o2",element("el_a","Outcome","Ready"),device="other")["data"]
    other["provenance"]={"elements_complete":True}
    assert not evaluate([{"source":"ui","selector":{"label":"Busy"},"operator":"absent"}],other,[],"asus")["verified"]


def test_native_scroll_rejects_stale_or_missing_target_before_input(monkeypatch):
    from unittest.mock import Mock
    from agent.ui_action_executor import UIActionExecutor
    executor=UIActionExecutor()
    monkeypatch.setattr(executor,"_ensure_desktop",lambda:None)
    monkeypatch.setattr(executor,"_check_fencing",lambda *args:(False,"expired"))
    executor._send_mouse_event=Mock()
    executor._move_cursor=Mock()
    result=executor.scroll({"observation_id":"old","direction":"down"})
    assert result["status"]=="failure" and not result["data"]["input_attempted"]
    monkeypatch.setattr(executor,"_check_fencing",lambda *args:(True,None))
    from agent.ui_grounder import GroundingResult
    monkeypatch.setattr(executor,"_resolve_target",lambda *args,**kwargs:(GroundingResult(success=False),None))
    result=executor.scroll({"target":"missing","observation_id":"fresh"})
    assert result["status"]=="failure" and not result["data"]["input_attempted"]
    executor._send_mouse_event.assert_not_called()
    executor._move_cursor.assert_not_called()



def test_drag_replay_fingerprint_survives_reenumerated_endpoint_ids():
    from hub.compound_ui import CompoundRuntime,CompoundState
    runtime=CompoundRuntime(CompoundState(task_id="drag",goal="move",source_device="asus"))
    before=scene("o1",element("el_a","Source"),element("el_b","Destination"))["data"]
    after=scene("o2",element("el_c","Source"),element("el_d","Destination"))["data"]
    one=runtime.action_identity("ui.drag",{"from_target":"el_a","to_target":"el_b","observation_id":"o1"},before,"asus")
    two=runtime.action_identity("ui.drag",{"from_target":"el_c","to_target":"el_d","observation_id":"o2"},after,"asus")
    assert one==two
