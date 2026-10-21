"""A10 closure contracts: real harness/AgentTurn, scripted provider and OS boundaries."""
import json
import pytest
from hub.agent_harness import AgentHarness
from hub.database import get_task
from hub.llm_provider import ReasoningProviderUnavailable
from test_agent_turn_runtime import ScriptedModel
from test_compound_ui_runtime import control, condition


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["RATE_LIMIT_429", "PROVIDER_COOLDOWN", "SERVICE_UNAVAILABLE_503"])
async def test_receipt_and_provider_wait_survive_next_turn_failure_without_replay(category):
    ctrl = control([{"status":"success", "command_id":"receipt-once", "data":{"opened":True,"window_id":42}}])
    model = ScriptedModel([{"decision":"PLAN", "success_condition":condition(), "plan_steps":[
        {"capability":"app.launch", "arguments":{"app":"Invented Meridian"}}]},
        ReasoningProviderUnavailable(category)])
    result = await AgentHarness(ctrl,model).process_request("asus","Prepare the invented workspace",{"_request_task_id":"closure-parent"},request_id="closure-request")
    assert result.status == "error"
    assert result.message == ReasoningProviderUnavailable(category).user_message
    assert result.latency_metrics["failure_category"] == category
    assert not result.latency_metrics["success"]
    child = await get_task(result.data["task_id"])
    context = json.loads(child["context"])
    state = context["compound_ui"]
    assert state["current_stage"] == "WAITING_FOR_PROVIDER"
    assert state["parent_task_id"] == "closure-parent"
    assert context["parent_request_id"] == "closure-parent"
    assert state["execution_receipt"]["command_id"] == "receipt-once"
    assert state["execution_receipt"]["ran"] is True
    assert state["execution_receipt"]["side_effects_possible"]
    assert not state["execution_receipt"]["retry_safe"]
    assert result.data["execution_receipts"][0]["command_id"] == "receipt-once"
    assert ctrl.execute_and_observe.await_count == 1
    assert ctrl.execute_and_observe.call_args.args[1] == "app.launch"
    assert len(model.calls) == 2
    assert all(e["task_id"] == child["id"] for e in state["operational_trace"])
    assert sum(e["next_state"] == "EXECUTING" for e in state["operational_trace"]) == 1
    assert context["result"]["data"]["primary_category"] == category


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["RATE_LIMIT_429", "PROVIDER_COOLDOWN", "PROVIDER_NOT_CONFIGURED"])
async def test_provider_block_before_first_action_never_dispatches(category):
    ctrl = control([])
    result = await AgentHarness(ctrl,ScriptedModel([ReasoningProviderUnavailable(category)])).process_request(
        "asus","Prepare an unfamiliar workspace",{})
    assert result.status == "error"
    assert result.message == ReasoningProviderUnavailable(category).user_message
    assert result.latency_metrics["failure_category"] == category
    assert result.data["primary_category"] == category
    ctrl.execute_and_observe.assert_not_awaited()
    context = json.loads((await get_task(result.data["task_id"]))["context"])
    assert context["compound_ui"]["current_stage"] == "WAITING_FOR_PROVIDER"
    assert not context["compound_ui"]["execution_receipts"]
    assert not context.get("completed_steps")


@pytest.mark.asyncio
async def test_non_provider_returned_error_is_not_promoted_to_provider_failure(monkeypatch):
    from hub.intent_schema import ConversationalResponse
    harness = AgentHarness(control([]),ScriptedModel([]))
    async def failed(*args,**kwargs):
        return ConversationalResponse(status="error",message="The action failed.",
            data={"error_code":"invalid_capability_call","primary_category":"RATE_LIMIT_429"})
    monkeypatch.setattr(harness.pipeline,"execute",failed)
    result = await harness.process_request("asus","Do a task",{})
    assert result.status == "error" and result.latency_metrics["failure_category"] == "HARNESS_ERROR"


@pytest.mark.asyncio
async def test_primary_and_backup_failure_details_are_preserved_in_telemetry():
    ctrl = control([])
    result = await AgentHarness(ctrl,ScriptedModel([
        ReasoningProviderUnavailable("RATE_LIMIT_429","NETWORK_ERROR",503)])).process_request("asus","A question",{})
    assert result.latency_metrics["failure_category"] == "RATE_LIMIT_429"
    assert result.data["fallback_category"] == "NETWORK_ERROR"
    assert result.data["fallback_http_status"] == 503
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("count,selected,required", [(3,"",3),(18,"",16),(18,"case-0",1)])
async def test_acceptance_metadata_counts_actual_selected_cases(tmp_path,monkeypatch,count,selected,required):
    from types import SimpleNamespace
    from scripts import live_benchmark
    class FixtureRuntime:
        def __init__(self,*args):
            self.env={}
            self.headers={}
            self.url="http://127.0.0.1"
        async def start(self): pass
        async def stop(self): pass
        async def run_case(self,case):
            return {"id":case["id"],"final":{"status":"error"},"passed":None}
    class Client:
        def __init__(self,*args,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,*args,**kwargs): return SimpleNamespace(status_code=200)
    monkeypatch.setattr(live_benchmark,"Runtime",FixtureRuntime)
    monkeypatch.setattr(live_benchmark.httpx,"AsyncClient",Client)
    manifest=tmp_path/"manifest.json"
    manifest.write_text(json.dumps([{"id":f"case-{i}","category":"fixture","prompt":"An invented compound task"}
        for i in range(count)]),encoding="utf-8")
    output=tmp_path/"report"
    await live_benchmark.main(SimpleNamespace(manifest=str(manifest),output=str(output),provider="direct",ids=selected,manual_approvals=False))
    report=json.loads((output/"results.json").read_text(encoding="utf-8"))
    assert report["acceptance_case_count"] == (1 if selected else count)
    assert report["acceptance_minimum_complete_cases"] == required
    assert len(report["cases"]) == report["acceptance_case_count"]
    assert all(case["passed"] is None for case in report["cases"])
