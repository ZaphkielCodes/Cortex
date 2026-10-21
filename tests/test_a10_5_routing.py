"""Offline routing contracts; fake providers, real AgentTurn/control/persistence boundaries."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from hub.direct_router import DirectModelRouter
from hub.routing_profiles import ModelProfile,TaskProfile,RoutingHints
from hub.provider_health import ProviderHealthStore
from hub.provider_failover import FailoverManager,Failure
from hub.llm_provider import ProviderHTTPError,ReasoningProviderUnavailable
from hub.intent_schema import ModelResult
from hub.privacy import PrivacyPolicyEngine


class FakeProvider:
    locality="cloud_worker"
    max_privacy_class="SENSITIVE"
    supports_vision=False
    configured=True
    def __init__(self,provider,model,*replies,**hints):
        self.name,self.model=provider,model
        self.profile=ModelProfile(model,provider,**hints)
        self.replies=list(replies) or ["Ready"]
        self.received=[]
        self.last_usage=None
        self.last_rate_limits={}
    async def generate_result(self,text,context,on_chunk=None):
        self.received.append((text,dict(context)))
        response=self.replies.pop(0)
        if isinstance(response,Exception):raise response
        if callable(response):response=response()
        if isinstance(response,dict):response=json.dumps(response)
        return ModelResult(type="RESPONSE",response_text=response)


def router(*providers,**kwargs):
    return DirectModelRouter(list(providers),profiles={(p.name,p.model):p.profile for p in providers},**kwargs)


def error(status,code=None,scope=None,retry=None):
    e=ProviderHTTPError("fixture",status)
    e.provider_error_code=code
    e.failure_scope=scope
    e.retry_after=retry
    return e


def quota(retry=None):
    e=error(429,retry=retry)
    e.quota_scopes=["TPD"]
    e.quota_metadata={"quota_scope":"TPD","known_limit":200000,"known_used":196395,"known_remaining":3605}
    return e


def choice(r,**profile):
    return r.candidates({"privacy_class":"PUBLIC","task_profile":profile})[0].provider.model


def test_simple_interactive_selects_low_latency():
    r=router(FakeProvider("alpha","fast",expected_latency=1),FakeProvider("beta","slow",expected_latency=35,reasoning=.99))
    assert choice(r)=="fast"


def test_complex_and_coding_use_structured_requirements():
    r=router(FakeProvider("alpha","fast",reasoning=.3,coding=.2,expected_latency=1),
        FakeProvider("beta","strong",reasoning=.99,coding=.99,expected_latency=4))
    assert choice(r,complexity="high")=="strong"
    assert choice(r,category="coding")=="strong"


def test_background_tolerates_slow_quality_but_computer_loop_penalizes_it():
    r=router(FakeProvider("alpha","fast",reasoning=.3,expected_latency=1,computer_use=.8),
        FakeProvider("beta","slow",reasoning=.99,expected_latency=35,computer_use=.99))
    assert choice(r,category="computer_use")=="fast"
    assert r.candidates({"privacy_class":"PUBLIC","execution_mode":"background","task_profile":{"complexity":"high"}})[0].provider.model=="slow"


def test_measured_latency_and_known_quota_pressure_change_ranking():
    r=router(FakeProvider("alpha","one",expected_latency=1),FakeProvider("beta","two",expected_latency=2))
    assert choice(r)=="one"
    r.health.get("alpha","one").average_latency=40
    assert choice(r)=="two"
    r.health.get("alpha","one").average_latency=None
    row=r.health.get("alpha","one");row.known_limit=100;row.known_remaining=1
    assert choice(r)=="two"


@pytest.mark.asyncio
async def test_model_specific_tool_failure_uses_same_provider_alternative():
    a=FakeProvider("alpha","one",error(400,"tool_use_failed"),configured_priority=1)
    b=FakeProvider("alpha","two","Ready")
    r=router(a,b)
    assert (await r.generate_result("An invented request",{"privacy_class":"PUBLIC"})).response_text=="Ready"
    assert len(a.received)==len(b.received)==1
    failed=next(e for e in r.events if e["event"]=="provider_failed")
    assert failed["failover_trigger"]=={"kind":"UNSUPPORTED_TOOL_USE","category":"INVALID_OUTPUT","scope":"model"}


@pytest.mark.asyncio
async def test_daily_quota_skips_sibling_models_and_uses_another_provider():
    a=FakeProvider("alpha","one",quota(),configured_priority=1)
    sibling=FakeProvider("alpha","two","must not run")
    b=FakeProvider("beta","three","Ready")
    r=router(a,sibling,b)
    result=await r.generate_result("An invented request",{"privacy_class":"PUBLIC"})
    assert result.response_text=="Ready" and len(a.received)==len(b.received)==1 and not sibling.received
    state=r.health.snapshot("alpha")
    assert state["state"]=="quota_exhausted" and state["failure_class"]=="DAILY_QUOTA_EXHAUSTED"
    assert state["known_remaining"]==3605 and state["quota_reset_at"] is None


@pytest.mark.asyncio
async def test_timeout_and_provider_outage_use_eligible_alternative():
    a=FakeProvider("alpha","one",TimeoutError(),configured_priority=1)
    b=FakeProvider("beta","two","Ready")
    r=router(a,b)
    assert (await r.generate_result("A request",{"privacy_class":"PUBLIC"})).response_text=="Ready"
    assert r.health.snapshot("alpha","one")["failure_class"]=="TIMEOUT"
    assert len(a.received)==len(b.received)==1


@pytest.mark.asyncio
async def test_auth_failure_does_not_retry_sibling_credentials():
    a=FakeProvider("alpha","one",error(401),configured_priority=1)
    sibling=FakeProvider("alpha","two","must not run")
    b=FakeProvider("beta","three","Ready","Again")
    r=router(a,sibling,b)
    await r.generate_result("A request",{"privacy_class":"PUBLIC"})
    await r.generate_result("Another request",{"privacy_class":"PUBLIC"})
    assert len(a.received)==1 and not sibling.received and len(b.received)==2
    assert r.health.get("alpha").state=="unavailable"


@pytest.mark.asyncio
async def test_invalid_request_stops_instead_of_repeating_bad_payload():
    a=FakeProvider("alpha","one",error(400,"invalid_request_error"),configured_priority=1)
    b=FakeProvider("beta","two","must not run")
    with pytest.raises(ReasoningProviderUnavailable) as exc:
        await router(a,b).generate_result("A request",{"privacy_class":"PUBLIC"})
    assert exc.value.primary_category=="INVALID_REQUEST" and not b.received


@pytest.mark.asyncio
async def test_context_failure_requires_known_larger_window():
    a=FakeProvider("alpha","one",error(413),context_window=4096,configured_priority=1)
    b=FakeProvider("beta","two","Ready",context_window=32768)
    unknown=FakeProvider("gamma","unknown","must not run")
    r=router(a,b,unknown)
    assert (await r.generate_result("A request",{"privacy_class":"PUBLIC"})).response_text=="Ready"
    assert not unknown.received
    selected=[e for e in r.events if e["event"]=="model_selected"][-1]
    assert any(c["reason"]=="context_window_unknown" for c in selected["rejected_candidates"])


def test_missing_credentials_and_unsupported_tool_models_are_removed():
    unavailable=FakeProvider("alpha","none");unavailable.configured=False
    incompatible=FakeProvider("beta","plain",tool_calling=False)
    good=FakeProvider("gamma","structured")
    r=router(unavailable,incompatible,good)
    assert choice(r,requires_tools=True)=="structured"
    assert not r.providers["alpha"].configured


def test_cooldown_persists_across_restart_and_reset_expires(tmp_path):
    now=[1000.]
    store=ProviderHealthStore(tmp_path/"health.json",clock=lambda:now[0])
    failure=FailoverManager.classify(quota("10"))
    store.failed("alpha","one",failure)
    restored=ProviderHealthStore(tmp_path/"health.json",clock=lambda:now[0])
    assert restored.blocked("alpha","one") and restored.get("alpha").quota_reset_at==1010
    now[0]=1011
    assert restored.blocked("alpha","one") is None
    assert restored.get("alpha").state=="unknown" and restored.get("alpha").known_remaining is None
    assert restored.get("alpha").quota_reset_at is None


def test_unknown_reset_is_not_fabricated_and_stale_knowledge_expires(tmp_path):
    now=[1000.];store=ProviderHealthStore(tmp_path/"health.json",clock=lambda:now[0])
    store.failed("alpha","one",FailoverManager.classify(quota()))
    assert store.get("alpha").quota_reset_at is None
    assert store.get("alpha").state=="quota_exhausted"
    now[0]=2000
    assert store.blocked("alpha","one") is None and store.get("alpha").known_limit is None


def test_cooldown_respected_without_a_request():
    a=FakeProvider("alpha","one");b=FakeProvider("beta","two")
    r=router(a,b);r.health.failed("alpha","one",Failure("PROVIDER_SPECIFIC_FAILURE","NETWORK_ERROR","provider",60))
    assert choice(r)=="two" and not a.received


@pytest.mark.asyncio
async def test_public_failover_and_private_recipient_binding():
    a=FakeProvider("alpha","one",error(503),configured_priority=1)
    b=FakeProvider("beta","two","Ready")
    assert (await router(a,b).generate_result("Public facts",{"privacy_class":"PUBLIC"})).response_text=="Ready"
    a=FakeProvider("alpha","one",error(503),configured_priority=1)
    b=FakeProvider("beta","two","must not run")
    selected,_=await router(a,b).select_for_turn("private",{"privacy_class":"SENSITIVE"},[])
    selected.authorize_recipient("SENSITIVE")
    with pytest.raises(ReasoningProviderUnavailable):
        await selected.generate_result("private",{"privacy_class":"SENSITIVE"})
    assert len(a.received)==1 and not b.received


@pytest.mark.asyncio
async def test_caller_approval_flags_cannot_authorize_sensitive_fallback():
    a=FakeProvider("alpha","one","must not run")
    r=router(a)
    with pytest.raises(ReasoningProviderUnavailable) as exc:
        await r.generate_result("private",{"privacy_class":"SENSITIVE","user_approved":True,"approved_executor_id":"agent_model"})
    assert exc.value.primary_category=="PRIVATE_MODEL_REQUIRED" and not a.received


@pytest.mark.asyncio
async def test_privacy_policy_is_rechecked_between_attempts():
    def revoke():
        PrivacyPolicyEngine.get_instance().policy.allow_remote_public_tasks=False
        raise error(503)
    a=FakeProvider("alpha","one",revoke,configured_priority=1)
    b=FakeProvider("beta","two","must not run")
    with pytest.raises(ReasoningProviderUnavailable):
        await router(a,b).generate_result("Public facts",{"privacy_class":"PUBLIC"})
    assert not b.received


@pytest.mark.asyncio
async def test_no_eligible_provider_waits_without_os_action():
    from hub.agent_turn import AgentTurn
    from hub.database import get_task
    from test_agent_turn_runtime import control
    p=FakeProvider("alpha","none");p.configured=False
    ctrl=control([])
    result=await AgentTurn(ctrl,router(p)).run("A request","asus",{})
    assert result.status=="error" and result.data["primary_category"]=="PROVIDER_NOT_CONFIGURED"
    state=json.loads((await get_task(result.task_id))["context"])["compound_ui"]
    assert state["current_stage"]=="WAITING_FOR_PROVIDER"
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_os_action_then_failover_continues_without_replay():
    from hub.agent_turn import AgentTurn
    from hub.database import get_task
    from test_agent_turn_runtime import control
    goal=[{"source":"capability","capability":"app.launch","path":["data","opened"],"operator":"equals","value":True}]
    proposal={"decision":"CALL_CAPABILITY","capability":"app.launch","arguments":{"app":"Invented Meridian"},
        "continue_after_result":True,"success_condition":goal}
    a=FakeProvider("alpha","one",proposal,error(503),configured_priority=1)
    b=FakeProvider("beta","two",{"decision":"RESPOND","response_text":"The requested window opened."})
    r=router(a,b)
    ctrl=control([{"status":"success","command_id":"only-once","data":{"opened":True,"window_id":42}}])
    result=await AgentTurn(ctrl,r).run("Prepare the invented window","asus",{"_request_task_id":"parent-fixture",
        "history":[{"role":"user","text":"Keep this context marker"}]},request_id="request-fixture")
    assert result.status=="success",result
    ctrl.execute_and_observe.assert_awaited_once()
    assert len(a.received)==2 and len(b.received)==1
    sent=b.received[0][1]
    assert sent["operational_task_id"]==result.task_id and sent["request_id"]=="request-fixture"
    assert "only-once" in sent["system_prompt"] and "Keep this context marker" in json.dumps(sent["conversation_history"])
    saved=json.loads((await get_task(result.task_id))["context"])
    state=saved["compound_ui"]
    assert state["current_stage"]=="COMPLETED" and state["execution_receipt"]["command_id"]=="only-once"
    assert state["parent_task_id"]=="parent-fixture"
    assert any(e["event"]=="provider_failed" and e["parent_task_id"]=="parent-fixture" for e in saved["routing_events"])


@pytest.mark.asyncio
async def test_all_failures_after_action_keep_receipt_and_wait_safely():
    from hub.agent_turn import AgentTurn
    from hub.database import get_task
    from test_agent_turn_runtime import control
    goal=[{"source":"capability","capability":"app.launch","path":["data","opened"],"value":True}]
    a=FakeProvider("alpha","one",{"decision":"PLAN","success_condition":goal,"plan_steps":[
        {"capability":"app.launch","arguments":{"app":"Invented Meridian"}}]},error(503),configured_priority=1)
    b=FakeProvider("beta","two",error(503))
    ctrl=control([{"status":"success","command_id":"only-once","data":{"opened":True}}])
    result=await AgentTurn(ctrl,router(a,b)).run("Prepare a window","asus",{})
    assert result.status=="error"
    saved=json.loads((await get_task(result.task_id))["context"])
    assert saved["compound_ui"]["current_stage"]=="WAITING_FOR_PROVIDER"
    assert saved["compound_ui"]["execution_receipt"]["command_id"]=="only-once"
    ctrl.execute_and_observe.assert_awaited_once()


@pytest.mark.asyncio
async def test_attempts_are_bounded_without_alternating_loops():
    models=[FakeProvider(f"p{i}",f"m{i}",TimeoutError()) for i in range(10)]
    r=router(*models)
    with pytest.raises(ReasoningProviderUnavailable):
        await r.generate_result("A request",{"privacy_class":"PUBLIC"})
    assert sum(len(p.received) for p in models)==FailoverManager.MAX_ATTEMPTS
    assert all(len(p.received)<=1 for p in models)


@pytest.mark.asyncio
async def test_routing_events_have_scores_but_no_prompts_or_reasoning(tmp_path):
    p=FakeProvider("alpha","one","Ready")
    r=router(p,health=ProviderHealthStore(tmp_path/"health.json"))
    await r.generate_result("PRIVATE-CANARY-PROMPT",{"privacy_class":"PUBLIC","system_prompt":"HIDDEN-CANARY-INSTRUCTIONS"})
    events=json.dumps(list(r.events))
    assert "PRIVATE-CANARY" not in events and "HIDDEN-CANARY" not in events
    selected=next(e for e in r.events if e["event"]=="model_selected")
    assert "expected_latency" in selected["eligible_candidates"][0]["components"]
    assert "PRIVATE-CANARY" not in (tmp_path/"health.json").read_text()


@pytest.mark.asyncio
async def test_callback_is_delivered_only_for_the_successful_attempt():
    a=FakeProvider("alpha","one",error(503),configured_priority=1)
    b=FakeProvider("beta","two","Ready")
    callback=AsyncMock()
    await router(a,b).generate_result("A request",{"privacy_class":"PUBLIC"},callback)
    callback.assert_awaited_once_with("Ready")


@pytest.mark.asyncio
async def test_cancellation_never_starts_fallback():
    started=asyncio.Event()
    a=FakeProvider("alpha","one",configured_priority=1)
    async def wait(*args):
        started.set();await asyncio.Event().wait()
    a.generate_result=wait
    b=FakeProvider("beta","two","must not run")
    job=asyncio.create_task(router(a,b).generate_result("A request",{"privacy_class":"PUBLIC"}))
    await started.wait();job.cancel()
    with pytest.raises(asyncio.CancelledError):await job
    assert not b.received


def test_task_hints_do_not_classify_words_or_lower_privacy():
    first=TaskProfile.from_context({"privacy_class":"SENSITIVE","task_profile":{"privacy_class":"PUBLIC","category":"coding"}})
    assert first.privacy_class=="SENSITIVE" and first.requires_code_reasoning
    with pytest.raises(ValueError):RoutingHints(privacy_class="PUBLIC")
    with pytest.raises(ValueError):ModelProfile("m","p",reasoning=float("nan"))


def test_factory_includes_configured_gemini_and_requires_trial_opt_in(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY","fixture")
    monkeypatch.setenv("GEMINI_API_KEY","fixture")
    monkeypatch.setenv("NVIDIA_API_KEY","fixture")
    monkeypatch.setenv("CORTEX_ENABLE_NVIDIA_TRIAL","0")
    monkeypatch.setenv("CORTEX_STATE_DIR",str(__import__('tempfile').gettempdir()))
    r=DirectModelRouter()
    assert {p.provider_id for p in r.providers.values()}=={"groq","gemini"}
    monkeypatch.setenv("CORTEX_ENABLE_NVIDIA_TRIAL","1")
    assert set(DirectModelRouter().providers)=={"groq","gemini","nvidia"}



def test_known_zero_quota_blocks_until_authoritative_reset():
    now=[1000.];store=ProviderHealthStore(clock=lambda:now[0])
    store.succeeded("alpha","one",1.,{"x-ratelimit-remaining-tokens":"0","x-ratelimit-reset-tokens":"2s"})
    assert store.blocked("alpha","one") and store.get("alpha").quota_reset_at==1002
    now[0]=1003
    assert store.blocked("alpha","one") is None and store.get("alpha").known_remaining is None


def test_unknown_quota_does_not_become_zero():
    store=ProviderHealthStore()
    store.succeeded("alpha","one",1.,{})
    assert store.get("alpha","one").known_remaining is None
    assert store.get("alpha","one").known_limit is None


def test_profile_cannot_advertise_an_unimplemented_transport_modality():
    p=FakeProvider("alpha","text-only",modalities=("text","image"))
    assert router(p).candidates({"privacy_class":"PUBLIC","routing_profile":"vision"})==[]


@pytest.mark.asyncio
async def test_next_model_cannot_repeat_the_prior_successful_action():
    from hub.agent_turn import AgentTurn
    from test_agent_turn_runtime import control
    from test_compound_ui_runtime import scene
    PrivacyPolicyEngine.get_instance().policy.sensitive_data_cloud_allowed=True
    goal=[{"source":"capability","capability":"app.launch","path":["data","opened"],"value":True}]
    proposal={"decision":"CALL_CAPABILITY","capability":"app.launch","arguments":{"app":"Invented Meridian"},
        "continue_after_result":True,"success_condition":goal}
    a=FakeProvider("alpha","one",proposal,error(503),configured_priority=1)
    b=FakeProvider("beta","two",proposal,{"decision":"RESPOND","response_text":"The launch is verified."})
    ctrl=control([{"status":"success","command_id":"once","data":{"opened":True}},scene("fresh")])
    result=await AgentTurn(ctrl,router(a,b)).run("Prepare a window","asus",{})
    assert result.status=="success",result
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list]==["app.launch","ui.observe"]


@pytest.mark.asyncio
async def test_semantic_decision_hints_rank_the_next_reasoning_call():
    from hub.agent_turn import AgentTurn
    from hub.database import get_task
    from test_agent_turn_runtime import control
    a=FakeProvider("alpha","fast",{"decision":"CALL_CAPABILITY","capability":"telemetry.get_system_usage","arguments":{},
        "routing_hints":{"category":"coding","complexity":"high","requires_code_reasoning":True}},
        reasoning=.1,coding=.1,expected_latency=1)
    b=FakeProvider("beta","strong","CPU usage is 34%.",reasoning=.99,coding=.99,expected_latency=5)
    ctrl=control([{"status":"success","data":{"cpu_percent":34}}])
    result=await AgentTurn(ctrl,router(a,b)).run("An invented request","asus",{})
    assert result.status=="success" and len(a.received)==len(b.received)==1
    assert b.received[0][1]["task_profile"]["requires_code_reasoning"]
    saved=json.loads((await get_task(result.task_id))["context"])
    assert saved["routing_task_profile"]["category"]=="coding"


@pytest.mark.asyncio
async def test_unsupported_tool_requirement_has_a_truthful_failure_class():
    p=FakeProvider("alpha","plain",tool_calling=False)
    with pytest.raises(ReasoningProviderUnavailable) as exc:
        await router(p).generate_result("A request",{"privacy_class":"PUBLIC","task_profile":{"requires_computer_use":True}})
    assert exc.value.primary_category=="UNSUPPORTED_TOOL_USE" and not p.received


def test_invalid_provider_configuration_does_not_disable_other_configured_providers(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY","fixture")
    monkeypatch.setenv("CORTEX_GROQ_MODEL","invalid/model")
    monkeypatch.setenv("GEMINI_API_KEY","fixture")
    monkeypatch.delenv("NVIDIA_API_KEY",raising=False)
    assert set(DirectModelRouter().providers)=={"gemini"}



def test_expired_degraded_measurements_do_not_survive_indefinitely():
    now=[1000.];store=ProviderHealthStore(clock=lambda:now[0])
    store.failed("alpha","one",Failure("PROVIDER_SPECIFIC_FAILURE","NETWORK_ERROR","provider",60))
    assert store.get("alpha","one").state=="degraded"
    now[0]+=86401
    row=store.get("alpha","one")
    assert row.state=="unknown" and row.last_failure_category is None
    assert row.average_latency is None and row.failures==0


def test_structured_output_rejection_is_model_specific_not_invalid_request():
    failure=FailoverManager.classify(error(400,"json_validate_failed"))
    assert failure.kind=="MODEL_SPECIFIC_FAILURE" and failure.scope=="model"
    assert failure.retryable



@pytest.mark.asyncio
async def test_routed_gemini_does_not_hide_nested_retry_or_invent_ttft(monkeypatch):
    from hub.gemini_route import GeminiRoute
    from hub.tracing import InteractionTrace
    gemini=GeminiRoute(api_key="fixture",model_name="gemini-test")
    request=AsyncMock(side_effect=error(503))
    client=SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=request)))
    monkeypatch.setattr(gemini,"_get_client",lambda:client)
    with pytest.raises(ProviderHTTPError):
        await gemini.generate_result("A public check",{"force_response_only":True})
    request.assert_awaited_once()
    request.side_effect=None
    request.return_value=SimpleNamespace(text="Ready")
    trace=InteractionTrace()
    result=await gemini.generate_result("A public check",{"force_response_only":True,"trace":trace})
    assert result.response_text=="Ready" and "model_first_token" not in trace.timestamps
    assert "model_complete" in trace.timestamps



@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped",[False,True])
async def test_unconfigured_private_request_does_not_ask_for_meaningless_sharing_approval(wrapped):
    from hub.agent_turn import AgentTurn
    from hub.database import get_task
    from hub.llm_provider import UnavailableLLMProvider
    from test_agent_turn_runtime import control
    ctrl=control([]);ctrl.request_approval=AsyncMock()
    from hub.model_router import ProviderRouterAdapter
    provider=ProviderRouterAdapter(UnavailableLLMProvider()) if wrapped else UnavailableLLMProvider()
    result=await AgentTurn(ctrl,provider).run("An invented private task","asus",{"privacy_class":"SENSITIVE"})
    assert result.status=="error" and result.data["primary_category"]=="PROVIDER_NOT_CONFIGURED"
    assert json.loads((await get_task(result.task_id))["context"])["compound_ui"]["current_stage"]=="WAITING_FOR_PROVIDER"
    ctrl.request_approval.assert_not_awaited();ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipient_approval_cannot_survive_revocation_or_cover_a_stronger_class():
    p=FakeProvider("alpha","one","must not run")
    selected,_=await router(p).select_for_turn("A request",{"privacy_class":"SENSITIVE"},[])
    selected.authorize_recipient("SENSITIVE")
    PrivacyPolicyEngine.get_instance().policy.allow_cloud_approval_override=False
    with pytest.raises(ReasoningProviderUnavailable):
        await selected.generate_result("A request",{"privacy_class":"SENSITIVE"})
    assert not p.received
    PrivacyPolicyEngine.get_instance().policy.allow_cloud_approval_override=True
    selected.authorized_class="PERSONAL"
    with pytest.raises(ReasoningProviderUnavailable):
        await selected.generate_result("A request",{"privacy_class":"SENSITIVE"})
    assert not p.received



@pytest.mark.asyncio
async def test_sdk_status_metadata_survives_without_scanning_a_raw_error_body():
    class SDKFailure(RuntimeError):
        code=503
    a=FakeProvider("alpha","one",error(503),configured_priority=1)
    b=FakeProvider("beta","two",SDKFailure("Redacted SDK failure"))
    r=router(a,b)
    with pytest.raises(ReasoningProviderUnavailable) as exc:
        await r.generate_result("A public request",{"privacy_class":"PUBLIC"})
    assert exc.value.fallback_status==503 and exc.value.fallback_category=="SERVICE_UNAVAILABLE_503"
    failed=[e for e in r.events if e["event"]=="provider_failed"]
    assert failed[-1]["http_status"]==503



@pytest.mark.asyncio
@pytest.mark.parametrize("background,expected",[(False,12),(True,45)])
async def test_router_deadline_reaches_the_actual_http_transport(background,expected):
    import httpx
    from hub.direct_provider import DirectLLMProvider
    sent=[]
    def reply(request):
        sent.append(request.extensions["timeout"]["read"])
        return httpx.Response(200,json={"choices":[{"message":{"content":"Ready"}}]})
    p=DirectLLMProvider(api_key="fixture",transport=httpx.MockTransport(reply))
    r=DirectModelRouter([p])
    await r.generate_result("A public check",{"privacy_class":"PUBLIC","force_response_only":True,
        "execution_mode":"background" if background else "interactive"})
    assert expected-1 < sent[0] <= expected


@pytest.mark.asyncio
async def test_background_deadline_reaches_gemini_sdk_without_nested_retry(monkeypatch):
    from hub.gemini_route import GeminiRoute
    from google.genai import types
    p=GeminiRoute(api_key="fixture",model_name="gemini-test")
    request=AsyncMock(return_value=SimpleNamespace(text="Ready"))
    monkeypatch.setattr(p,"_get_client",lambda:SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=request))))
    await DirectModelRouter([p]).generate_result("A public check",{"privacy_class":"PUBLIC","force_response_only":True,"execution_mode":"background"})
    # Validate against the installed SDK schema, not just a mirrored dictionary.
    config=types.GenerateContentConfig.model_validate(request.call_args.kwargs["config"])
    assert 44000 < config.http_options.timeout <= 45000
    assert config.http_options.retry_options.attempts==1
    request.assert_awaited_once()



@pytest.mark.asyncio
async def test_known_cooldown_private_request_waits_before_sharing_approval():
    from hub.agent_turn import AgentTurn
    from test_agent_turn_runtime import control
    from hub.database import get_task
    p=FakeProvider("alpha","one","must not run")
    r=router(p);r.health.failed("alpha","one",Failure("PROVIDER_SPECIFIC_FAILURE","NETWORK_ERROR","provider",60))
    ctrl=control([]);ctrl.request_approval=AsyncMock()
    result=await AgentTurn(ctrl,r).run("An invented private request","asus",{"privacy_class":"SENSITIVE"})
    assert result.status=="error" and result.data["primary_category"]=="PROVIDER_COOLDOWN"
    assert json.loads((await get_task(result.task_id))["context"])["compound_ui"]["current_stage"]=="WAITING_FOR_PROVIDER"
    ctrl.request_approval.assert_not_awaited()
    assert not p.received



@pytest.mark.asyncio
async def test_unknown_long_context_does_not_claim_a_provider_rejected_it():
    p=FakeProvider("alpha","unknown","must not run")
    with pytest.raises(ReasoningProviderUnavailable) as exc:
        await router(p).generate_result("A public request",{"privacy_class":"PUBLIC","task_profile":{"requires_long_context":True}})
    assert exc.value.primary_category=="CONTEXT_TOO_LARGE" and not p.received
    assert "verified context capacity" in exc.value.user_message
    assert "rejected" not in exc.value.user_message



@pytest.mark.parametrize("scope",[None,"model"])
def test_late_success_from_an_admitted_request_cannot_erase_daily_quota(scope):
    now=[1000.];store=ProviderHealthStore(clock=lambda:now[0])
    failure=quota();failure.failure_scope=scope
    store.failed("alpha","one",FailoverManager.classify(failure))
    target="one" if scope=="model" else None
    until=store.get("alpha",target).cooldown_until
    now[0]+=1
    # Another model was already in flight when the provider rejected the first.
    store.succeeded("alpha","one" if scope=="model" else "two",1.,{"x-ratelimit-remaining-tokens":"0","x-ratelimit-reset-tokens":"1s"})
    state=store.get("alpha",target)
    assert state.state=="quota_exhausted" and state.cooldown_until==until
    assert state.quota_scope=="TPD" and state.quota_reset_at is None
    assert state.known_remaining==3605
