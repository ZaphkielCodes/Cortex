"""General runtime repairs found by historical and live stabilization."""
import time
import httpx
import pytest
from hub.runtime_introspection import runtime_snapshot
from hub.database import enroll_device, set_device_permission
from hub.permissions import check_permission
from hub.privacy import classify_task_privacy
from hub.task_ownership import TaskOwnershipManager
from hub.direct_provider import DirectLLMProvider
from hub.direct_router import DirectModelRouter


@pytest.mark.asyncio
async def test_runtime_introspection_requires_enrollment_and_scopes_non_admin():
    assert (await runtime_snapshot("unknown"))["status"] == "denied"
    await enroll_device("owner-a","A","sub_admin","test")
    await enroll_device("owner-b","B","sub_admin","test")
    manager=TaskOwnershipManager.get_instance()
    own=await manager.create_execution(goal_id="own",executor_device_id="owner-a",owner_device_id="owner-a")
    other=await manager.create_execution(goal_id="other",executor_device_id="owner-b",owner_device_id="owner-b")
    result=await runtime_snapshot("owner-a")
    ids=[e["task_execution_id"] for e in result["data"]["executions"]]
    assert own.task_execution_id in ids and other.task_execution_id not in ids
    assert all(d["device_id"]=="owner-a" for d in result["data"]["devices"])
    assert classify_task_privacy("system.runtime_state").privacy_class == "SENSITIVE"


@pytest.mark.asyncio
async def test_hub_capability_permission_uses_canonical_owner_and_keeps_overrides():
    await enroll_device("source","Source","admin","test")
    permission=await check_permission("admin","source","source","system.runtime_state")
    assert permission.allowed  # Hub-owned; not advertised by a Windows executor.
    await set_device_permission("source","system.runtime_state","deny")
    assert not (await check_permission("admin","source","source","system.runtime_state")).allowed
    assert not (await check_permission("sub_admin","source","source","system.runtime_state")).allowed


@pytest.mark.asyncio
async def test_provider_requested_wait_retries_only_reasoning(monkeypatch):
    calls=[]; pauses=[]
    def respond(request):
        calls.append(request)
        if len(calls)==1:return httpx.Response(429,headers={"retry-after":"1"})
        return httpx.Response(200,json={"choices":[{"message":{"content":"Ready"}}]})
    router=DirectModelRouter([DirectLLMProvider(api_key="test",transport=httpx.MockTransport(respond))])
    health_now = [router.health.clock()]
    router.health.clock = lambda: health_now[0]
    async def pause(seconds):
        pauses.append(seconds)
        health_now[0] += seconds
        for route in router.routes:route.cooldown_until=0
    monkeypatch.setattr("hub.direct_router.asyncio.sleep",pause)
    monkeypatch.setenv("CORTEX_PROVIDER_RATE_WAIT_SECONDS","2")
    result=await router.generate_result("A public question",{"force_response_only":True,"privacy_class":"PUBLIC"})
    assert result.response_text=="Ready" and len(calls)==2
    assert len(pauses)==1 and 0<pauses[0]<2
    assert calls[0].content==calls[1].content


@pytest.mark.asyncio
async def test_capability_schema_is_loaded_on_demand_without_executing_it():
    import json
    from model_contract_fixtures import scripted_harness,call
    harness,ctrl,model=scripted_harness([
        {"decision":"INSPECT_CAPABILITY","capability":"app.launch"},
        call("app.launch",app="Paint")], [{"status":"success","data":{"opened":True}}])
    result=await harness.process_request("asus","Start the requested application",{})
    assert result.status=="success"
    assert ctrl.execute_and_observe.await_count==1
    def catalog(index):
        return json.loads(model.calls[index][1]["system_prompt"].split("CAPABILITIES:\n",1)[1].split("\nSTATE:",1)[0])
    before=catalog(0); after=catalog(1)
    assert {row[0] for row in before["rows"]}=={row[0] for row in after["rows"]}
    assert "app" in next(row for row in before["rows"] if row[0]=="app.launch")[2]  # Concise signature is immediately usable.
    assert "app" in next(row for row in after["rows"] if row[0]=="app.launch")[2]
    assert len(model.calls[0][1]["system_prompt"]) < len(model.calls[1][1]["system_prompt"])


@pytest.mark.asyncio
async def test_repeated_schema_inspection_stops_without_desktop_actions():
    from model_contract_fixtures import scripted_harness
    inspect={"decision":"INSPECT_CAPABILITY","capability":"app.launch"}
    harness,ctrl,model=scripted_harness([inspect,inspect])
    result=await harness.process_request("asus","A generic request",{})
    assert result.status=="error"
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_proposal_is_repaired_without_executing_or_replaying_it():
    from model_contract_fixtures import scripted_harness,call
    harness,ctrl,model=scripted_harness([call("invented.capability"),
        {"decision":"INSPECT_CAPABILITY","capability":"app.launch"},
        call("app.launch",app="Paint")],[{"status":"success","data":{"opened":True}}])
    result=await harness.process_request("asus","A generic action request",{})
    assert result.status=="success"
    assert ctrl.execute_and_observe.await_count==1
    assert ctrl.execute_and_observe.await_args.args[1]=="app.launch"
    assert "No action was sent" in model.calls[1][1]["system_prompt"]


@pytest.mark.asyncio
async def test_repair_budget_exhaustion_never_dispatches():
    from model_contract_fixtures import scripted_harness,call
    harness,ctrl,model=scripted_harness([call("invented.capability")]*4)
    result=await harness.process_request("asus","A generic action request",{})
    assert result.status=="error" and len(model.calls)==4
    assert result.data["error_code"]=="recovery_exhausted"
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_enrollment_is_discoverable_without_claiming_liveness(monkeypatch):
    import json
    from unittest.mock import AsyncMock
    from hub import main
    from hub.models import EnrollRequest
    from hub.executor_registry import ExecutorRegistry
    from model_contract_fixtures import scripted_harness
    monkeypatch.setattr(main,"_authenticate_http_headers",AsyncMock(return_value={"role":"admin"}))
    response=await main.enroll_endpoint(EnrollRequest(device_id="new-worker",device_name="New worker",role="sub_admin"),"test","source",None)
    reg=ExecutorRegistry.get_instance()
    assert reg.get_device_handle("new-worker") is not None and not reg.is_device_online("new-worker")
    assert not reg.get_device_handle("new-worker").capabilities
    harness,ctrl,model=scripted_harness([{"decision":"ASK_CLARIFICATION","response_text":"Which device?"}])
    await harness.process_request("asus","Inspect available workers",{})
    state=json.loads(model.calls[0][1]["system_prompt"].split("\nSTATE:\n")[1])
    assert {"device_id":"new-worker","name":"New worker","online":False} in state["known_devices"]
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_schema_inspection_provides_readable_arguments_and_clears_old_feedback():
    import json
    from model_contract_fixtures import scripted_harness,call
    harness,ctrl,model=scripted_harness([call("made_up"),
        {"decision":"INSPECT_CAPABILITY","capability":"app.launch"},
        call("app.launch",app="Paint")],[{"status":"success","data":{"opened":True}}])
    result=await harness.process_request("asus","A general action",{})
    assert result.status=="success"
    state=json.loads(model.calls[2][1]["system_prompt"].split("\nSTATE:\n")[1])
    assert state["validation_feedback"] is None
    assert state["loaded_schemas"]["app.launch"]["arguments"]["app"]["required"] is True
    assert ctrl.execute_and_observe.await_count==1


def test_launch_waits_for_window_even_when_process_already_exists(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from agent import desktop_actions as desktop
    monkeypatch.setattr(desktop,"_ACTION_OVERRIDE",None)
    monkeypatch.setattr("agent.app_resolver.resolve_installed_app",lambda _: {
        "launch_type":"executable","launch_target":"C:/fixtures/editor.exe","display_name":"Fixture Editor"})
    monkeypatch.setattr(desktop.os,"startfile",lambda _:None)
    monkeypatch.setattr(desktop.time,"sleep",lambda _:None)
    monkeypatch.setattr(desktop.psutil,"process_iter",lambda _: [SimpleNamespace(info={"name":"editor.exe","pid":12})])
    observed=[{"windows":[]},{"windows":[{"title":"Fixture Editor","process_name":"editor.exe","pid":12,"window_id":77}]}]
    clock=[0.0]
    monkeypatch.setattr(desktop.time,"monotonic",lambda:clock[0])
    monkeypatch.setattr(desktop.time,"sleep",lambda duration:clock.__setitem__(0,clock[0]+duration))
    windows=Mock(side_effect=lambda: observed.pop(0) if len(observed)>1 else observed[0])
    monkeypatch.setattr(desktop,"list_windows",windows)
    monkeypatch.setattr(desktop,"focus_window",lambda args:{"status":"success","verified":True})
    result=desktop.launch_app({"app":"Fixture Editor"})
    assert result["status"]=="success" and result["data"]["window_id"]==77
    assert windows.call_count>=3  # First process-only poll, then repeated stable-window observations.


@pytest.mark.asyncio
async def test_default_observation_shares_only_metadata_without_private_contents():
    import json
    from model_contract_fixtures import scripted_harness
    harness,ctrl,model=scripted_harness([{"decision":"CALL_CAPABILITY","capability":"ui.observe","arguments":{}}],
        [{"status":"success","message":"private-canary-title","data":{"foreground_app":"Example","window_title":"private-canary-title","elements":[{"text":"private-canary-control"}]}}])
    model.locality="cloud_worker"
    result=await harness.process_request("asus","Inspect app metadata",{})
    assert result.status=="success"
    assert ctrl.execute_and_observe.await_args.args[2]["include_tree"] is False
    assert "private-canary" not in json.dumps(model.calls)


@pytest.mark.asyncio
async def test_multiple_actual_rate_replies_wait_within_budget_without_action_replay(monkeypatch):
    calls=[]; pauses=[]
    def respond(request):
        calls.append(request)
        if len(calls)<3:return httpx.Response(429,headers={"retry-after":"1"})
        return httpx.Response(200,json={"choices":[{"message":{"content":"Ready"}}]})
    router=DirectModelRouter([DirectLLMProvider(api_key="test",transport=httpx.MockTransport(respond))])
    health_now = [router.health.clock()]
    router.health.clock = lambda: health_now[0]
    async def pause(seconds):
        pauses.append(seconds)
        health_now[0] += seconds
        for route in router.routes:route.cooldown_until=0
    monkeypatch.setattr("hub.direct_router.asyncio.sleep",pause)
    monkeypatch.setenv("CORTEX_PROVIDER_RATE_WAIT_SECONDS","4")
    result=await router.generate_result("A public question",{"force_response_only":True,"privacy_class":"PUBLIC"})
    assert result.response_text=="Ready" and len(calls)==3 and len(pauses)==2
    assert sum(pauses)<4 and calls[0].content==calls[2].content


def test_explicitly_unverified_executor_success_is_never_completion():
    from hub.agent_turn import _validate_result
    raw={"status":"success","verified":False,"data":{"foreground":False}}
    assert _validate_result("window.focus",raw)["status"]=="failure"


@pytest.mark.asyncio
async def test_window_identity_survives_session_reload_without_private_contents():
    from hub.session_context import SessionContextManager
    from hub.reasoning_context import build_reasoning_context
    await SessionContextManager.update_session("source","Open the application",{
        "status":"success","message":"Opened the application.",
        "data":{"window_id":77,"pid":12,"app_name":"Example Editor","window_title":"private-canary-title"},
        "provenance":{"capability":"app.launch","executor_device":"remote-worker","retrieved_at":"fixture-time"}})
    SessionContextManager._memory_cache.clear()
    sess=await SessionContextManager.get_session("source")
    receipt=sess["references"]["last_window_receipt"]
    assert receipt["window_id"]==77 and receipt["executor_device"]=="remote-worker"
    context=build_reasoning_context({"session":sess},lambda *args:True)
    assert context["previous_window"]["snapshot_only"] is True
    assert "private-canary-title" not in str(context)


def test_executor_transport_preserves_path_and_verification_metadata(tmp_path,monkeypatch):
    from agent import executor
    monkeypatch.setitem(executor.HANDLERS,"fixture.result",lambda args:{
        "status":"success","message":"Opened fixture.","path":str(tmp_path),"verified":True})
    native=executor.execute("fixture.result",{})
    wire={key:native.get(key) for key in ("status","message","error","data")}
    assert wire["data"]["path"]==str(tmp_path) and wire["data"]["verified"] is True
    monkeypatch.setitem(executor.HANDLERS,"fixture.result",lambda args:{
        "status":"success","message":"Input sent.","verified":False,"verification_required":True,"data":{"input_attempted":True}})
    typed=executor.execute("fixture.result",{})
    assert typed["data"]["verified"] is False and typed["data"]["verification_required"] is True


@pytest.mark.asyncio
async def test_native_decision_transport_accepts_only_one_matching_proposal():
    import json
    from hub.agent_turn import AgentDecision
    bodies=[]
    proposal={"decision":"CALL_CAPABILITY","capability":"app.launch","arguments":{"app":"Example Editor"}}
    def response(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,json={"choices":[{"finish_reason":"tool_calls","message":{"tool_calls":[{
            "type":"function","function":{"name":"agent_decision","arguments":json.dumps(proposal)}}]}}]})
    p=DirectLLMProvider(api_key="test",transport=httpx.MockTransport(response))
    result=await p.generate_result("A public action",{"mode":"agent_decision","decision_schema":AgentDecision.model_json_schema()})
    assert json.loads(result.response_text)==proposal
    assert bodies[0]["tool_choice"]["function"]["name"]=="agent_decision"
    assert "response_format" not in bodies[0]
    p.transport=httpx.MockTransport(lambda _:httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":json.dumps(proposal)}}]}))
    with pytest.raises(ValueError):
        await p.generate_result("A public action",{"mode":"agent_decision","decision_schema":AgentDecision.model_json_schema()})


def test_reasoning_profile_prefers_the_configured_reasoning_model():
    qwen=DirectLLMProvider(api_key="test")
    reasoning=DirectLLMProvider(model_name="openai/gpt-oss-120b",api_key="test")
    router=DirectModelRouter([qwen,reasoning])
    assert router.candidates({"privacy_class":"INTERNAL","routing_profile":"reasoning"})[0].provider is reasoning
    assert router.candidates({"privacy_class":"INTERNAL","routing_profile":"general"})[0].provider is qwen


@pytest.mark.asyncio
async def test_compound_completion_keeps_real_window_identity_for_followup():
    import json
    from model_contract_fixtures import scripted_harness,call
    from hub.session_context import SessionContextManager
    harness,ctrl,model=scripted_harness([
        dict(call("app.launch",app="Example Editor"),continue_after_result=True,
             success_condition=[{"source":"capability","capability":"app.launch","path":["data","window_id"],"operator":"equals","value":77}]),
        {"decision":"RESPOND","response_text":"The editor is open."}],
        [{"status":"success","data":{"window_id":77,"pid":12,"app_name":"Example Editor"}}])
    first=await harness.process_request("asus","Open the editor",{})
    assert first.status=="success" and first.data["window_id"]==77
    await SessionContextManager.update_session("asus","Open the editor",first.model_dump())
    harness,ctrl,model=scripted_harness([call("ui.observe",target_window_id=77,include_tree=False)],
        [{"status":"success","data":{"window_id":77,"foreground_app":"Example Editor"}}])
    final=await harness.process_request("asus","Inspect that window",{"session":await SessionContextManager.get_session("asus")})
    assert final.status=="success"
    assert ctrl.execute_and_observe.await_args.args[2]["target_window_id"]==77
    state=json.loads(model.calls[0][1]["system_prompt"].split("\nSTATE:\n")[1])
    assert state["context"]["previous_window"]["window_id"]==77
