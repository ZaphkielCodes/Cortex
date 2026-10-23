"""A12 protocol, device isolation, real lifecycle and authorized interaction."""
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from hub.workspace import Cursor, Envelope, WorkspaceStore, project
from hub.database import add_task, update_task, get_task, enroll_device
from hub.auth import hash_secret


@pytest_asyncio.fixture
async def client(monkeypatch):
    from hub.main import app
    from hub import api_workspace
    monkeypatch.setattr(api_workspace,'store',WorkspaceStore())
    await enroll_device('asus','ASUS','admin',hash_secret('workspace-fixture-secret'))
    headers={'X-Cortex-Device':'asus','X-Cortex-Secret':'workspace-fixture-secret'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers=headers) as c:
        yield c


async def task(tid='request_workspace',status='RUNNING',source='asus',context=None):
    await add_task({'id':tid,'title':'Workspace task','type':'request','command':'request','status':status,
                    'assigned_device':source,'context':context or {}})


@pytest.mark.asyncio
async def test_protocol_snapshot_delta_noop_and_restart_recovery(client):
    await task()
    first=(await client.post('/api/workspace/sync',json={})).json()
    Envelope.model_validate(first)
    assert first['mode']=='snapshot' and len(first['items'])==4
    cursor={'epoch':first['epoch'],'revision':first['revision']}
    noop=(await client.post('/api/workspace/sync',json=cursor)).json()
    assert noop['mode']=='delta' and noop['items']==[] and noop['revision']==first['revision']
    await update_task('request_workspace',{'status':'FAILED','context':{'result':{'status':'error','message':'Provider unavailable'}}})
    update=(await client.post('/api/workspace/sync',json=cursor)).json()
    assert update['mode']=='delta' and update['base_revision']==first['revision']
    assert any(i['kind']=='error' and i['text']=='Provider unavailable' for i in update['items'])
    assert not any(i.get('value')==1 for i in update['items'])
    restarted=await WorkspaceStore().sync('asus',Cursor(**cursor))
    assert restarted.mode=='snapshot' and restarted.epoch!=first['epoch']


@pytest.mark.asyncio
async def test_workspace_requires_auth_admin_and_source_ownership(client):
    await task();await task('other-task',source='other')
    assert (await client.post('/api/workspace/sync',json={},headers={'X-Cortex-Secret':''})).status_code==401
    await enroll_device('worker','Worker','trusted_worker',hash_secret('worker-fixture-secret'))
    assert (await client.post('/api/workspace/sync',json={},headers={'X-Cortex-Device':'worker','X-Cortex-Secret':'worker-fixture-secret'})).status_code==403
    payload=(await client.post('/api/workspace/sync',json={})).json()
    assert 'other-task' not in json.dumps(payload)
    deny=await client.post('/api/workspace/action',json={'kind':'cancel_task','task_id':'other-task','action_id':'0'*32})
    assert deny.status_code==403 and (await get_task('other-task'))['status']=='RUNNING'


@pytest.mark.asyncio
async def test_valid_control_uses_existing_cancel_path_and_stale_control_is_rejected(client):
    await task()
    snapshot=(await client.post('/api/workspace/sync',json={})).json()
    action=next(i for i in snapshot['items'] if i['kind']=='task')['actions'][0]
    body={k:action[k] for k in ('kind','task_id','action_id')}
    result=await client.post('/api/workspace/action',json=body)
    assert result.status_code==200 and (await get_task('request_workspace'))['status'].upper()=='CANCELLED'
    assert (await client.post('/api/workspace/action',json=body)).status_code==409
    final=(await client.post('/api/workspace/sync',json={})).json()
    detail=next(i for i in final['items'] if i['kind']=='task')
    assert detail['status']=='CANCELLED' and not detail['actions']


@pytest.mark.asyncio
async def test_forged_and_executable_controls_cannot_dispatch(client):
    await task()
    assert (await client.post('/api/workspace/action',json={'kind':'cancel_task','task_id':'request_workspace','action_id':'forged'})).status_code==409
    for payload in ({'kind':'run_shell','task_id':'request_workspace','action_id':'x'},
                    {'kind':'cancel_task','task_id':'request_workspace','action_id':'x','command':'run_shell'},
                    {'kind':'cancel_task','task_id':'../other','action_id':'x'}):
        assert (await client.post('/api/workspace/action',json=payload)).status_code==422
    assert (await get_task('request_workspace'))['status']=='RUNNING'


@pytest.mark.asyncio
async def test_untrusted_output_is_plain_bounded_data_without_credentials(client,monkeypatch):
    secret='a12-private-canary-value'
    monkeypatch.setenv('TEST_API_KEY',secret)
    await task(context={'result':{'message':f'<img src=x onerror=alert(1)> {secret} access_token=hidden'}})
    raw=(await client.post('/api/workspace/sync',json={})).json()
    encoded=json.dumps(raw)
    assert secret not in encoded and 'access_token=hidden' not in encoded
    assert '<img src=x' in encoded # Literal text, not a generated HTML component.
    assert all(i['kind']!='html' for i in raw['items'])
    assert 'context' not in encoded and 'api_key' not in encoded


def test_presentations_bound_history_and_do_not_invent_active_progress():
    rows=[{'id':f'request_{n}','status':'RUNNING','title':'x'*5000,'assigned_device':'asus',
        'context':{'compound_ui':{'operational_trace':[{'stage':'RUNNING'}]*1000},'completed_steps':[]}} for n in range(100)]
    items=project('asus',rows)
    assert len(items)==80 and max(len(i.title) for i in items)<=240
    assert all(i.value is None for i in items if i.kind=='progress')
    assert all(len(i.entries)==32 for i in items if i.kind=='timeline')


@pytest.mark.asyncio
async def test_real_agentturn_events_project_and_workspace_cancel_reaches_running_turn(client,monkeypatch):
    from hub.agent_turn import AgentTurn
    from test_agent_turn_runtime import ScriptedModel,call,control
    entered=asyncio.Event();hold=asyncio.Event()
    ctrl=control([])
    async def slow(*args,**kwargs):
        entered.set();await hold.wait()
        return {'status':'success','data':{'cpu_percent':1}}
    ctrl.execute_and_observe.side_effect=slow
    agent=AgentTurn(ctrl,ScriptedModel([call('telemetry.get_system_usage')]))
    agent.ACK_SECONDS=.01
    ack=await agent.run('Read telemetry','asus',{'response_channel':'gui'})
    await asyncio.wait_for(entered.wait(),3)
    current=(await client.post('/api/workspace/sync',json={})).json()
    detail=next(i for i in current['items'] if i['kind']=='task' and i['provenance']['task_id']==ack.task_id)
    descriptor=detail['actions'][0]
    assert detail['status']=='RUNNING'
    cancelled=await client.post('/api/workspace/action',json={k:descriptor[k] for k in ('kind','task_id','action_id')})
    assert cancelled.status_code==200
    await asyncio.gather(*list(AgentTurn._running.values()),return_exceptions=True)
    final=(await client.post('/api/workspace/sync',json={})).json()
    assert next(i for i in final['items'] if i['kind']=='task')['status']=='CANCELLED'
    assert ctrl.execute_and_observe.await_count==1


def test_protocol_disallows_executable_components_and_unbounded_versions():
    base={'protocol_version':1,'epoch':'epoch','revision':1,'mode':'snapshot','generated_at':'now','items':[]}
    for invalid in ({**base,'protocol_version':2},{**base,'html':'<script/>'},
                    {**base,'items':[{'id':'script','kind':'html','title':'bad','provenance':{'source':'task_db','task_id':'task'},'html':'x'}]}):
        with pytest.raises(ValidationError):Envelope.model_validate(invalid)


def test_workspace_protocol_client_behaviors():
    run=subprocess.run(['node','--test','tests/workspace_protocol.test.mjs'],cwd=Path(__file__).resolve().parents[1],
                       capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert run.returncode==0,run.stdout+run.stderr
