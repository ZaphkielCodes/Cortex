"""Session 2 contracts: fake providers/SDK only, actual router and AgentTurn."""
import asyncio,json
from types import SimpleNamespace
import httpx
import pytest
from hub.cost_accounting import BudgetLedger,metered_gemini
from hub.decision_retry import DecisionRetry
from hub.llm_provider import ProviderHTTPError
from hub.pipeline.pipeline import RequestPipeline
from test_agent_turn_runtime import ScriptedModel,control
from test_a10_5_routing import FakeProvider,router,error

@pytest.mark.asyncio
async def test_second_invalid_proposal_stops_without_dispatch():
    ctrl=control([])
    model=ScriptedModel(['broken','broken',{'decision':'RESPOND','response_text':'must not happen'}])
    result=await RequestPipeline(ctrl,model).execute('asus','Synthetic request',{})
    assert result.status=='error' and result.data['error_code']=='corrective_retry_exhausted'
    assert len(model.calls)==2 and ctrl.execute_and_observe.await_count==0

@pytest.mark.asyncio
async def test_router_does_not_fallback_around_invalid_budget():
    a=FakeProvider('alpha','one',error(400,'tool_use_failed'),'{}',configured_priority=1)
    b=FakeProvider('beta','two','unsafe')
    r=router(a,b);budget=DecisionRetry()
    with budget.scope():
        with pytest.raises(ValueError):await r.generate_result('Synthetic',{'privacy_class':'PUBLIC'})
    assert budget.reject()
    with budget.scope():
        await r.generate_result('Synthetic correction',{'privacy_class':'PUBLIC'})
    assert len(a.received)==2 and not b.received
    assert not budget.reject()

@pytest.mark.asyncio
async def test_corrective_transient_error_cannot_switch_recipient():
    a=FakeProvider('alpha','one',ValueError('bad'),error(503),configured_priority=1)
    b=FakeProvider('beta','two','unsafe')
    r=router(a,b);budget=DecisionRetry()
    with budget.scope():
        with pytest.raises(ValueError):await r.generate_result('Synthetic',{'privacy_class':'PUBLIC'})
    budget.reject()
    with budget.scope():
        with pytest.raises(Exception):await r.generate_result('Synthetic',{'privacy_class':'PUBLIC'})
    assert not b.received


def test_correction_rejects_identity_or_privacy_change():
    budget=DecisionRetry();p=SimpleNamespace(name='a',model='one')
    budget.select(p,{'privacy_class':'SENSITIVE'});budget.reject()
    for candidate,privacy in [(SimpleNamespace(name='b',model='one'),'SENSITIVE'),(p,'PUBLIC')]:
        with pytest.raises(ProviderHTTPError):budget.select(candidate,{'privacy_class':privacy})


def test_total_cap_survives_daily_reset_and_tasks_aggregate(tmp_path):
    db=BudgetLedger(tmp_path/'db',10,initialize=True,total_limit=12,task_id='task-a',clock=lambda:0)
    i=db.reserve('p','m',10)
    db.settle(i,dict(prompt_tokens=4,completion_tokens=4,total_tokens=8),dict(input_nusd_per_token=1,output_nusd_per_token=1),0)
    db.clock=lambda:86400
    with pytest.raises(ProviderHTTPError):db.reserve('p','m',5)
    assert db.aggregate()[0]['charged_nusd']==8
    assert 'task-a' not in json.dumps(db.aggregate())


def test_reconciliation_is_once_only_and_audited(tmp_path):
    db=BudgetLedger(tmp_path/'db',10,initialize=True,total_limit=10)
    i=db.reserve('p','m',10);db.settle(i,None,{},0)
    db.reconcile(i,2,'a'*64)
    assert db.aggregate()[0]['charged_nusd']==2
    with pytest.raises(ValueError):db.reconcile(i,0,'a'*64)
    assert db.reserve('p','m',8)


def test_policy_cannot_silently_raise_cap(tmp_path):
    db=BudgetLedger(tmp_path/'db',10,initialize=True,total_limit=10,experiment='trial')
    db.reserve('p','m',1)
    other=BudgetLedger(db.path,100,total_limit=100,experiment='trial')
    with pytest.raises(ProviderHTTPError):other.reserve('p','m',20)

@pytest.mark.asyncio
async def test_gemini_thought_tokens_count_once(tmp_path,monkeypatch):
    db=BudgetLedger(tmp_path/'db',10000,initialize=True)
    cfg={'version':2,'experiment_id':'test','policy_version':'v1','total_limit_nusd':10000,'daily_limit_nusd':10000,'ledger_path':str(db.path),
         'models':{'gemini/test':{'version':'p1','valid_until':'2099-01-01T00:00:00+00:00','text_only_all_tokens':True,
           'input_nusd_per_token':2,'output_nusd_per_token':3,'max_input_tokens':100,'max_output_tokens':100}}}
    path=tmp_path/'config.json';path.write_text(json.dumps(cfg));monkeypatch.setenv('CORTEX_BUDGET_CONFIG',str(path))
    provider=SimpleNamespace(model='test')
    async def call():return SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=10,candidates_token_count=5,thoughts_token_count=7,total_token_count=22),text='synthetic')
    await metered_gemini(provider,{'operational_task_id':'task1'},'synthetic',20,call)
    with db.connect() as con:
        row=con.execute('SELECT charged,output_tokens FROM calls').fetchone()
        assert row==(56,12)
        assert con.execute('SELECT reasoning_tokens FROM call_meta').fetchone()[0]==7

@pytest.mark.asyncio
async def test_actual_gemini_sdk_boundary_disables_hidden_retries(tmp_path,monkeypatch):
    from hub.llm_provider import GeminiLLMProvider
    db=BudgetLedger(tmp_path/'sdk.sqlite',100000,initialize=True)
    cfg={'version':2,'experiment_id':'sdk','policy_version':'v1','total_limit_nusd':100000,'daily_limit_nusd':100000,'ledger_path':str(db.path),
         'models':{'gemini/fixture':{'version':'p1','valid_until':'2099-01-01T00:00:00+00:00','text_only_all_tokens':True,
           'input_nusd_per_token':1,'output_nusd_per_token':1,'max_input_tokens':40000,'max_output_tokens':2048}}}
    path=tmp_path/'config.json';path.write_text(json.dumps(cfg));monkeypatch.setenv('CORTEX_BUDGET_CONFIG',str(path))
    seen=[]
    async def generate(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(text='A synthetic answer',usage_metadata=SimpleNamespace(prompt_token_count=12,candidates_token_count=4,thoughts_token_count=2,total_token_count=18))
    provider=GeminiLLMProvider(api_key='synthetic',model_name='fixture')
    monkeypatch.setattr(provider,'_get_client',lambda:SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))))
    result=await provider.generate_result('Synthetic input',{'force_response_only':True})
    assert result.response_text=='A synthetic answer'
    assert len(seen)==1 and seen[0]['config']['http_options']['retry_options']['attempts']==1
    with db.connect() as con:assert con.execute('SELECT charged FROM calls').fetchone()[0]==18


def test_concurrent_total_cap_and_crash_reservation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path=tmp_path/'shared.sqlite'
    BudgetLedger(path,100,initialize=True,total_limit=10)
    def admit(_):
        db=BudgetLedger(path,100,total_limit=10)
        try:return db.reserve('p','m',6)
        except ProviderHTTPError:return None
    with ThreadPoolExecutor(2) as pool:ids=list(pool.map(admit,range(2)))
    assert sum(i is not None for i in ids)==1
    reopened=BudgetLedger(path,100,total_limit=10,clock=lambda:4102444800)
    with pytest.raises(ProviderHTTPError):reopened.reserve('p','m',5)
    assert reopened.attempts()[0]['status']=='reserved'


@pytest.mark.asyncio
async def test_real_router_accounts_each_fallback_transport(tmp_path):
    from hub.direct_provider import DirectLLMProvider
    from hub.cost_accounting import BudgetTransport
    from hub.routing_profiles import ModelProfile
    from test_v1_cost_accounting import PRICE
    db=BudgetLedger(tmp_path/'fallback.sqlite',1000000,initialize=True,total_limit=1000000)
    def unavailable(r):return httpx.Response(429,json={'error':{'code':'rate_limit_exceeded','message':'daily quota exhausted'}})
    def success(r):return httpx.Response(200,json={'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},'choices':[{'finish_reason':'stop','message':{'content':'Fixture response'}}]})
    a=DirectLLMProvider('groq','openai/gpt-oss-120b',api_key='fake',transport=BudgetTransport(httpx.MockTransport(unavailable),db,'groq','openai/gpt-oss-120b',PRICE))
    b=DirectLLMProvider('nvidia','deepseek-ai/deepseek-v4.1-flash',api_key='fake',transport=BudgetTransport(httpx.MockTransport(success),db,'nvidia','deepseek-ai/deepseek-v4.1-flash',PRICE))
    for i,p in enumerate((a,b)):p.profile=ModelProfile(p.model,p.name,configured_priority=1-i,context_window=131072)
    result=await router(a,b).generate_result('Fixture',{'privacy_class':'PUBLIC','force_response_only':True})
    assert result.response_text=='Fixture response'
    rows=db.attempts()
    assert len(rows)==2 and [r['provider'] for r in rows]==['groq','nvidia']
    assert rows[0]['status']=='unknown' and rows[1]['charged']==35


@pytest.mark.asyncio
async def test_invalid_capability_arguments_never_dispatch(tmp_path):
    ctrl=control(['app.launch'])
    invalid={'decision':'CALL_CAPABILITY','capability':'app.launch','arguments':{}}
    model=ScriptedModel([invalid,invalid,{'decision':'RESPOND','response_text':'not reached'}])
    result=await RequestPipeline(ctrl,model).execute('asus','Synthetic task',{})
    assert result.data['error_code']=='corrective_retry_exhausted'
    assert len(model.calls)==2 and ctrl.execute_and_observe.await_count==0
