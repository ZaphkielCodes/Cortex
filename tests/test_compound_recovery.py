"""General recovery fixtures; invented applications and controls, no external calls."""
from datetime import datetime, timezone
import json
import pytest
from hub.agent_turn import AgentTurn
from hub.database import get_task
from test_agent_turn_runtime import ScriptedModel, call, control
from test_compound_ui_runtime import action, condition, element, scene


def observed(identifier, *elements, window=42, pid=100, app="Invented Aster Desk"):
    result = scene(identifier,*elements,window=window)
    result["data"].update(pid=pid,process_name=app,foreground_app=app)
    return result


def launched(window=42,pid=100,app="Invented Aster Desk"):
    return {"status":"success","data":{"window_id":window,"pid":pid,"process_name":app,"app_name":app}}


def state(model, index=-1):
    return json.loads(model.calls[index][1]["system_prompt"].split("\nSTATE:\n")[1])


async def saved(result):
    return json.loads((await get_task(result.task_id))["context"])


@pytest.mark.asyncio
async def test_unchanged_ui_rejects_semantic_duplicate_reobserves_and_alternate_succeeds():
    model=ScriptedModel([call("ui.observe"),action("ui.click",target="el_a",observation_id="o1"),
        call("ui.observe"),action("ui.click",target="el_b",observation_id="o2"),
        action("ui.click",target="el_alternate",observation_id="o3"),call("ui.observe"),
        {"decision":"RESPOND","response_text":"The requested state is verified."}])
    ctrl=control([observed("o1",element("el_a","Same action")),{"status":"success","command_id":"first-once"},
        observed("o2",element("el_b","Same action")),
        observed("o3",element("el_c","Same action"),element("el_alternate","Other action")),
        {"status":"success","command_id":"alternate-once"},observed("o4",element("el_final","Outcome","Ready"))])
    result=await AgentTurn(ctrl,model).run("Prepare the invented workspace","asus",{})
    assert result.status=="success",result
    clicks=[c.args[2]["target"] for c in ctrl.execute_and_observe.await_args_list if c.args[1]=="ui.click"]
    assert clicks==["el_a","el_alternate"]
    snapshot=(await saved(result))["compound_ui"]
    assert snapshot["recovery_counts"]["NO_PROGRESS"]==2
    assert any(e["observed_effect"].get("meaningful_ui_change") is False for e in snapshot["recovery_evidence"])
    assert state(model)["compound_task"]["verification_result"]["verified"]


@pytest.mark.asyncio
async def test_wrong_foreground_contents_never_reach_model_and_intended_target_reacquired():
    goal=condition()
    model=ScriptedModel([dict(call("app.launch",app="Aster Desk"),continue_after_result=True,success_condition=goal),
        call("ui.observe"),{"decision":"RESPOND","response_text":"The intended workspace is ready."}])
    ctrl=control([launched(),observed("wrong",element("el_secret","UNRELATED-PRIVATE-CANARY"),window=99,pid=200,app="Invented Other App"),
        launched(),observed("right",element("el_done","Outcome","Ready"))])
    result=await AgentTurn(ctrl,model).run("Prepare the invented workspace","asus",{})
    assert result.status=="success",result
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list]==["app.launch","ui.observe","window.focus","ui.observe"]
    assert ctrl.execute_and_observe.await_args_list[1].args[2]["target_window_id"]==42
    assert "UNRELATED-PRIVATE-CANARY" not in json.dumps(model.calls)
    assert "UNRELATED-PRIVATE-CANARY" not in json.dumps(await saved(result))
    assert (await saved(result))["compound_ui"]["recovery_counts"]["TARGET_CONTEXT_MISMATCH"]==1


@pytest.mark.asyncio
async def test_recreated_window_reacquires_canonical_process_after_old_handle_disappears():
    model=ScriptedModel([{"decision":"PLAN","success_condition":condition(),"plan_steps":[{"capability":"app.launch","arguments":{"app":"Aster Desk"}}]},
        call("ui.observe"),{"decision":"RESPOND","response_text":"The replacement window is verified."}])
    missing={"status":"failure","error":"window_not_found","data":{"input_attempted":False}}
    ctrl=control([launched(),missing,missing,launched(window=57,pid=101),observed("new",element("el_done","Outcome","Ready"),window=57,pid=101)])
    result=await AgentTurn(ctrl,model).run("Prepare the invented workspace","asus",{})
    assert result.status=="success",result
    calls=ctrl.execute_and_observe.await_args_list
    assert calls[2].args[2]["window_id"]==42 and calls[2].args[2]["pid"]==100
    assert calls[3].args[2]["process"]=="Invented Aster Desk"
    assert calls[4].args[2]["target_window_id"]==57
    assert (await saved(result))["compound_ui"]["expected_targets"]["asus"]["pid"]==101
    assert sum(c.args[1]=="app.launch" for c in calls)==1


@pytest.mark.asyncio
async def test_truly_disappeared_target_returns_evidence_and_allows_explicit_relaunch():
    launch=dict(call("app.launch",app="Aster Desk"),continue_after_result=True,success_condition=condition())
    model=ScriptedModel([launch,call("ui.observe"),launch,call("ui.observe"),{"decision":"RESPOND","response_text":"The new window is verified."}])
    missing={"status":"failure","error":"window_not_found","data":{"input_attempted":False}}
    ctrl=control([launched(),missing,missing,missing,launched(window=58,pid=102),observed("new",element("el_done","Outcome","Ready"),window=58,pid=102)])
    result=await AgentTurn(ctrl,model).run("Prepare the invented workspace","asus",{})
    assert result.status=="success",result
    assert state(model,2)["validation_feedback"]["category"]=="TARGET_DISAPPEARED"
    assert sum(c.args[1]=="app.launch" for c in ctrl.execute_and_observe.await_args_list)==2
    assert not (await saved(result))["compound_ui"]["relaunch_permits"]


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal,category",[(call("imaginary.change"),"CAPABILITY_REJECTED"),
    (call("app.launch",app=47),"ARGUMENT_SCHEMA_REJECTED"),
    (call("app.launch",app="Orchid Pane",unknown_argument=True),"ARGUMENT_SCHEMA_REJECTED")])
async def test_rejected_proposal_supplies_canonical_schema_then_corrected_call_succeeds(proposal,category):
    model=ScriptedModel([proposal,call("app.launch",app="Orchid Pane")])
    ctrl=control([{"status":"success","data":{"opened":True}}])
    result=await AgentTurn(ctrl,model).run("Prepare the invented pane","asus",{})
    assert result.status=="success",result
    ctrl.execute_and_observe.assert_awaited_once()
    feedback=state(model,1)["validation_feedback"]
    assert feedback["category"]==category and "app.launch" in feedback["eligible_capabilities"]
    if category=="ARGUMENT_SCHEMA_REJECTED":
        assert feedback["canonical_schema"]["arguments"]["app"]["required"]
    assert (await saved(result))["compound_ui"]["recovery_counts"][category]==1


@pytest.mark.asyncio
async def test_unavailable_executor_capability_is_rejected_before_execution():
    from hub.executor_registry import ExecutorRegistry
    ctrl=control([{"status":"success","data":{"opened":True}}])
    handle=ExecutorRegistry.get_instance().get_device_handle("asus")
    handle.capabilities=[c for c in handle.capabilities if c!="ui.click"]
    model=ScriptedModel([call("ui.click",target="invented",observation_id="invented"),call("app.launch",app="Orchid Pane")])
    result=await AgentTurn(ctrl,model).run("Prepare the invented pane","asus",{})
    assert result.status=="success",result
    assert state(model,1)["validation_feedback"]["category"]=="CAPABILITY_REJECTED"
    ctrl.execute_and_observe.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_invalid_capabilities_stop_after_one_correction():
    model=ScriptedModel([call("imaginary.change")]*5)
    ctrl=control([])
    result=await AgentTurn(ctrl,model).run("Prepare the invented pane","asus",{})
    assert result.status=="error" and result.data["error_code"]=="corrective_retry_exhausted"
    ctrl.execute_and_observe.assert_not_awaited()
    assert len(model.calls)==2


@pytest.mark.asyncio
async def test_corrective_provider_failure_keeps_receipts_without_recipient_change():
    from test_a10_5_routing import FakeProvider,router,error
    from hub.privacy import PrivacyPolicyEngine
    PrivacyPolicyEngine.get_instance().policy.sensitive_data_cloud_allowed=True
    goal=[{"source":"capability","capability":"app.launch","path":["data","opened"],"value":True}]
    proposal=dict(call("app.launch",app="Invented Meridian"),continue_after_result=True,success_condition=goal)
    initial=[proposal,call("imaginary.change"),error(503)]
    primary=FakeProvider("alpha","one",*initial,configured_priority=1)
    alternate=FakeProvider("beta","two",proposal,
        {"decision":"RESPOND","response_text":"The final state is verified."})
    ctrl=control([{"status":"success","command_id":"receipt-once","data":{"opened":True}},
        observed("o1",element("el_done","Outcome","Ready"))])
    result=await AgentTurn(ctrl,router(primary,alternate)).run("Prepare the invented pane","asus",{"_request_task_id":"parent-marker"})
    assert result.status=="error" and result.data["error_code"]=="model_provider_unavailable"
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list]==["app.launch"]
    assert not alternate.received
    assert "receipt-once" in json.dumps(primary.received)
    snapshot=await saved(result)
    assert snapshot["compound_ui"]["parent_task_id"]=="parent-marker"
    assert all(e["task_id"]==result.task_id for e in snapshot["routing_events"])


@pytest.mark.asyncio
async def test_failure_classes_have_independent_persisted_budgets():
    from hub.compound_ui import CompoundRuntime, CompoundState
    from hub.compound_recovery import RecoveryExhausted
    from unittest.mock import AsyncMock
    runtime=CompoundRuntime(CompoundState(task_id="fixture",goal="fixture",source_device="asus"))
    runtime.save=AsyncMock()
    for _ in range(3):
        await runtime.recover("NO_PROGRESS")
        await runtime.recover("CAPABILITY_REJECTED")
    assert runtime.state.retry_count==0
    assert runtime.state.recovery_counts=={"NO_PROGRESS":3,"CAPABILITY_REJECTED":3}
    with pytest.raises(RecoveryExhausted):
        await runtime.recover("NO_PROGRESS")


def test_pid_reuse_and_wrong_device_are_rejected_despite_matching_handle():
    from hub.compound_recovery import observation_problem
    expected={"device_id":"asus","window_id":42,"pid":100,"process_name":"Invented Aster Desk"}
    assert observation_problem(observed("fresh",pid=200)["data"],expected,"asus")=="TARGET_CONTEXT_MISMATCH"
    changed=observed("fresh")["data"]
    changed["device_id"]="another-device"
    assert observation_problem(changed,expected,"asus")=="TARGET_CONTEXT_MISMATCH"


def test_hosted_content_upgrade_requires_canonical_os_ownership_proof():
    from hub.compound_recovery import observation_problem
    expected={"device_id":"asus","window_id":42,"pid":10,"process_name":"Invented Window Host"}
    data=observed("content",pid=100)["data"]
    data["provenance"]={"window_identity":{"pid":100,"host_pid":10,"host_app":"Invented Window Host",
        "identity_source":"hosted_content_process"}}
    assert observation_problem(data,expected,"asus") is None
    data["provenance"]["window_identity"]["host_pid"]=11
    assert observation_problem(data,expected,"asus")=="TARGET_CONTEXT_MISMATCH"


@pytest.mark.asyncio
async def test_window_ownership_race_discards_tree_before_return(monkeypatch):
    from agent.ui_observer import UIObserver
    observer=UIObserver()
    windows=iter([{"window_id":42,"pid":100,"foreground_app":"Invented Aster Desk"},
                  {"window_id":42,"pid":200,"foreground_app":"Invented Other App"}])
    monkeypatch.setattr(observer,"_get_active_window_info",lambda *_:next(windows))
    monkeypatch.setattr(observer,"get_screen_size",lambda:{"width":800,"height":600})
    monkeypatch.setattr(observer,"_extract_accessibility_elements",lambda *_:[element("secret","UNRELATED-CANARY")])
    result=await observer.observe(target_device="asus",include_tree=True)
    assert result.elements==[] and result.provenance["identity_consistent"] is False
    assert "UNRELATED-CANARY" not in json.dumps(result.to_dict())


def test_protocol_equivalent_pointer_calls_share_semantic_action_identity():
    from hub.compound_ui import CompoundRuntime, CompoundState
    runtime=CompoundRuntime(CompoundState(task_id="fixture",goal="fixture",source_device="asus"))
    observation=observed("o1",element("el_a","Control"))["data"]
    base=runtime.action_identity("ui.click",{"target":"el_a","observation_id":"o1"},observation,"asus")
    explicit=runtime.action_identity("ui.click",{"target":"el_a","observation_id":"new","button":"left","target_device":"asus"},observation,"asus")
    assert base==explicit
    assert runtime.action_identity("ui.click",{"target":"el_a","button":"right"},observation,"asus")==runtime.action_identity("ui.right_click",{"target":"el_a"},observation,"asus")


def test_text_readback_cannot_verify_against_recycled_window_in_other_process():
    from agent.ui_models import UIObservation
    from agent.ui_text import verify_text_entry
    before=UIObservation.from_dict(observed("before",element("el_field","Draft",role="input"))["data"])
    after=UIObservation.from_dict(observed("after",element("el_field","Draft","hello",role="input"),pid=200)["data"])
    assert not verify_text_entry(before,after,{"target":"el_field","text":"hello"})


def test_phantom_controls_without_window_cannot_be_used_as_completion_evidence():
    from hub.compound_recovery import observation_problem
    phantom=observed("phantom",element("el_fake","Outcome","Ready"))["data"]
    phantom["window_id"]=None
    assert observation_problem(phantom,None,"asus",require_window=True)=="TARGET_DISAPPEARED"


@pytest.mark.asyncio
async def test_invalid_plan_is_corrected_before_freezing_goal_or_dispatching_steps():
    bad={"decision":"PLAN","success_condition":condition(),
        "plan_steps":[{"capability":"imaginary.launch","arguments":{}}]}
    model=ScriptedModel([bad,dict(call("app.launch",app="Orchid Pane"),continue_after_result=True,success_condition=condition()),
        call("ui.observe"),{"decision":"RESPOND","response_text":"The goal is verified."}])
    ctrl=control([launched(),observed("final",element("el_done","Outcome","Ready"))])
    result=await AgentTurn(ctrl,model).run("Prepare the invented pane","asus",{})
    assert result.status=="success",result
    assert state(model,1)["validation_feedback"]["category"]=="CAPABILITY_REJECTED"
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list]==["app.launch","ui.observe"]


def test_opaque_legacy_checkpoint_cannot_replay_last_action_after_identity_upgrade():
    from hub.compound_ui import CompoundRuntime, CompoundState
    runtime=CompoundRuntime(CompoundState(task_id="fixture",goal="fixture",source_device="asus"))
    runtime.state.action_history=[{"fingerprint":"old-format-opaque","verified":False,"recovered":True}]
    runtime.state.execution_receipt={"capability":"ui.click","side_effects_possible":True,"retry_safe":False}
    observation=observed("fresh",element("el_new","Control"),window=57,pid=101)["data"]
    assert runtime.duplicate("ui.click",{"target":"el_new","observation_id":"fresh"},observation,"asus")
    assert not runtime.duplicate("ui.observe",{},observation,"asus")


@pytest.mark.asyncio
async def test_rate_limit_coded_413_requires_verified_context_capacity():
    from test_a10_5_routing import FakeProvider,router,error
    a=FakeProvider("alpha","one",error(413,"rate_limit_exceeded"),context_window=4096,configured_priority=1)
    unknown=FakeProvider("beta","unknown","Must not run")
    b=FakeProvider("beta","two","Usable response",context_window=32768)
    routed=router(a,unknown,b)
    result=await routed.generate_result("Synthetic public request",{"privacy_class":"PUBLIC"})
    assert result.response_text=="Usable response"
    assert len(a.received)==len(b.received)==1
    failure=next(e for e in routed.events if e["event"]=="provider_failed")
    assert failure["failover_trigger"]["kind"]=="CONTEXT_TOO_LARGE"
    assert failure["http_status"]==413 and failure["task_profile"]["requires_long_context"]
    assert not unknown.received
    assert routed.health.get("alpha","one").known_limit is None


@pytest.mark.asyncio
async def test_413_without_verified_larger_capacity_fails_truthfully_without_replay():
    from test_a10_5_routing import FakeProvider,router,error
    from hub.llm_provider import ReasoningProviderUnavailable
    a=FakeProvider("alpha","one",error(413,"rate_limit_exceeded"),configured_priority=1)
    b=FakeProvider("beta","unknown","Must not run")
    routed=router(a,b)
    with pytest.raises(ReasoningProviderUnavailable) as raised:
        await routed.generate_result("Synthetic public request",{"privacy_class":"PUBLIC"})
    assert raised.value.primary_category=="CONTEXT_TOO_LARGE"
    assert "verified context capacity" in raised.value.user_message
    assert len(a.received)==1 and not b.received


@pytest.mark.parametrize("code",[None,"rate_limit_exceeded","context_length_exceeded"])
def test_http_413_status_wins_over_provider_error_wording(code):
    from hub.provider_failover import FailoverManager
    from test_a10_5_routing import error
    failure=FailoverManager.classify(error(413,code))
    assert failure.kind==failure.category=="CONTEXT_TOO_LARGE"
    assert not failure.safe_retry_after and not failure.quota


def test_actual_context_413_retains_existing_context_capacity_guard():
    from hub.provider_failover import FailoverManager
    from test_a10_5_routing import error
    failure=FailoverManager.classify(error(413,"context_length_exceeded"))
    assert failure.kind=="CONTEXT_TOO_LARGE" and failure.retryable
