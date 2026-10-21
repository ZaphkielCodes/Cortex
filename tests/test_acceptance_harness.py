"""Offline human-review integration; real approval boundaries, no provider calls."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from scripts.acceptance_review import review_approval, remaining_seconds, provider_failure, interruption
from scripts.live_benchmark import selected_cases
from hub.approval_observability import snapshot
from hub.database import get_confirmation
from hub.main import _execute_resolve_confirmation
from hub.agent_turn import AgentTurn
from test_privacy_approval_runtime import private_turn, pending, finish

ROOT = Path(__file__).resolve().parents[1]


class Reply:
    def __init__(self, data, status=200): self.data, self.status_code = data, status
    def json(self): return self.data
    def raise_for_status(self): assert self.status_code==200


class LocalApprovalClient:
    def __init__(self, agent_id): self.agent_id=agent_id; self.posts=[]
    async def get(self, url):
        cid=url.rsplit('/',1)[-1]; row=await get_confirmation(cid)
        return Reply(dict(row, **snapshot(cid), server_now=datetime.now(timezone.utc).isoformat()))
    async def post(self,url,json):
        self.posts.append((url,json))
        if url.endswith('/stop'):
            AgentTurn.cancel_task(self.agent_id); return Reply({})
        ok,_=await _execute_resolve_confirmation(url.split('/')[-2],json['decision'],'asus')
        return Reply({},200 if ok else 400)


class Human:
    def __init__(self, choice): self.choice=choice
    def poll(self):
        if self.choice=='wait': return None
        choice,self.choice=self.choice,'wait'
        return time.monotonic(),choice


@pytest.mark.asyncio
@pytest.mark.parametrize('choice,expected', [('y','APPROVAL_GRANTED'),('n','APPROVAL_DENIED'),('wait','APPROVAL_EXPIRED'),('c','APPROVAL_CANCELLED'),(None,'APPROVAL_CANCELLED')])
async def test_real_approval_review_outcomes_preserve_runtime_and_identity(choice,expected):
    agent,ctrl,model=private_turn(); agent.APPROVAL_SECONDS=.5
    ack=await agent.run('Inspect the synthetic interface','asus',{},'human-review-parent')
    job=AgentTurn._running[ack.task_id]
    row=await pending(); cid=row['command_id']
    for _ in range(100):
        if snapshot(cid): break
        await asyncio.sleep(.001)
    metadata=snapshot(cid)
    assert metadata['task_id']==ack.task_id
    assert (datetime.fromisoformat(metadata['expires_at'])-datetime.fromisoformat(metadata['created_at'])).total_seconds()==pytest.approx(.5)
    client=LocalApprovalClient(ack.task_id); lines=[]
    started=time.monotonic()
    def emit(line,**kwargs): lines.append((time.monotonic(),line))
    class Reviewer(Human):
        def poll(self):
            assert any('HUMAN APPROVAL REQUIRED' in line for _,line in lines)
            return super().poll()
    result=await review_approval(client,'http://local',dict(row,**metadata),'fixture-case','human-review-parent',Reviewer(choice),emit)
    final=await job
    await asyncio.gather(*list(AgentTurn._running.values()))
    assert result['outcome']==expected
    assert result['task_id']=='human-review-parent'
    assert any('HUMAN APPROVAL REQUIRED' in line for _,line in lines)
    assert 'private-canary-text' not in json.dumps(lines)
    if choice=='y':
        assert final.status=='success' and ctrl.execute_and_observe.await_count==1
        assert result['remaining_seconds_when_decided']>0
        assert (await get_confirmation(cid))['status']=='approved'
    elif choice=='n':
        assert not (await _execute_resolve_confirmation(cid,'approve','asus'))[0]
        assert (await get_confirmation(cid))['status']=='denied'
    elif choice=='wait':
        assert not client.posts and result['human_decision_at'] is None
        assert not (await _execute_resolve_confirmation(cid,'approve','asus'))[0]
        assert (await get_confirmation(cid))['status']=='expired'
    else:
        assert final.status=='cancelled'


def test_countdown_uses_actual_expiry_and_server_clock_not_assumed_ttl():
    now=datetime.now(timezone.utc)
    row={'created_at':(now-timedelta(seconds=2)).isoformat(), 'expires_at':(now+timedelta(seconds=7.25)).isoformat(),'server_now':now.isoformat()}
    assert remaining_seconds(row)==7.25
    row['server_now']=(now+timedelta(seconds=9)).isoformat()
    assert remaining_seconds(row)==0
    row.pop('expires_at'); assert remaining_seconds(row) is None


def test_original_compound_prompts_are_byte_for_byte_unchanged():
    content=(ROOT/'benchmarks/a10_compound_acceptance.json').read_bytes()
    assert hashlib.sha256(content).hexdigest()=='94bbfa541b8d6f1740b75eb631d6d58c4af55e273a84bc30ed7a29319595e35c'


@pytest.mark.parametrize('case_id',['layout_change','canvas_dimensions','unsent_document'])
def test_case_selection_preserves_prompt_and_excludes_other_cases(case_id):
    cases=json.loads((ROOT/'benchmarks/a10_compound_acceptance.json').read_bytes())
    selected=selected_cases(cases,case_id)
    assert len(selected)==1 and selected[0]['id']==case_id
    assert selected[0] is next(c for c in cases if c['id']==case_id)


def test_unknown_case_fails_before_runtime_or_provider_start():
    with pytest.raises(ValueError): selected_cases([{'id':'one'}],'missing')


@pytest.mark.asyncio
async def test_individual_case_evidence_and_execution_are_isolated(tmp_path,monkeypatch):
    from scripts import live_benchmark as live
    cases=json.loads((ROOT/'benchmarks/a10_compound_acceptance.json').read_bytes())
    manifest=ROOT/'benchmarks/a10_compound_acceptance.json'; runs=[]
    class Runtime:
        def __init__(self,output,provider):
            self.output=output; self.env={}; self.headers={}; self.device='fixture'; self.url='http://local'
        async def start(self): pass
        async def stop(self): pass
        async def run_case(self,case):
            runs.append(case)
            return {'id':case['id'],'final':{'status':'success'},'passed':None}
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,*args,**kwargs): return Reply({})
    monkeypatch.setattr(live,'Runtime',Runtime);monkeypatch.setattr(live.httpx,'AsyncClient',Client)
    for case in cases:
        output=tmp_path/case['id']
        await live.main(SimpleNamespace(manifest=str(manifest),output=str(output),provider='direct',ids=case['id'],manual_approvals=True))
        saved=json.loads((output/'results.json').read_text())
        assert len(saved['cases'])==1 and saved['cases'][0]['id']==case['id']
        assert saved['cases'][0]['passed'] is None
    assert [r['id'] for r in runs]==[c['id'] for c in cases]


@pytest.mark.parametrize('status,kind,category,expected',[
    (429,'RATE_LIMIT','RATE_LIMIT_429','HTTP_429'),
    (400,'UNSUPPORTED_TOOL_USE','INVALID_OUTPUT','HTTP_400_TOOL_USE_FAILED'),
    (503,'PROVIDER_SPECIFIC_FAILURE','SERVICE_UNAVAILABLE_503','HTTP_503'),
    (504,'TIMEOUT','DEADLINE_EXCEEDED','HTTP_504'),
    (None,'TIMEOUT','DEADLINE_EXCEEDED','TIMEOUT'),
    (None,'TEMPORARY_COOLDOWN','PROVIDER_COOLDOWN','COOLDOWN'),
    (413,'CONTEXT_TOO_LARGE','CONTEXT_TOO_LARGE','CONTEXT_TOO_LARGE')])
def test_provider_failure_classifications_are_distinct(status,kind,category,expected):
    assert provider_failure({'http_status':status,'failover_trigger':{'kind':kind,'category':category}})['classification']==expected


def test_http_or_model_success_is_never_a_semantic_pass():
    assert interruption({'final':{'status':'success'}})=='SEMANTIC_REVIEW_REQUIRED'


def test_recovered_provider_error_does_not_hide_later_semantic_failure():
    record={'provider_failures':[{'classification':'HTTP_503'}],
            'final':{'status':'error','data':{'error_code':'recovery_exhausted'}}}
    assert interruption(record)=='SEMANTIC_TASK_FAILURE'
    record['final']['data']['error_code']='model_provider_unavailable'
    assert interruption(record)=='PROVIDER_MODEL_INTERRUPTION'


@pytest.mark.asyncio
async def test_late_answer_from_previous_approval_is_never_reused():
    agent,_,_=private_turn();agent.APPROVAL_SECONDS=.5
    ack=await agent.run('Inspect the synthetic interface','asus',{})
    job=AgentTurn._running[ack.task_id]
    row=await pending();cid=row['command_id']
    class Reviewer:
        answers=[(0.,'y')]
        def poll(self):
            return self.answers.pop(0) if self.answers else (time.monotonic(),'n')
    client=LocalApprovalClient(ack.task_id)
    result=await review_approval(client,'http://local',row,'fixture',ack.task_id,Reviewer(),lambda *a,**k:None)
    await job
    await asyncio.gather(*list(AgentTurn._running.values()))
    assert result['outcome']=='APPROVAL_DENIED'
    assert len(client.posts)==1 and client.posts[0][1]=={'decision':'deny'}


@pytest.mark.asyncio
async def test_approval_status_metadata_is_origin_scoped():
    from hub.api_control import approval_status_endpoint
    from fastapi import HTTPException
    agent,_,_=private_turn();ack=await agent.run('Inspect synthetic UI','asus',{})
    row=await pending();cid=row['command_id']
    request=SimpleNamespace(state=SimpleNamespace(control_caller={'device_id':'lenovo'}))
    with pytest.raises(HTTPException) as e: await approval_status_endpoint(cid,request)
    assert e.value.status_code==403
    request.state.control_caller={'device_id':'asus'}
    visible=await approval_status_endpoint(cid,request)
    assert visible['expires_at'] and visible['recipient']
    assert 'private-canary-text' not in json.dumps(visible)
    AgentTurn.cancel_task(ack.task_id);await finish(ack.task_id)
