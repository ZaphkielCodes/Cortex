"""A13 deterministic lifecycle/security tests; no providers or OS inputs."""
import json
import time
from unittest.mock import AsyncMock
import httpx
import pytest
from pydantic import ValidationError
from shared.discovery import AdvertisementBatch, describe
from hub.capability_registry import CanonicalCapabilityRegistry
from hub.executor_registry import ExecutorRegistry
from hub.device_handle import DeviceHandle
from hub.discovery import reconcile, eligibility, candidates, ensure_dispatch, KEY
from hub.database import enroll_device, update_device_capabilities, set_device_permission
from hub.auth import hash_secret
from test_a11_integrations import env


def connected(name='asus', names=('ui.wait',), **kwargs):
    reg=ExecutorRegistry.get_instance()
    handle=DeviceHandle(device_id=name,display_name=name,role='primary_admin',trust_level='admin',**kwargs)
    reg.register_device(handle);reg.on_device_connected(name,AsyncMock())
    reg.on_device_authenticated(name,list(names))
    return reg,handle


def batch(name='asus',names=('ui.wait',),generation=0):
    reg=CanonicalCapabilityRegistry.get_instance()
    return AdvertisementBatch(device_id=name,generation=generation,
        capabilities=[describe(reg.get_capability(c)) for c in names])


async def enrolled(name='asus',names=('ui.wait',),role='admin'):
    await enroll_device(name,name,role,hash_secret('discovery-fixture-secret'))
    await update_device_capabilities(name,'windows',list(names))


def test_initial_discovery_is_serializable_and_does_not_verify_execution():
    reg,handle=connected();reconcile(reg,'asus',batch())
    assert eligibility(reg,'asus','ui.wait')=='ELIGIBLE'
    json.dumps(handle.to_dict())
    assert handle.metadata[KEY]['entries']['ui.wait']['last_verified_at'] is None
    assert handle.metadata[KEY]['protocol_version']==1


def test_duplicate_reconciliation_is_idempotent_but_duplicate_ids_are_invalid():
    reg,h=connected();msg=batch();reconcile(reg,'asus',msg);reconcile(reg,'asus',msg)
    assert h.capabilities==['ui.wait']
    with pytest.raises(ValidationError):AdvertisementBatch(device_id='asus',capabilities=[*msg.capabilities,*msg.capabilities])


def test_reconnect_requires_authenticated_session_and_replaces_old_generation():
    reg,h=connected();reconcile(reg,'asus',batch(generation=8));reg.on_device_disconnected('asus')
    assert eligibility(reg,'asus','ui.wait')!='ELIGIBLE'
    reg.on_heartbeat('asus')
    with pytest.raises(ValueError):reconcile(reg,'asus',batch())
    reg.on_device_connected('asus',AsyncMock())
    assert eligibility(reg,'asus','ui.wait')=='SESSION_REPLACED'
    reconcile(reg,'asus',batch())
    assert eligibility(reg,'asus','ui.wait')=='ELIGIBLE'


def test_heartbeat_and_advertisement_expire_independently():
    reg,h=connected();reconcile(reg,'asus',batch())
    reg._last_heartbeat_time['asus']=time.monotonic()-reg.heartbeat_timeout-1
    assert eligibility(reg,'asus','ui.wait')=='DEVICE_OFFLINE'
    reg.on_heartbeat('asus');h.metadata[KEY]['expires_mono']=0
    assert eligibility(reg,'asus','ui.wait')=='ADVERTISEMENT_EXPIRED'
    assert not h.supports_capability('ui.wait')
    reconcile(reg,'asus',batch());assert eligibility(reg,'asus','ui.wait')=='ELIGIBLE'


def test_updates_remove_support_without_old_database_resurrection():
    reg,h=connected(names=('ui.wait','fs.list'));reconcile(reg,'asus',batch(names=('ui.wait','fs.list')))
    with pytest.raises(ValueError):reconcile(reg,'asus',batch(names=('ui.wait',)))
    reconcile(reg,'asus',batch(generation=1));assert not h.supports_capability('fs.list')
    with pytest.raises(ValueError):ensure_dispatch('asus','fs.list')
    with pytest.raises(ValueError):reconcile(reg,'asus',batch(generation=0))


@pytest.mark.parametrize('change',['version','schema'])
def test_version_or_schema_mismatch_retains_presence_without_execution(change):
    reg,h=connected();msg=batch()
    if change=='version':msg.capabilities[0].version='2.0'
    else:msg.capabilities[0].input_schema={'type':'object','properties':{'injected':{'type':'string'}}}
    reconcile(reg,'asus',msg)
    assert 'ui.wait' in h.metadata[KEY]['entries'] and not h.supports_capability('ui.wait')
    assert eligibility(reg,'asus','ui.wait')=='SCHEMA_INCOMPATIBLE'
    assert 'injected' not in json.dumps(h.metadata[KEY]['entries']['ui.wait']['advertisement'])


def test_canonical_schema_change_is_revalidated_at_dispatch(monkeypatch):
    reg,h=connected();reconcile(reg,'asus',batch())
    cap=CanonicalCapabilityRegistry.get_instance().get_capability('ui.wait')
    monkeypatch.setattr(cap,'arguments',{'changed':{'type':'boolean','required':True}})
    assert eligibility(reg,'asus','ui.wait')=='SCHEMA_INCOMPATIBLE'
    with pytest.raises(ValueError):ensure_dispatch('asus','ui.wait')


@pytest.mark.parametrize('kind',['identity','invented','policy'])
def test_forged_and_escalating_advertisements_are_atomic_rejections(kind):
    reg,h=connected();reconcile(reg,'asus',batch());prior=h.metadata[KEY]
    msg=batch(generation=1)
    if kind=='identity':msg.device_id='lenovo'
    if kind=='invented':msg.capabilities[0].capability_id='run.arbitrary_code'
    if kind=='policy':msg.capabilities[0].permission_tier=3
    with pytest.raises(ValueError):reconcile(reg,'asus',msg)
    assert h.metadata[KEY] is prior and h.capabilities==['ui.wait']


@pytest.mark.parametrize('extra',[{'$ref':'https://evil.invalid/schema'}, {'pattern':'(a+)+$'}])
def test_schema_references_and_executable_extensions_are_rejected(extra):
    msg=batch().model_dump();msg['capabilities'][0]['input_schema']=extra
    with pytest.raises(ValidationError):AdvertisementBatch.model_validate(msg)


@pytest.mark.asyncio
async def test_permission_revocation_is_checked_immediately_before_send():
    await enrolled();reg,h=connected();reconcile(reg,'asus',batch())
    from hub.control_layer import CortexControlLayer
    ctrl=CortexControlLayer({'asus':reg._live_connections['asus']})
    await set_device_permission('asus','ui.wait','deny')
    with pytest.raises(ValueError):await ctrl.execute_command('asus','ui.wait',{})
    reg._live_connections['asus'].send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_or_removed_advertisements_never_dispatch():
    reg,h=connected();reconcile(reg,'asus',batch());h.metadata[KEY]['expires_mono']=0
    from hub.control_layer import CortexControlLayer
    result=await CortexControlLayer({'asus':reg._live_connections['asus']}).execute_and_observe('asus','ui.wait',{})
    assert result['status']=='failure' and 'ADVERTISEMENT_EXPIRED' in result['message']
    reg._live_connections['asus'].send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_selection_respects_device_support_and_ownership():
    reg,h=connected();reconcile(reg,'asus',batch())
    _,second=connected('lenovo',names=('fs.list',));reconcile(reg,'lenovo',batch('lenovo',('fs.list',)))
    from hub.executor_selector import ExecutorSelector
    selector=ExecutorSelector(registry=reg)
    assert selector.select_executor('ui.wait',source_device='asus').selected_device=='asus'
    assert not selector.select_executor('ui.wait',requested_device='lenovo',source_device='asus').is_success
    reg.on_device_disconnected('asus')
    assert not selector.select_executor('ui.wait',owning_device='asus',source_device='lenovo').is_success


@pytest.mark.asyncio
async def test_oauth_scopes_multiaccount_revocation_and_provider_recovery(env):
    from hub.integrations.definitions import G
    await env.connect(account='reader',scopes=[G+'gmail.readonly'])
    await env.connect(account='wrongscope',scopes=[G+'contacts.readonly'])
    rows=await candidates('asus');email=[r for r in rows if r['capability_id']=='email.search']
    assert len(email)==2 and sum(r['executable'] for r in email)==1
    assert all(not r['executable'] for r in rows if r['capability_id']=='email.send')
    assert 'A11_ACCESS_TOKEN_CANARY' not in json.dumps(rows) and '@example.invalid' not in json.dumps(rows)
    env.registry.health['google_gmail']['cooldown_until']=time.time()+60
    assert all(not r['executable'] for r in await candidates('asus') if r['capability_id']=='email.search')
    env.registry.health['google_gmail'].update(cooldown_until=0,state='healthy')
    assert any(r['executable'] for r in await candidates('asus') if r['capability_id']=='email.search')
    from hub.integrations.google_auth import account_key
    env.vault[account_key('gmail','reader')]['revoked']=True
    assert not any(r['executable'] for r in await candidates('asus') if r['capability_id']=='email.search')


@pytest.mark.asyncio
async def test_presence_availability_and_authorization_are_independent():
    await enrolled();reg,h=connected();reconcile(reg,'asus',batch());reg.on_device_disconnected('asus')
    row=next(r for r in await candidates('asus') if r['provider_id']=='asus')
    assert row['present'] and row['authorized'] and not row['available'] and not row['executable']
    assert row['state']=='UNAVAILABLE'


def test_agent_discovery_tracks_registered_handlers_and_runtime_suppression(monkeypatch):
    from agent import executor
    from agent.discovery import advertisement
    assert 'ui.wait' in [c.capability_id for c in advertisement('asus').capabilities]
    monkeypatch.delitem(executor.HANDLERS,'ui.wait')
    assert 'ui.wait' not in [c.capability_id for c in advertisement('asus').capabilities]
    monkeypatch.setenv('CORTEX_SUPPRESS_CAPS','fs.list')
    assert 'fs.list' not in [c.capability_id for c in advertisement('asus').capabilities]


@pytest.mark.asyncio
async def test_drive_is_deferred_and_local_only_excludes_external_integrations(env):
    await env.connect()
    rows=await candidates('asus',privacy_context={'preferred_execution_scope':'local_only'})
    assert all(not r['executable'] for r in rows)
    assert all(r['reason']=='DEFERRED' for r in rows if r['provider_id']=='google_drive')


@pytest.mark.asyncio
async def test_untrusted_worker_cannot_inspect_remote_device_or_accounts():
    await enrolled('worker',role='sub_admin');await enrolled('asus')
    reg,h=connected('worker');reconcile(reg,'worker',batch('worker'))
    connected();reconcile(reg,'asus',batch())
    rows=await candidates('worker')
    assert rows and all(r['provider_id']=='worker' for r in rows)


@pytest.mark.asyncio
async def test_read_api_is_authenticated_and_workspace_uses_data_only_tables():
    await enrolled();reg,h=connected();reconcile(reg,'asus',batch())
    from hub.main import app
    headers={'X-Cortex-Device':'asus','X-Cortex-Secret':'discovery-fixture-secret'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.get('/api/discovery')).status_code==401
        response=await c.get('/api/discovery',headers=headers);assert response.status_code==200
        assert any(r['capability_id']=='ui.wait' and r['executable'] for r in response.json()['candidates'])
        assert (await c.post('/api/discovery',headers=headers,json={'command':'run_shell'})).status_code==405
        view=(await c.post('/api/workspace/sync',headers=headers,json={})).json()
        tables=[i for i in view['items'] if i['provenance']['source']=='capability_discovery' and i['kind']=='table']
        assert tables and any(row[0]=='ui.wait' for t in tables for row in t['rows'])
        assert all('actions' not in i for i in tables)


@pytest.mark.asyncio
async def test_only_correlated_success_receipt_updates_verification():
    from hub.discovery import record_verified_result
    from hub.models import ResultMessage
    from hub.database import record_command,update_command_status
    reg,h=connected();reconcile(reg,'asus',batch())
    msg=ResultMessage(command_id='discovery-receipt',command='ui.wait',status='success',message='Waited')
    await record_verified_result('asus',msg)
    assert h.metadata[KEY]['entries']['ui.wait']['last_verified_at'] is None
    await record_command(msg.command_id,msg.command,'asus','asus')
    await update_command_status(msg.command_id,'dispatched')
    await record_verified_result('lenovo',msg)
    assert h.metadata[KEY]['entries']['ui.wait']['last_verified_at'] is None
    await record_verified_result('asus',msg)
    assert h.metadata[KEY]['entries']['ui.wait']['last_verified_at']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['identity','unknown_capability','policy'])
async def test_rejected_handshake_preserves_existing_authenticated_registry(monkeypatch,kind):
    from types import SimpleNamespace
    from hub import main
    from hub.database import get_device_capabilities
    await enrolled();reg,handle=connected();reconcile(reg,'asus',batch())
    original=reg._live_connections['asus']
    monkeypatch.setattr(main,'CONNECTED_DEVICES',{'asus':original})
    before=json.dumps(handle.to_dict(),sort_keys=True)
    payload=batch(generation=1).model_dump()
    if kind=='identity':payload['device_id']='lenovo'
    elif kind=='unknown_capability':payload['capabilities'][0]['capability_id']='invented.execute'
    else:payload['capabilities'][0]['permission_tier']=3
    wire={'type':'auth_response','device_id':'asus','response':'discovery-fixture-secret',
          'capabilities':[],'discovery':payload}
    socket=SimpleNamespace(accept=AsyncMock(),send_text=AsyncMock(),close=AsyncMock(),
                           receive_text=AsyncMock(return_value=json.dumps(wire)))
    await main.agent_endpoint(socket,'asus')
    socket.close.assert_awaited_once_with(code=4003,reason='Invalid capability discovery')
    assert reg._live_connections.get('asus') is original
    assert main.CONNECTED_DEVICES['asus'] is original
    assert json.dumps(handle.to_dict(),sort_keys=True)==before
    assert await get_device_capabilities('asus')==['ui.wait']


@pytest.mark.asyncio
async def test_workspace_separates_availability_authorization_and_approval():
    from hub.workspace import WorkspaceStore,Cursor
    await enrolled(names=('ui.wait','fs.delete'))
    reg,h=connected(names=('ui.wait','fs.delete'))
    reconcile(reg,'asus',batch(names=('ui.wait','fs.delete')))
    store=WorkspaceStore()
    online=await store.sync('asus',Cursor())
    def rows(view):
        return {row[0]:row for item in view.items if item.kind=='table' and item.provenance.source=='capability_discovery' for row in item.rows}
    visible=rows(online)
    assert visible['ui.wait'][1]=='asus / local_device'
    assert visible['ui.wait'][2:5]==['Available','Authorized (Tier 1)','EXECUTABLE']
    assert visible['fs.delete'][2]=='Available'
    assert visible['fs.delete'][4:] == ['APPROVAL_REQUIRED','APPROVAL_REQUIRED']
    reg.on_device_disconnected('asus')
    offline=rows(await store.sync('asus',Cursor(epoch=online.epoch,revision=online.revision)))
    assert offline['ui.wait'][2:]==['Unavailable','Authorized (Tier 1)','UNAVAILABLE','DEVICE_OFFLINE']
