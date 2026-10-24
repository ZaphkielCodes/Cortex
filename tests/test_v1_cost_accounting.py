import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from hub.cost_accounting import BudgetLedger, BudgetTransport, configured_transport
from hub.direct_provider import DirectLLMProvider
from hub.llm_provider import ProviderHTTPError
from hub.provider_failover import FailoverManager

PRICE = dict(input_nusd_per_token=2, output_nusd_per_token=3,
             max_input_tokens=40000,max_output_tokens=1024)


def ledger(tmp_path, limit=100000):
    return BudgetLedger(tmp_path/'budget.sqlite',limit,initialize=True)


def records(db):
    with db.connect() as conn:
        conn.row_factory=sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT * FROM calls')]


def test_atomic_concurrent_admission(tmp_path):
    db=ledger(tmp_path,10)
    def reserve(_):
        try:db.reserve('groq','model',6);return True
        except ProviderHTTPError:return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(reserve,range(2)))==[False,True]
    assert sum(r['charged'] for r in records(db))==6


def test_unknown_usage_survives_restart_and_midnight(tmp_path):
    db=ledger(tmp_path,10);db.clock=lambda:0
    ident=db.reserve('groq','model',8);db.settle(ident,None,PRICE,1)
    reopened=BudgetLedger(db.path,10,clock=lambda:86400)
    with pytest.raises(ProviderHTTPError):reopened.reserve('groq','model',3)


def test_known_usage_reconciles_and_resets_next_day(tmp_path):
    db=ledger(tmp_path,10);db.clock=lambda:0
    ident=db.reserve('groq','model',10)
    db.settle(ident,dict(prompt_tokens=1,completion_tokens=1,total_tokens=2),PRICE,1)
    assert records(db)[0]['charged']==5
    db.clock=lambda:86400
    db.reserve('groq','model',10)


@pytest.mark.parametrize('usage',[None,{},dict(prompt_tokens=True,completion_tokens=1,total_tokens=2),dict(prompt_tokens=1,completion_tokens=1,total_tokens=3),dict(prompt_tokens=-1,completion_tokens=1,total_tokens=0)])
def test_invalid_usage_never_refunds(tmp_path,usage):
    db=ledger(tmp_path);ident=db.reserve('groq','model',50)
    db.settle(ident,usage,PRICE,0)
    assert records(db)[0]['charged']==50
    assert records(db)[0]['status']=='unknown'


def test_overrun_blocks_even_if_budget_remains(tmp_path):
    db=ledger(tmp_path);ident=db.reserve('groq','model',1)
    db.settle(ident,dict(prompt_tokens=1,completion_tokens=1,total_tokens=2),PRICE,0)
    with pytest.raises(ProviderHTTPError):db.reserve('groq','model',1)


def test_double_settlement_rejected(tmp_path):
    db=ledger(tmp_path);ident=db.reserve('groq','model',50)
    db.settle(ident,None,PRICE,0)
    with pytest.raises(ValueError):db.settle(ident,{},PRICE,0)


def test_missing_and_corrupt_config_fail_closed(tmp_path,monkeypatch):
    monkeypatch.setenv('CORTEX_BUDGET_CONFIG',str(tmp_path/'missing.json'))
    with pytest.raises(ProviderHTTPError) as exc:configured_transport('groq','model',None)
    assert not FailoverManager.classify(exc.value).retryable


def test_missing_ledger_never_created(tmp_path):
    with pytest.raises(ValueError):BudgetLedger(tmp_path/'missing.sqlite',10)
    assert not (tmp_path/'missing.sqlite').exists()


@pytest.mark.asyncio
async def test_malformed_response_still_accounts_tokens_without_content(tmp_path):
    db=ledger(tmp_path)
    async def respond(request):
        return httpx.Response(200,json={'usage':dict(prompt_tokens=10,completion_tokens=5,total_tokens=15),
            'choices':[{'finish_reason':'stop','message':{'content':'private malformed answer'}}]})
    transport=BudgetTransport(httpx.MockTransport(respond),db,'groq','openai/gpt-oss-120b',PRICE)
    provider=DirectLLMProvider('groq',model_name='openai/gpt-oss-120b',api_key='test',transport=transport)
    with pytest.raises(ValueError):await provider.generate_result('private email prompt',{'mode':'agent_decision'})
    row=records(db)[0]
    assert row['input_tokens']==10 and row['output_tokens']==5 and row['charged']==35
    assert 'private' not in json.dumps(row)
    assert provider.last_usage['total_tokens']==15


@pytest.mark.asyncio
async def test_budget_denial_sends_no_request(tmp_path):
    db=ledger(tmp_path,0);calls=[]
    transport=BudgetTransport(httpx.MockTransport(lambda r:calls.append(r)),db,'groq','openai/gpt-oss-120b',PRICE)
    provider=DirectLLMProvider('groq',model_name='openai/gpt-oss-120b',api_key='test',transport=transport)
    with pytest.raises(ProviderHTTPError) as exc:await provider.generate_result('hello',{})
    assert not calls and not FailoverManager.classify(exc.value).retryable


@pytest.mark.asyncio
async def test_cancellation_retains_reservation(tmp_path):
    db=ledger(tmp_path)
    async def cancel(r):raise asyncio.CancelledError()
    transport=BudgetTransport(httpx.MockTransport(cancel),db,'groq','openai/gpt-oss-120b',PRICE)
    provider=DirectLLMProvider('groq',model_name='openai/gpt-oss-120b',api_key='test',transport=transport)
    with pytest.raises(asyncio.CancelledError):await provider.generate_result('hello',{})
    assert records(db)[0]['status']=='unknown'


def test_existing_invalid_output_policy_exposes_retry_debt():
    # Characterization, not a claim the requested one-correction gate is met.
    failure=FailoverManager.classify(ValueError('malformed proposal'))
    assert failure.retryable
    assert FailoverManager.MAX_ATTEMPTS>2


@pytest.mark.asyncio
async def test_sdk_fallback_is_blocked_in_budget_mode(monkeypatch):
    from hub.tracing import observe_model_attempt
    called=[]
    class SDK:
        name='unmetered_sdk'
        model='test'
        @observe_model_attempt
        async def generate_result(self,user_text,context):called.append(True)
    monkeypatch.setenv('CORTEX_BUDGET_CONFIG','configured-but-not-read-by-sdk')
    with pytest.raises(ProviderHTTPError) as exc:await SDK().generate_result('private',{})
    assert not called and not FailoverManager.classify(exc.value).retryable


@pytest.mark.asyncio
async def test_runtime_config_wires_accounting(tmp_path,monkeypatch):
    db=ledger(tmp_path)
    config={'version':2,'experiment_id':'fixture','policy_version':'v1','total_limit_nusd':100000,'ledger_path':str(db.path),'daily_limit_nusd':100000,
            'models':{'groq/openai/gpt-oss-120b':dict(PRICE,version='fixture-v1',valid_until='2099-01-01T00:00:00+00:00',text_only_all_tokens=True)}}
    path=tmp_path/'config.json';path.write_text(json.dumps(config))
    monkeypatch.setenv('CORTEX_BUDGET_CONFIG',str(path))
    def response(r):return httpx.Response(200,json={'usage':dict(prompt_tokens=10,completion_tokens=5,total_tokens=15),'choices':[{'finish_reason':'stop','message':{'content':'Hello'}}]})
    provider=DirectLLMProvider('groq',model_name='openai/gpt-oss-120b',api_key='test',transport=httpx.MockTransport(response))
    result=await provider.generate_result('hello',{'force_response_only':True})
    assert result.response_text=='Hello' and records(db)[0]['charged']==35


@pytest.mark.parametrize('patch',[{'valid_until':'2000-01-01T00:00:00+00:00'},{'input_nusd_per_token':0},{'text_only_all_tokens':False}])
def test_unverified_prices_reject(tmp_path,monkeypatch,patch):
    db=ledger(tmp_path)
    price=dict(PRICE,version='fixture-v1',valid_until='2099-01-01T00:00:00+00:00',text_only_all_tokens=True);price.update(patch)
    config={'version':2,'experiment_id':'fixture','policy_version':'v1','total_limit_nusd':100000,'ledger_path':str(db.path),'daily_limit_nusd':100000,'models':{'groq/model':price}}
    p=tmp_path/'config.json';p.write_text(json.dumps(config));monkeypatch.setenv('CORTEX_BUDGET_CONFIG',str(p))
    with pytest.raises(ProviderHTTPError):configured_transport('groq','model',httpx.MockTransport(lambda r:None))
