"""Deterministic A14 behavior and security; no external model or OS side effects."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from hub.orchestration import Orchestrator, validate_scope, scoped_context, recover_graph, safe, eligible_catalog as real_eligible_catalog
from hub.orchestration_protocol import TaskGraph, Node, WorkerOutput, DEFAULT_ROLES
from hub.orchestration_workers import Workers, CallBudget, ScopedTurn, BudgetProvider, DelegatedControl
from hub.database import add_task, get_task, update_task, enroll_device
from hub.workspace import context, project, Envelope
from test_a11_integrations import env


def node(name='analysis', **kw):
    return dict(id=name, role='reasoning', objective='Analyze the supplied evidence.',
        success_criteria=['A supported answer with limitations.'], **kw)


def graph(*nodes):
    return TaskGraph(nodes=list(nodes or (node('a'), node('b', dependencies=['a']))))


def output(value='Supported analysis.', **kw):
    return {'output': WorkerOutput(value=value, **kw).model_dump(), 'receipts': [],
            'provenance': {'kind': 'model_analysis', 'privacy_class': 'INTERNAL'}}


class FakeWorkers:
    """Controlled outcomes for scheduler tests, never counted as live evidence."""
    def __init__(self, plan, run=None, verify=True):
        self.plan, self.runner, self.verify = plan, run, verify
        self.calls = []

    async def infer(self, schema, payload, source, ctx, tid, role):
        self.calls.append((schema.__name__, payload, dict(ctx)))
        if schema is TaskGraph:
            value = self.plan
        elif schema.__name__ == 'Synthesis':
            value = schema(answer='Combined supported analysis.', evidence_nodes=list(payload['results']))
        else:
            value = schema(supported=self.verify, criteria_satisfied=self.verify,
                checked_nodes=list(payload['results']))
        return value, {'provider': 'fixture', 'model': 'fixture', 'privacy_class': 'INTERNAL'}

    async def run(self, n, inputs, source, ctx, tid):
        return await self.runner(n, inputs, ctx) if self.runner else output(n.id)


@pytest_asyncio.fixture
async def setup(monkeypatch, env):
    Orchestrator._active.clear()
    Orchestrator._node_jobs.clear()
    Orchestrator._device_locks.clear()
    control = SimpleNamespace(check_permissions=AsyncMock(return_value=SimpleNamespace(allowed=True)),
        execute_and_observe=AsyncMock(return_value={'status': 'success', 'message': 'Wait completed.', 'data': {}}))
    await add_task({'id': 'turn_parent', 'title': 'Compare alternatives and combine findings.',
        'type': 'agent_turn', 'command': 'agent_turn', 'status': 'RUNNING', 'assigned_device': 'asus',
        'context': {'request': 'Compare alternatives and combine findings.', 'parent_request_id': 'request_parent'}})
    monkeypatch.setattr('hub.orchestration.eligible_catalog', AsyncMock(return_value=[]))
    return SimpleNamespace(control=control, monkeypatch=monkeypatch)


async def execute(setup, fake, **kw):
    setup.monkeypatch.setattr('hub.orchestration.Workers', lambda *args: fake)
    return await Orchestrator(setup.control, object()).execute('asus', 'turn_parent', kw)


@pytest.mark.parametrize('nodes', [
    [node('a'), node('a')],
    [node('a', dependencies=['b']), node('b', dependencies=['a'])],
    [node('a', dependencies=['unknown']), node('b')],
    [node('a', dependencies=['a']), node('b')],
    [node('a', required=False), node('b', required=False)],
    [node('a', required_capabilities=['ui.wait']), node('b')],
])
def test_invalid_dags_rejected(nodes):
    with pytest.raises(ValidationError):
        graph(*nodes)


def test_typed_protocol_rejects_executable_extensions_and_unbounded_graphs():
    with pytest.raises(ValidationError):
        Node(**node(), code='run arbitrary code')
    with pytest.raises(ValidationError):
        graph(*(node('n'+str(i)) for i in range(9)))
    with pytest.raises(ValidationError):
        WorkerOutput(value='valid', spawn_agents=['evil'])
    with pytest.raises(ValueError):
        WorkerOutput(value=True).validate_type('number')


@pytest.mark.parametrize('changes', [
    {'role': 'administrator'},
    {'kind': 'capability', 'required_capabilities': ['agent.orchestrate']},
    {'kind': 'capability', 'required_capabilities': ['shell.execute'], 'target_device': 'other'},
])
def test_roles_never_grant_capabilities(changes):
    first = node('a'); first.update(changes)
    with pytest.raises((ValueError, PermissionError)):
        validate_scope(graph(first, node('b')), {r.name: r for r in DEFAULT_ROLES}, [], 'asus', {})


@pytest.mark.asyncio
async def test_parallel_nodes_overlap_and_dependencies_wait(setup):
    arrived, release = set(), asyncio.Event()
    async def run(n, inputs, ctx):
        if n.id in {'a', 'b'}:
            arrived.add(n.id)
            if len(arrived) == 2: release.set()
            await asyncio.wait_for(release.wait(), 1)
        else:
            assert set(inputs) == {'a', 'b'}
        return output(n.id)
    result = await execute(setup, FakeWorkers(graph(node('a'), node('b'), node('c', dependencies=['a','b'])), run))
    assert result['status'] == 'success'
    saved = context(await get_task(result['data']['orchestration_task_id']))['orchestration']
    assert all(r['state'] == 'COMPLETED' for r in saved['nodes'].values())
    events = saved['events']
    assert max(i for i,e in enumerate(events) if e['node'] in {'a','b'} and e['stage']=='RUNNING') < min(i for i,e in enumerate(events) if e['stage']=='COMPLETED')


@pytest.mark.asyncio
async def test_parallel_limit_is_enforced(setup):
    active, peak = 0, 0
    async def run(n, inputs, ctx):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        await asyncio.sleep(.02)
        active -= 1
        return output()
    result = await execute(setup, FakeWorkers(graph(*(node('n'+str(i)) for i in range(8))), run))
    assert result['status']=='success' and peak==Orchestrator.MAX_PARALLEL


@pytest.mark.asyncio
async def test_context_shares_only_dependencies_and_preserves_privacy_floor(setup):
    async def run(n, inputs, ctx):
        assert 'password' not in ctx and 'memory' not in ctx and 'conversation_history' not in ctx
        if n.id == 'b': assert inputs == {}
        if n.id == 'c':
            assert set(inputs) == {'a'} and ctx['privacy_class']=='SENSITIVE'
        result = output(n.id)
        if n.id == 'a': result['provenance']['privacy_class']='SENSITIVE'
        return result
    fake = FakeWorkers(graph(node('a'),node('b'),node('c',dependencies=['a'])),run)
    result = await execute(setup, fake, password='SECRET', memory=['unrelated'], conversation_history=['private'],privacy_class='INTERNAL')
    assert result['status']=='success'
    assert fake.calls[-1][2]['privacy_class']=='SENSITIVE'


@pytest.mark.asyncio
async def test_failure_blocks_dependents_but_preserves_independent_results(setup):
    async def run(n, inputs, ctx):
        if n.id == 'a': raise RuntimeError('fixture')
        assert n.id != 'c'
        return output()
    fake = FakeWorkers(graph(node('a'),node('b'),node('c',dependencies=['a'])),run)
    result = await execute(setup,fake)
    assert result['status']=='failure' and len(fake.calls)==1
    saved=context(await get_task(result['data']['orchestration_task_id']))['orchestration']['nodes']
    assert [saved[n]['state'] for n in ('a','b','c')]==['FAILED','COMPLETED','BLOCKED']


@pytest.mark.asyncio
async def test_cancel_propagates_and_awaits_all_workers(setup):
    entered, cancelled, started = asyncio.Event(), set(), set()
    async def run(n, inputs, ctx):
        started.add(n.id)
        if len(started) == 2: entered.set()
        try: await asyncio.Event().wait()
        finally: cancelled.add(n.id)
    task=asyncio.create_task(execute(setup,FakeWorkers(graph(node('a'),node('b')),run)))
    await entered.wait()
    tid=next(iter(Orchestrator._active));Orchestrator.cancel(tid)
    result=await task
    assert result['status']=='cancelled' and cancelled=={'a','b'} and not Orchestrator._active
    assert all(r['state']=='CANCELLED' for r in context(await get_task(tid))['orchestration']['nodes'].values())


@pytest.mark.asyncio
async def test_deadline_stops_children_without_success_claim(setup,monkeypatch):
    assert Orchestrator.DEADLINE_SECONDS < 130  # Frozen parent dispatch lease.
    monkeypatch.setattr(Orchestrator,'DEADLINE_SECONDS',.04)
    async def run(*args): await asyncio.Event().wait()
    result=await execute(setup,FakeWorkers(graph(node('a'),node('b')),run))
    assert result['status']=='failure' and 'deadline' in result['message']


@pytest.mark.asyncio
async def test_action_claim_is_durable_and_never_repeated(setup):
    coordinator=Orchestrator(setup.control,object())
    coordinator.tid='turn_parent';coordinator._save_lock=asyncio.Lock()
    coordinator.saved={'orchestration':{'action_claims':[]}}
    await coordinator.claim_action('asus','fs.write',{'path':'fixture','text':'data'})
    assert len(context(await get_task('turn_parent'))['orchestration']['action_claims'])==1
    with pytest.raises(ValueError,match='duplicate'):
        await coordinator.claim_action('asus','fs.write',{'path':'fixture','text':'data'})
    setup.control.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_graph_is_returned_without_model_or_action_replay(setup):
    fake=FakeWorkers(graph())
    first=await execute(setup,fake)
    count=len(fake.calls)
    second=await execute(setup,fake)
    assert first==second and len(fake.calls)==count


@pytest.mark.asyncio
async def test_owned_parent_and_permissions_cannot_be_forged(setup):
    coordinator=Orchestrator(setup.control,object())
    assert (await coordinator.execute('other','turn_parent',{}))['status']=='denied'
    setup.control.check_permissions.return_value.allowed=False
    assert (await coordinator.execute('asus','turn_parent',{}))['status']=='denied'
    assert not Orchestrator._active


@pytest.mark.asyncio
async def test_nested_delegation_rejected_even_if_called_directly(setup):
    await add_task({'id':'graph_owner','title':'graph','type':'orchestration','command':'agent.orchestrate','assigned_device':'asus'})
    await update_task('turn_parent',{'context':{'parent_request_id':'graph_owner'}})
    result=await Orchestrator(setup.control,object()).execute('asus','turn_parent',{})
    assert result['status']=='denied'


@pytest.mark.asyncio
async def test_restart_keeps_completed_results_and_marks_incomplete_unverified(setup):
    data={'orchestration':{'nodes':{'a':{'state':'COMPLETED','result':output()},'b':{'state':'RUNNING'}},'action_claims':['intent']}}
    await add_task({'id':'graph_restart','title':'graph','type':'orchestration','status':'RUNNING','command':'agent.orchestrate','assigned_device':'asus','context':data})
    from hub.task_orchestrator import TaskOrchestrator
    await TaskOrchestrator().reload_active_tasks()
    saved=context(await get_task('graph_restart'))
    assert saved['orchestration']['nodes']['a']['result']==output()
    assert saved['orchestration']['nodes']['b']['state']=='UNVERIFIED'
    assert saved['orchestration']['action_claims']==['intent']
    setup.control.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_claims_prevent_successful_synthesis(setup):
    async def run(n, *args):return output(claims=[{'key':'measurement','value':n.id}])
    fake=FakeWorkers(graph(node('a'),node('b')),run)
    result=await execute(setup,fake)
    assert result['status']=='failure' and 'conflicting' in result['message'] and len(fake.calls)==1


@pytest.mark.asyncio
async def test_verifier_failure_cannot_be_erased_by_synthesizer(setup):
    result=await execute(setup,FakeWorkers(graph(),verify=False))
    assert result['status']=='failure' and 'Verification' in result['message']


def test_secret_redaction_and_context_budget(monkeypatch):
    monkeypatch.setenv('FIXTURE_API_KEY','secret-value-1234')
    assert 'secret-value-1234' not in json.dumps(safe({'value':'secret-value-1234','token':'canary'}))
    assert scoped_context({'approved':True,'memory':'all','privacy_class':'SENSITIVE'})=={'privacy_class':'SENSITIVE'}
    with pytest.raises(ValueError,match='context_budget'):
        Orchestrator.bounded({str(i):'x'*4000 for i in range(10)})


@pytest.mark.asyncio
async def test_budget_counts_all_worker_inference_turns():
    provider=SimpleNamespace(generate_result=AsyncMock(return_value='value'))
    budget=CallBudget(1);wrapped=BudgetProvider(provider,budget)
    await wrapped.generate_result('request',{})
    with pytest.raises(ValueError,match='budget'):
        await wrapped.generate_result('request',{})
    assert provider.generate_result.await_count==1


@pytest.mark.asyncio
async def test_scoped_worker_denies_out_of_scope_and_offline_device(setup):
    from hub.agent_turn import AgentDecision
    turn=ScopedTurn(setup.control,object(),['ui.wait'],'asus')
    with pytest.raises(ValueError,match='out_of_scope'):
        turn._validate_call('shell.execute',{'command':'anything'})
    with pytest.raises(ValueError):
        await turn._execute('turn_parent',AgentDecision(decision='CALL_CAPABILITY',capability='ui.wait',arguments={'seconds':1}),'asus',{})
    setup.control.execute_and_observe.assert_not_awaited()
    # Declaring a legacy task-spawning capability cannot bypass graph bounds.
    recursive=ScopedTurn(setup.control,object(),['goal.create'],'asus')
    with pytest.raises(ValueError):recursive._validate_call('goal.create',{'description':'spawn'})
    with pytest.raises(PermissionError):
        validate_scope(graph(node('a',kind='capability',required_capabilities=['goal.create']),node('b')),
            {r.name:r for r in DEFAULT_ROLES},[{'name':'goal.create','targets':['asus']}],'asus',{})


@pytest.mark.asyncio
async def test_workspace_projects_real_graph_and_keeps_actions_on_owned_parent(setup):
    result=await execute(setup,FakeWorkers(graph()))
    graph_row=await get_task(result['data']['orchestration_task_id'])
    root=await get_task('turn_parent')
    items=project('asus',[root,graph_row])
    table=next(i for i in items if i.id=='agents:turn_parent')
    assert len(table.rows)==2 and all(r[3]=='COMPLETED' for r in table.rows)
    assert next(i for i in items if i.id=='agents-progress:turn_parent').value==1.
    assert next(i for i in items if i.kind=='task').actions[0].task_id=='turn_parent'
    Envelope(epoch='fixture',revision=1,mode='snapshot',generated_at='',items=items)


class RealPathProvider:
    name='fixture_local';model='fixture';locality='local_device';configured=True
    async def generate_result(self,text,ctx,on_chunk=None):
        from hub.intent_schema import ModelResult
        if ctx.get('mode')=='result_interpretation':raw='The requested wait completed.'
        else:raw=json.dumps({'decision':'CALL_CAPABILITY','capability':'ui.wait','arguments':{'seconds':1}})
        return ModelResult(type='RESPONSE',response_text=raw)


@pytest.mark.asyncio
async def test_real_frozen_worker_path_requires_receipts_and_preserves_requester(setup):
    from test_a13_discovery import connected,batch,enrolled
    from hub.discovery import reconcile
    await enrolled();reg,_=connected();reconcile(reg,'asus',batch())
    worker=Workers(setup.control,RealPathProvider(),{r.name:r for r in DEFAULT_ROLES},CallBudget())
    n=Node(**node('wait',kind='capability',required_capabilities=['ui.wait'],target_device='asus'))
    result=await worker.run(n,{},'asus',{'privacy_class':'INTERNAL'},'turn_parent')
    assert result['receipts'][0]['status']=='success'
    assert result['receipts'][0]['capability']=='ui.wait'
    assert (await get_task(result['provenance']['task_id']))['assigned_device']=='asus'
    assert setup.control.execute_and_observe.await_count==1


@pytest.mark.asyncio
async def test_private_output_is_not_silently_shared_with_parent_interpreter(setup):
    async def run(*args):
        value=output('Private canary')
        value['provenance']['privacy_class']='SENSITIVE'
        return value
    result=await execute(setup,FakeWorkers(graph(),run))
    assert result['data']['private_result_local_only'] is True
    assert 'Private canary' not in json.dumps(result)
    saved=context(await get_task(result['data']['orchestration_task_id']))
    assert saved['result']['message']=='Combined supported analysis.'
    replay=await execute(setup,FakeWorkers(graph(),run))
    assert replay==result


@pytest.mark.asyncio
async def test_model_privacy_denial_prevents_any_provider_call(setup):
    from hub.orchestration_protocol import WorkerRole
    provider=SimpleNamespace(name='external',model='fixture',locality='cloud_worker',
        generate_result=AsyncMock())
    workers=Workers(setup.control,provider,{},CallBudget())
    with pytest.raises(PermissionError):
        await workers.infer(WorkerOutput,{'request':'private'},'asus',{'privacy_class':'SENSITIVE'},
            'turn_parent',WorkerRole(name='analysis',purpose='Analyze evidence.'))
    provider.generate_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_router_selects_worker_model_from_role_profile(setup):
    from hub.direct_router import DirectModelRouter
    from hub.intent_schema import ModelResult
    from hub.orchestration_protocol import WorkerRole
    payloads=[]
    class Local:
        name='local_fixture';model='model';configured=True;locality='local_device'
        async def generate_result(self,text,ctx,on_chunk=None):
            payloads.append(ctx)
            return ModelResult(type='RESPONSE',response_text=json.dumps({'value':'Supported coding analysis.'}))
    router=DirectModelRouter([Local()])
    workers=Workers(setup.control,router,{},CallBudget())
    value,route=await workers.infer(WorkerOutput,{'objective':'Analyze a bounded algorithm.'},'asus',{},
        'turn_parent',WorkerRole(name='coding',purpose='Analyze code.',category='coding'))
    assert value.value=='Supported coding analysis.' and route['provider']=='local_fixture'
    assert payloads[0]['task_profile']['category']=='coding'
    assert any(e['event']=='routing_succeeded' for e in router.events)


@pytest.mark.asyncio
async def test_worker_failure_has_no_action_retry(setup):
    from test_a13_discovery import connected,batch,enrolled
    from hub.discovery import reconcile
    await enrolled();reg,_=connected();reconcile(reg,'asus',batch())
    setup.control.execute_and_observe.return_value={'status':'failure','message':'Executor failed.'}
    workers=Workers(setup.control,RealPathProvider(),{r.name:r for r in DEFAULT_ROLES},CallBudget())
    n=Node(**node('wait',kind='capability',required_capabilities=['ui.wait']))
    with pytest.raises(ValueError,match='not_verified'):
        await workers.run(n,{},'asus',{},'turn_parent')
    assert setup.control.execute_and_observe.await_count==1


@pytest.mark.asyncio
async def test_worker_cannot_approve_its_own_action(setup):
    from test_a13_discovery import connected,batch,enrolled
    from hub.discovery import reconcile
    from hub.agent_turn import AgentDecision
    await enrolled(names=('fs.move',));reg,_=connected(names=('fs.move',));reconcile(reg,'asus',batch(names=('fs.move',)))
    setup.control.check_permissions.return_value.needs_confirmation=True
    turn=ScopedTurn(setup.control,RealPathProvider(),['fs.move'],'asus')
    result=await turn._execute('turn_parent',AgentDecision(decision='CALL_CAPABILITY',capability='fs.move',
        arguments={'src':'fixture-source','dst':'fixture-destination'}),'asus',{'approved':True})
    assert result['status']=='needs_approval'
    setup.control.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_permission_revocation_after_planning_blocks_execution(setup):
    from test_a13_discovery import connected,batch,enrolled
    from hub.discovery import reconcile
    from hub.agent_turn import AgentDecision
    await enrolled();reg,_=connected();reconcile(reg,'asus',batch())
    setup.control.check_permissions.return_value.allowed=False
    turn=ScopedTurn(setup.control,RealPathProvider(),['ui.wait'],'asus')
    result=await turn._execute('turn_parent',AgentDecision(decision='CALL_CAPABILITY',capability='ui.wait',arguments={'seconds':1}),'asus',{})
    assert result['status']=='denied'
    setup.control.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_decomposition_has_one_bounded_replan_and_no_execution(setup):
    fake=FakeWorkers(graph(node('a'),node('b')))
    original=fake.infer
    calls=0
    async def infer(*args):
        nonlocal calls
        calls+=1
        raise ValueError('Invalid plan')
    fake.infer=infer
    result=await execute(setup,fake)
    assert result['status']=='failure' and calls==2
    setup.control.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_optional_work_is_disclosed_in_synthesis(setup):
    async def run(n,*args):
        if n.id=='b':raise RuntimeError('Unavailable')
        return output()
    fake=FakeWorkers(graph(node('a'),node('b',required=False)),run)
    result=await execute(setup,fake)
    assert result['status']=='success' and result['data']['incomplete_nodes']==['b']
    assert fake.calls[1][1]['incomplete_optional_nodes']==['b']


@pytest.mark.asyncio
async def test_native_workspace_cancel_uses_existing_backend_and_cancels_graph(setup):
    from hub.api_workspace import action
    from hub.workspace import ActionRequest, action_id
    entered=asyncio.Event();started=set()
    async def run(n,*args):
        started.add(n.id)
        if len(started)==2:entered.set()
        await asyncio.Event().wait()
    task=asyncio.create_task(execute(setup,FakeWorkers(graph(node('a'),node('b')),run)))
    try:
        await entered.wait()
        tid=next(iter(Orchestrator._active));row=await get_task(tid)
        result=await action(ActionRequest(kind='cancel_task',task_id=tid,action_id=action_id('asus',row)),{'device_id':'asus'})
        assert result['state']=='CANCELLED' and (await task)['status']=='cancelled'
    finally:
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)


def test_large_workspace_stays_within_existing_protocol_budget():
    rows=[{'id':'t'+str(i),'title':'task','status':'FAILED','context':{'result':{'message':'error'}},'updated_at':str(i)} for i in range(30)]
    items=project('asus',rows)
    assert len(items)<=121
    Envelope(epoch='fixture',revision=1,mode='snapshot',generated_at='',items=items)


@pytest.mark.asyncio
async def test_explicit_memory_selection_is_scoped_and_raises_privacy(setup):
    from hub.orchestration_protocol import WorkerRole
    captured=[]
    class Provider:
        name='local';model='fixture';configured=True;locality='local_device'
        async def generate_result(self,text,ctx,on_chunk=None):
            from hub.intent_schema import ModelResult
            captured.append((json.loads(text),ctx))
            return ModelResult(type='RESPONSE',response_text='{"value":"Supported preference analysis."}')
    memories={'chosen':{'key':'chosen','value':'Selected preference','privacy_class':'PERSONAL'},
              'unrelated':{'key':'unrelated','value':'Unrelated private data'}}
    workers=Workers(setup.control,Provider(),{r.name:r for r in DEFAULT_ROLES},CallBudget(),memory=memories)
    result=await workers.run(Node(**node('analysis',memory_keys=['chosen'])),{},'asus',{},'turn_parent')
    assert captured[0][0]['selected_memory']==[memories['chosen']]
    assert 'Unrelated private data' not in json.dumps(captured)
    assert captured[0][1]['privacy_class']=='PERSONAL' and result['provenance']['privacy_class']=='PERSONAL'
    with pytest.raises(ValueError,match='memory_reference'):
        validate_scope(graph(node('a',memory_keys=['not_in_context']),node('b')),
            {r.name:r for r in DEFAULT_ROLES},[],'asus',{},memory_keys=memories)


@pytest.mark.asyncio
async def test_subtask_cancellation_stops_graph_and_releases_job_handles(setup):
    entered=asyncio.Event()
    async def run(*args):entered.set();await asyncio.Event().wait()
    task=asyncio.create_task(execute(setup,FakeWorkers(graph(node('a'),node('b')),run)))
    await entered.wait()
    worker_id=next(iter(Orchestrator._node_jobs))
    Orchestrator.cancel(worker_id)
    assert (await task)['status']=='cancelled'
    assert not Orchestrator._node_jobs and not Orchestrator._active


@pytest.mark.asyncio
async def test_capability_workers_are_serialized_and_lock_lifecycle_is_bounded(setup):
    active,peak=0,0
    async def run(*args):
        nonlocal active,peak
        active+=1;peak=max(peak,active)
        await asyncio.sleep(.02)
        active-=1
        return output()
    setup.monkeypatch.setattr('hub.orchestration.eligible_catalog',AsyncMock(return_value=[
        {'name':'ui.wait','targets':['asus'],'target_type':'device'}]))
    plan=graph(node('a',kind='capability',required_capabilities=['ui.wait']),
               node('b',kind='capability',required_capabilities=['ui.wait']))
    assert (await execute(setup,FakeWorkers(plan,run)))['status']=='success'
    assert peak==1 and not Orchestrator._device_locks


@pytest.mark.asyncio
async def test_fabricated_execution_summary_cannot_replace_missing_receipt(setup):
    from test_a13_discovery import connected,batch,enrolled
    from hub.discovery import reconcile
    from hub.intent_schema import ModelResult
    class Fabricator(RealPathProvider):
        async def generate_result(self,*args,**kw):
            return ModelResult(type='RESPONSE',response_text='{"decision":"RESPOND","response_text":"The wait completed."}')
    await enrolled();reg,_=connected();reconcile(reg,'asus',batch())
    workers=Workers(setup.control,Fabricator(),{r.name:r for r in DEFAULT_ROLES},CallBudget())
    with pytest.raises(ValueError,match='not_verified'):
        await workers.run(Node(**node('wait',kind='capability',required_capabilities=['ui.wait'])),{},'asus',{},'turn_parent')
    setup.control.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_hub_internal_fast_path_cannot_escalate_delegated_role(setup):
    # A legacy internal control may return allowed. Delegation still uses the
    # enrolled caller's full RBAC, including command lists and per-device denies.
    await enroll_device('restricted','Restricted','restricted','fixture-hash')
    delegated=DelegatedControl(setup.control)
    assert not (await delegated.check_permissions('restricted','restricted','goal.wait')).allowed
    from hub.database import set_device_permission
    await set_device_permission('asus','goal.wait','deny')
    assert not (await delegated.check_permissions('asus','asus','goal.wait')).allowed
    assert not (await delegated.check_permissions('unenrolled','asus','goal.wait')).allowed


@pytest.mark.asyncio
async def test_normal_agentturn_to_graph_workers_executor_and_unified_response(setup):
    """Entire production composition; only provider output and OS endpoint are fixtures."""
    from hub.pipeline.pipeline import RequestPipeline
    from hub.intent_schema import ModelResult
    from test_a13_discovery import connected,batch,enrolled
    from hub.discovery import reconcile
    await enrolled();reg,_=connected();reconcile(reg,'asus',batch())
    setup.monkeypatch.setattr('hub.orchestration.eligible_catalog',real_eligible_catalog)
    arrived=set();both=asyncio.Event()
    plan=graph(node('alpha'),node('beta'),
        node('pause',kind='capability',required_capabilities=['ui.wait']),
        node('combine',dependencies=['alpha','beta','pause']))
    class Provider(RealPathProvider):
        async def generate_result(self,text,ctx,on_chunk=None):
            if ctx.get('mode')=='result_interpretation':
                return ModelResult(type='RESPONSE',response_text=ctx['tool_result']['message'])
            title=ctx.get('decision_schema',{}).get('title')
            if title=='TaskGraph':value=plan.model_dump()
            elif title=='WorkerOutput':
                payload=json.loads(text)
                if not payload['dependencies']:
                    arrived.add(payload['objective']+str(len(arrived)))
                    if len(arrived)==2:both.set()
                    await asyncio.wait_for(both.wait(),1)
                value={'value':'Analysis supported by supplied dependencies.'}
            elif title=='Synthesis':
                value={'answer':'Combined analysis; the executor wait receipt succeeded.',
                    'evidence_nodes':['alpha','beta','pause','combine']}
            elif title=='Verification':
                value={'supported':True,'criteria_satisfied':True,
                    'checked_nodes':['alpha','beta','pause','combine']}
            else:
                delegated=text.startswith('{')
                value={'decision':'CALL_CAPABILITY','capability':'ui.wait' if delegated else 'agent.orchestrate',
                       'arguments':{'seconds':1} if delegated else {}}
            return ModelResult(type='RESPONSE',response_text=json.dumps(value))
    provider=Provider()
    setup.monkeypatch.setattr('hub.llm_provider._active_provider',provider)
    result=await RequestPipeline(setup.control,provider).execute('asus',
        'Coordinate independent analyses and a harmless wait, then combine their verified results.',
        {'_request_delivery_owned':True,'privacy_class':'INTERNAL','response_channel':'gui'},'e2e-a14')
    assert result.status=='success' and result.message=='Combined analysis; the executor wait receipt succeeded.'
    assert len(arrived)==2 and setup.control.execute_and_observe.await_count==1
    from hub.database import get_tasks
    rows=await get_tasks()
    record=next(r for r in rows if r['type']=='orchestration')
    saved=context(record)['orchestration']
    assert saved['verification']['verified'] is True
    assert saved['nodes']['pause']['result']['receipts'][0]['status']=='success'
    assert any(item.title=='Multi-agent subtasks' and len(item.rows)==4 for item in project('asus',rows) if item.kind=='table')


@pytest.mark.asyncio
async def test_orchestration_contract_preserves_model_output_and_request_data():
    from hub.orchestration_contract import OrchestrationContract
    raw=object()
    provider=SimpleNamespace(generate_result=AsyncMock(return_value=raw))
    adapter=OrchestrationContract(provider)
    ctx={'mode':'agent_decision','system_prompt':'Existing policy.\nSTATE:\n{"task_id":"opaque"}',
         'privacy_class':'SENSITIVE','allow_provider_fallback':False,
         'decision_schema':{'properties':{'capability':{'enum':['ui.wait','agent.orchestrate',None]}}}}
    original=json.loads(json.dumps(ctx))
    assert await adapter.generate_result('An unseen request.',ctx) is raw
    text,sent=provider.generate_result.call_args.args
    assert text=='An unseen request.' and ctx==original
    assert sent['privacy_class']=='SENSITIVE' and sent['allow_provider_fallback'] is False
    assert sent['decision_schema']==ctx['decision_schema']
    assert sent['system_prompt'].endswith(ctx['system_prompt'])
    assert json.loads(sent['system_prompt'].split('\nSTATE:\n')[1])=={'task_id':'opaque'}
    assert provider.generate_result.await_count==1
    callback=AsyncMock()
    assert await adapter.generate_result('streamed request',ctx,callback) is raw
    assert provider.generate_result.call_args.kwargs['on_chunk'] is callback


@pytest.mark.asyncio
async def test_orchestration_contract_leaves_non_admission_calls_unchanged():
    from hub.orchestration_contract import OrchestrationContract
    provider=SimpleNamespace(generate_result=AsyncMock(return_value='invalid output'))
    adapter=OrchestrationContract(provider)
    for ctx in ({'mode':'result_interpretation','system_prompt':'Existing interpretation.'},
                {'mode':'agent_decision','decision_schema':{'title':'TaskGraph'}},
                {'mode':'agent_decision','decision_schema':{'properties':{'capability':{'enum':['ui.wait']}}}}):
        assert await adapter.generate_result('request',ctx)=='invalid output'
        assert provider.generate_result.call_args.args[1] is ctx


@pytest.mark.asyncio
async def test_orchestration_contract_preserves_existing_model_selection_and_recipient():
    from hub.orchestration_contract import OrchestrationContract
    route=SimpleNamespace(name='selected',model='chosen',locality='private_server',
        authorize_recipient=AsyncMock(),generate_result=AsyncMock())
    selector=SimpleNamespace(select_for_turn=AsyncMock(return_value=(route,{'privacy_class':'PERSONAL'})))
    wrapped,ctx=await OrchestrationContract(selector).select_for_turn('request',{'privacy_class':'PERSONAL'},[])
    assert wrapped.provider is route and wrapped.name=='selected' and wrapped.model=='chosen'
    assert wrapped.locality=='private_server' and ctx['privacy_class']=='PERSONAL'
    selector.select_for_turn.assert_awaited_once()
    selector.generate_result=AsyncMock()
    await wrapped.authorize_recipient('PERSONAL')
    route.authorize_recipient.assert_awaited_once_with('PERSONAL')


@pytest.mark.asyncio
async def test_orchestration_contract_accepts_existing_keyword_extension_provider():
    from hub.orchestration_contract import OrchestrationContract
    callback=AsyncMock()
    observed=[]
    class Provider:
        async def generate_result(self,text,context,**kwargs):
            observed.append(kwargs)
            return 'unmodified model output'
    assert await OrchestrationContract(Provider()).generate_result('request',{})=='unmodified model output'
    assert await OrchestrationContract(Provider()).generate_result('request',{},callback)=='unmodified model output'
    assert observed==[{}, {'on_chunk':callback}]


def test_admission_contract_is_adjacent_to_catalog_without_losing_policy_or_data():
    from hub.agent_turn import _build_agent_system_prompt
    from hub.orchestration_contract import admission_prompt, CONTRACT
    from hub.capability_registry import CanonicalCapabilityRegistry
    cap=CanonicalCapabilityRegistry.get_instance().get_capability('agent.orchestrate')
    assert cap.availability and cap.target_type=='hub' and cap.arguments=={}
    from hub.permissions import COMMAND_TIERS
    assert COMMAND_TIERS[cap.name]==cap.permission_tier
    catalog=[dict(name=cap.name,description=cap.description,arguments={},devices=[])]
    state={'inspected_capabilities':[],'unavailable_capabilities':[],'task_id':'actual-task'}
    original=_build_agent_system_prompt(catalog,state)
    changed=admission_prompt(original)
    before,data=original.split('\nCAPABILITIES:\n',1)
    assert changed.startswith(before) and changed.endswith('\nCAPABILITIES:\n'+data)
    assert changed.index(CONTRACT)>changed.index('Prefer native capabilities')
    assert 'registered HUB' in changed and 'unavailable_capabilities says otherwise' in changed
    assert json.loads(changed.split('\nSTATE:\n')[1])['task_id']=='actual-task'


@pytest.mark.asyncio
async def test_delegated_provider_keeps_state_parseable_and_actual_model_output():
    raw=object()
    provider=SimpleNamespace(generate_result=AsyncMock(return_value=raw))
    budget=CallBudget()
    ctx={'decision_schema':{'properties':{'capability':{'enum':['ui.wait','agent.orchestrate']}}},
         'system_prompt':'Keep all original safety rules.\nCAPABILITIES:\n{}\nSTATE:\n{"task_id":"worker"}'}
    original=json.loads(json.dumps(ctx))
    assert await BudgetProvider(provider,budget,{'ui.wait'}).generate_result('objective',ctx) is raw
    sent=provider.generate_result.call_args.args[1]
    assert json.loads(sent['system_prompt'].split('\nSTATE:\n')[1])=={'task_id':'worker'}
    assert sent['decision_schema']['properties']['capability']['enum']==['ui.wait',None]
    assert ctx==original and budget.remaining==31


def test_compact_schema_removes_annotations_without_removing_field_names_or_constraints():
    from hub.orchestration_inference import compact_schema, proposal_schema
    source={'title':'Decoration','type':'object','additionalProperties':False,
        'properties':{'title':{'type':'string','default':'x','maxLength':3},
                      'default':{'enum':[{'title':'keep','default':'keep'}]}}}
    result=compact_schema(source)
    assert result=={'type':'object','additionalProperties':False,
        'properties':{'title':{'type':'string','maxLength':3},
                      'default':{'enum':[{'title':'keep','default':'keep'}]}}}
    from hub.orchestration_protocol import Synthesis, Verification
    for schema in (TaskGraph,WorkerOutput,Synthesis,Verification):
        original=schema.model_json_schema();compact=proposal_schema(schema)
        assert compact_schema(original)==compact_schema(compact)
        assert len(json.dumps(compact))<len(json.dumps(original))
        assert compact['title']==schema.__name__ and compact['additionalProperties'] is False


@pytest.mark.parametrize('wrapper',['plain','fenced'])
def test_graph_json_boundary_accepts_complete_proposals_and_preserves_protocol(wrapper):
    from hub.orchestration_inference import parse_proposal
    proposed=graph(node('first'),node('second'),node('dependent',dependencies=['first','second']))
    raw=proposed.model_dump_json()
    if wrapper=='fenced':raw='```json\n'+raw+'\n```'
    assert parse_proposal(TaskGraph,raw)==proposed


@pytest.mark.parametrize('raw',[
    '{"nodes":[]}', '{"decision":"RESPOND","response_text":"Done"}',
    'prefix {"nodes":[]} suffix', '{"nodes":[', '', None,
    '{"nodes":[],"sensitive-injected-key-DO-NOT-ECHO":"private input"}',
])
def test_invalid_graph_feedback_is_bounded_classified_and_never_repairs_a_graph(raw):
    from hub.orchestration_inference import parse_proposal, OrchestrationOutputError, validation_feedback
    from hub.provider_failover import FailoverManager
    with pytest.raises(OrchestrationOutputError) as captured:
        parse_proposal(TaskGraph,raw)
    feedback=validation_feedback(captured.value)
    assert feedback['category']=='INVALID_OUTPUT' and len(feedback['issues'])<=6
    assert 'private input' not in json.dumps(feedback) and 'DO-NOT-ECHO' not in json.dumps(feedback)
    assert FailoverManager.classify(captured.value).category=='INVALID_OUTPUT'


@pytest.mark.asyncio
async def test_worker_inference_uses_its_actual_graph_schema_and_reports_parse_failure(setup):
    from hub.orchestration_inference import OrchestrationOutputError
    from hub.orchestration_protocol import WorkerRole
    from hub.intent_schema import ModelResult
    class Provider:
        name='local';model='fixture';configured=True;locality='local_device'
        async def generate_result(self,text,ctx):
            assert ctx['decision_schema']['title']=='TaskGraph'
            assert 'not an AgentDecision or ModelResult wrapper' in ctx['system_prompt']
            assert 'selected_memory' not in text
            return ModelResult(type='RESPONSE',response_text='{"decision":"RESPOND"}')
    worker=Workers(setup.control,Provider(),{},CallBudget())
    with pytest.raises(OrchestrationOutputError):
        await worker.infer(TaskGraph,{'request':'Decompose independent work.'},'asus',{},'turn_parent',
                           WorkerRole(name='planner',purpose='Decompose supplied work.',category='planning'))
    assert worker.budget.remaining==31
    setup.control.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('capability,arguments',[('agent.orchestrate',{}),('ui.wait',{'seconds':1})])
async def test_admission_contract_does_not_replace_complex_or_simple_model_choices(capability,arguments):
    from hub.agent_turn import _get_model_decision
    from hub.orchestration_contract import OrchestrationContract
    from hub.intent_schema import ModelResult
    provider=SimpleNamespace(generate_result=AsyncMock(return_value=ModelResult(type='RESPONSE',
        response_text=json.dumps({'decision':'CALL_CAPABILITY','capability':capability,'arguments':arguments}))))
    catalog=[{'name':name,'description':'Registered capability.','arguments':{},'devices':[]}
             for name in ('agent.orchestrate','ui.wait')]
    result=await _get_model_decision('An unseen public request.',catalog,{},[],OrchestrationContract(provider))
    assert result.capability==capability and result.arguments==arguments
    assert provider.generate_result.await_count==1


@pytest.mark.parametrize('status,code,expected',[
    (400,'tool_use_failed','INVALID_OUTPUT'),(413,'rate_limit_exceeded','CONTEXT_TOO_LARGE'),
    (429,'rate_limit_exceeded','RATE_LIMIT_429'),(503,None,'SERVICE_UNAVAILABLE_503'),
])
def test_provider_failure_classification_remains_frozen_and_explicit(status,code,expected):
    from hub.llm_provider import ProviderHTTPError
    from hub.provider_failover import FailoverManager
    error=ProviderHTTPError('groq',status)
    error.provider_error_code=code
    assert FailoverManager.classify(error).category==expected


@pytest.mark.asyncio
@pytest.mark.parametrize('model',['openai/gpt-oss-120b','qwen/qwen3.8-27b'])
async def test_native_tool_transport_accepts_actual_graph_schema_without_decision_wrapper(setup,model):
    import httpx
    from hub.direct_provider import DirectLLMProvider
    from hub.orchestration_protocol import WorkerRole
    proposed=graph(node('alpha'),node('beta'),node('combined',dependencies=['alpha','beta']))
    def respond(request):
        wire=json.loads(request.content)
        spec=wire['tools'][0]['function']
        assert spec['parameters']['title']=='TaskGraph'
        assert 'nodes' in spec['parameters']['properties'] and 'decision' not in spec['parameters']['properties']
        assert wire['tool_choice']['function']['name']==spec['name']=='agent_decision'
        return httpx.Response(200,json={'choices':[{'finish_reason':'tool_calls','message':{
            'tool_calls':[{'function':{'name':spec['name'],'arguments':proposed.model_dump_json()}}]}}]})
    provider=DirectLLMProvider('groq',model,api_key='fixture',transport=httpx.MockTransport(respond))
    workers=Workers(setup.control,provider,{},CallBudget())
    value,route=await workers.infer(TaskGraph,{'request':'Independent work and a comparison.'},'asus',
        {'privacy_class':'PUBLIC'},'turn_parent',WorkerRole(name='planner',purpose='Decompose work.',category='planning'))
    assert value==proposed and route['provider']=='groq' and route['model']==model
    setup.control.execute_and_observe.assert_not_awaited()
