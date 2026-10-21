"""Deterministic service, account, policy and AgentTurn contracts. No external I/O."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio

from hub.integrations.ecosystem import IntegrationRegistry, NormalizedResult
from hub.integrations.definitions import AuthState, Effect, G
from hub.integrations.google_api import GoogleAPI
from hub.integrations.google_auth import GoogleOAuth, IntegrationError, account_key
from hub.credentials import CredentialStore
from hub.database import add_integration, enroll_device, create_confirmation, resolve_confirmation, get_confirmation
from hub.capability_registry import CanonicalCapabilityRegistry


ACCESS="A11_ACCESS_TOKEN_CANARY_812"
REFRESH="A11_REFRESH_TOKEN_CANARY_913"


@pytest_asyncio.fixture
async def env(monkeypatch):
    vault={}
    monkeypatch.setattr(CredentialStore,'get_tokens',lambda key:json.loads(json.dumps(vault[key])) if key in vault else None)
    def store(key,value):vault[key]=json.loads(json.dumps(value));return True
    monkeypatch.setattr(CredentialStore,'store_tokens',store)
    monkeypatch.setattr(CredentialStore,'delete_tokens',lambda key:vault.pop(key,None) is not False)
    monkeypatch.setenv('GOOGLE_CLIENT_ID','fixture-client')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET','fixture-client-secret')
    monkeypatch.delenv('GOOGLE_MAPS_API_KEY',raising=False)
    monkeypatch.delenv('CORTEX_GOOGLE_MAPS_ENABLED',raising=False)
    registry=IntegrationRegistry()
    monkeypatch.setattr(IntegrationRegistry,'_instance',registry)
    CanonicalCapabilityRegistry.reset()
    await enroll_device('asus','Fixture device','admin','fixture-hash')
    async def connect(service='gmail',account='primary',scopes=None,revoked=False,expired=False,default=False):
        scopes=scopes if scopes is not None else [G+'gmail.readonly']
        vault[account_key(service,account)]={'access_token':ACCESS,'refresh_token':REFRESH,'scopes':scopes,
            'identity':{'sub':'fixture-'+account,'email':account+'@example.invalid'},'revoked':revoked,
            'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=-60 if expired else 3600)).isoformat()}
        await add_integration({'id':account_key(service,account),'plugin_name':service,'account_id':account,
            'status':'revoked' if revoked else 'active','config':{'default':default}})
        await registry.sync_connections()
    return SimpleNamespace(registry=registry,vault=vault,connect=connect,monkeypatch=monkeypatch)


def adapter(env,provider,handler):
    api=GoogleAPI(provider,{'google_gmail':'gmail','google_calendar':'calendar','google_drive':'drive',
        'google_contacts':'contacts','google_places':'places','google_geocoding':'geocoding','google_routes':'routes'}[provider],httpx.MockTransport(handler))
    env.registry.register(provider,api)
    return api


async def grant(env,capability,args,source='asus',decision='approved'):
    import uuid
    cid=uuid.uuid4().hex
    await create_confirmation(cid,'asus',capability,[],source,3)
    env.registry.bind_approval(cid,capability,args,source)
    await resolve_confirmation(cid,decision,source)
    return cid


@pytest.mark.asyncio
async def test_one_canonical_registry_contains_all_seven_providers_and_thirteen_capabilities(env):
    assert len(env.registry.providers)==7 and len(env.registry.capabilities)==13
    view=CanonicalCapabilityRegistry.get_instance().get_capability('email.search')
    assert view.provider=='google_gmail' and view.target_type=='hub'
    assert view.provider_status['auth_state']=='UNAUTHORIZED' and not view.availability
    assert view.privacy_metadata['result_schema']['properties']['data']['properties']['items']['items']['additionalProperties'] is False
    env.registry.unregister('google_gmail')
    assert env.registry.capability_status(env.registry.get('email.search'))['auth_state']=='UNCONFIGURED'


@pytest.mark.asyncio
async def test_connected_disconnected_scope_and_revoked_states_are_distinct(env):
    await env.connect(scopes=[G+'gmail.send'])
    state=env.registry.capability_status(env.registry.get('email.search'))
    assert not state['available'] and state['auth_state']=='SCOPE_MISSING'
    send=env.registry.capability_status(env.registry.get('email.send'))
    assert send['available']
    env.vault['gmail_primary']['revoked']=True
    await env.registry.sync_connections()
    with pytest.raises(IntegrationError) as error:env.registry.select_account(env.registry.get('email.send'),'primary')
    assert error.value.diagnostic=='REVOKED'
    env.vault.clear();await env.registry.sync_connections()
    assert not (await env.registry.statuses())[0]['connected']


@pytest.mark.asyncio
async def test_multi_account_selection_is_explicit_or_default_and_not_global(env):
    await env.connect(account='school');await env.connect(account='personal')
    cap=env.registry.get('email.search')
    with pytest.raises(IntegrationError) as error:env.registry.select_account(cap)
    assert error.value.diagnostic=='ACCOUNT_SELECTION_REQUIRED'
    assert env.registry.select_account(cap,'school').account_id=='school'
    await env.registry.set_default('google_gmail','personal')
    assert env.registry.select_account(cap).account_id=='personal'
    assert env.registry.select_account(cap,'school').account_id=='school'


@pytest.mark.asyncio
async def test_read_api_is_minimized_normalized_and_has_provenance(env,caplog):
    import logging
    caplog.set_level(logging.INFO, logger="httpx")
    await env.connect();seen=[]
    def response(request):
        seen.append(request)
        return httpx.Response(200,json={'messages':[{'id':'m1','threadId':'t1','access_token':ACCESS,'metadata':'unneeded'}],'opaque':'do-not-leak'})
    adapter(env,'google_gmail',response)
    result=await env.registry.execute('email.search',{'query':'subject:registration','max_results':2},'asus')
    assert result['status']=='success' and result['data']['items']==[{'id':'m1','threadId':'t1'}]
    assert result['provider']=='google_gmail' and result['provenance']['account']=='primary'
    assert result['provenance']['resource_ids']==['m1'] and result['retrieved_at']
    assert seen[0].headers['authorization']=='Bearer '+ACCESS
    assert 'opaque' not in json.dumps(result)
    assert ACCESS not in json.dumps(result)+caplog.text and REFRESH not in caplog.text
    assert 'subject:registration' not in caplog.text
    NormalizedResult.model_validate(result)


@pytest.mark.asyncio
async def test_missing_scope_and_revoked_auth_never_call_provider(env):
    await env.connect(scopes=[])
    api=adapter(env,'google_gmail',lambda request:(_ for _ in ()).throw(AssertionError('Unexpected network call')))
    result=await env.registry.execute('email.search',{'query':'synthetic'},'asus')
    assert result['error']['category']=='SCOPE_MISSING'
    env.vault['gmail_primary']['revoked']=True
    result=await env.registry.execute('email.search',{'query':'synthetic','account_id':'primary'},'asus')
    assert result['error']['category']=='AUTH_REQUIRED'


@pytest.mark.asyncio
@pytest.mark.parametrize('args',[{'query':'x','access_token':ACCESS},{'query':'x','max_results':100},{'query':3},{'query':'x','account_id':'../other'}])
async def test_argument_schema_rejects_unknown_secret_fields_and_bad_types(env,args):
    result=await env.registry.execute('email.search',args,'asus')
    assert result['error']['category']=='INVALID_ARGUMENT'
    assert ACCESS not in json.dumps(result)


@pytest.mark.asyncio
async def test_write_requires_exact_account_arguments_and_one_use_approval(env):
    await env.connect(scopes=[G+'gmail.send']);calls=[]
    adapter(env,'google_gmail',lambda request:(calls.append(request) or httpx.Response(200,json={'id':'sent1','threadId':'thread1'})))
    args={'to':'test@example.invalid','subject':'Fixture','body':'Synthetic body','account_id':'primary'}
    result=await env.registry.execute('email.send',args,'asus')
    assert result['status']=='needs_approval' and not calls
    await grant(env,'email.send',args)
    result=await env.registry.execute('email.send',dict(args,to='other@example.invalid'),'asus')
    assert result['status']=='needs_approval' and not calls
    result=await env.registry.execute('email.send',args,'asus')
    assert result['status']=='success' and len(calls)==1
    again=await env.registry.execute('email.send',args,'asus')
    assert again['status']=='needs_approval' and len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('decision',['denied','expired'])
async def test_denied_or_expired_write_approval_never_executes(env,decision):
    await env.connect(scopes=[G+'gmail.send'])
    api=adapter(env,'google_gmail',lambda request:(_ for _ in ()).throw(AssertionError()))
    args={'to':'test@example.invalid','subject':'Fixture','body':'Synthetic'}
    await grant(env,'email.send',args,decision=decision)
    result=await env.registry.execute('email.send',args,'asus')
    assert result['status']=='needs_approval'


@pytest.mark.asyncio
async def test_unverified_write_response_is_not_success_and_cannot_replay(env):
    await env.connect(scopes=[G+'gmail.send']);calls=[]
    adapter(env,'google_gmail',lambda request:(calls.append(request) or httpx.Response(200,json={})))
    args={'to':'test@example.invalid','subject':'Fixture','body':'Synthetic'}
    await grant(env,'email.send',args)
    result=await env.registry.execute('email.send',args,'asus')
    assert result['status']=='failure' and result['side_effects_possible'] and not result['retry_safe']
    assert result['error']['diagnostic']=='UNVERIFIED_RESPONSE' and len(calls)==1
    env.registry.health['google_gmail']['cooldown_until']=0
    assert (await env.registry.execute('email.send',args,'asus'))['status']=='needs_approval'
    assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('status,category',[(400,'INVALID_ARGUMENT'),(401,'AUTH_REQUIRED'),(403,'PERMISSION_DENIED'),(404,'NOT_FOUND'),(409,'CONFLICT'),(429,'RATE_LIMITED'),(503,'PROVIDER_UNAVAILABLE')])
async def test_provider_errors_are_normalized_without_error_bodies(env,status,category,caplog):
    await env.connect()
    adapter(env,'google_gmail',lambda request:httpx.Response(status,json={'error':{'message':ACCESS+REFRESH}},headers={'retry-after':'2'}))
    result=await env.registry.execute('email.search',{'query':'synthetic'},'asus')
    assert result['error']['category']==category and result['error']['http_status']==status
    assert ACCESS not in json.dumps(result)+caplog.text and REFRESH not in json.dumps(result)+caplog.text
    if status in (429,503):
        assert env.registry.preference('email.search',['ui.observe'])['preferred']=='ui.observe'
    from hub.provider_health import ProviderHealthStore
    assert ProviderHealthStore().get('gemini').state=='unknown'


@pytest.mark.asyncio
async def test_read_preference_and_a7_privacy_derive_from_capability_metadata(env):
    await env.connect()
    assert env.registry.preference('email.search',['ui.observe'])['preferred']=='email.search'
    from hub.privacy import classify_task_privacy
    assert classify_task_privacy('email.search',{},{}).privacy_class=='SENSITIVE'
    cap=CanonicalCapabilityRegistry.get_instance().get_capability('email.search')
    assert cap.privacy_metadata['data_class']=='SENSITIVE'
    assert CanonicalCapabilityRegistry.get_instance().list_capabilities()[0].provider_status['transport']=='structured_api'
    env.registry.unregister('google_gmail')
    assert env.registry.preference('email.search',['ui.observe'])['ui_fallback_eligible']


@pytest.mark.asyncio
async def test_no_credential_canary_crosses_email_readback_or_events(env):
    await env.connect()
    body=base64.urlsafe_b64encode(('Relevant text '+ACCESS+' '+REFRESH).encode()).decode()
    adapter(env,'google_gmail',lambda request:httpx.Response(200,json={'id':'message1','payload':{'mimeType':'text/plain','headers':[{'name':'Subject','value':'Fixture'},{'name':'X-Secret','value':ACCESS}],'body':{'data':body}}}))
    result=await env.registry.execute('email.read',{'message_id':'message1','include_body':True},'asus')
    assert result['status']=='success' and '[redacted]' in result['data']['items'][0]['body']
    assert ACCESS not in json.dumps(result)+json.dumps(list(env.registry.recent))


@pytest.mark.asyncio
async def test_kill_switch_and_private_only_context_cannot_be_bypassed(env):
    await env.connect();calls=[]
    adapter(env,'google_gmail',lambda request:(calls.append(request) or httpx.Response(200,json={})))
    result=await env.registry.execute('email.search',{'query':'synthetic'},'asus',{'privacy_class':'HIGHLY_SENSITIVE'})
    assert result['error']['diagnostic']=='PRIVACY_POLICY' and not calls
    from hub.goals import GoalManager
    await GoalManager.get_instance().set_autonomy_enabled(False)
    result=await env.registry.execute('email.search',{'query':'synthetic'},'asus')
    assert result['error']['diagnostic']=='KILL_SWITCH' and not calls


@pytest.mark.asyncio
async def test_agentturn_discovers_invokes_and_interprets_canonical_read(env):
    await env.connect()
    adapter(env,'google_gmail',lambda request:httpx.Response(200,json={'messages':[{'id':'m1','threadId':'t1'}]}))
    from test_agent_turn_runtime import ScriptedModel,call,control
    from hub.agent_turn import AgentTurn
    model=ScriptedModel([call('email.search',query='subject:enrollment')],answer='I found one matching message.')
    ctrl=control([])
    result=await AgentTurn(ctrl,model).run('Find the enrollment message','asus',{'response_channel':'gui'})
    assert result.status=='success',result
    assert result.message=='I found one matching message.'
    assert 'email.search' in model.calls[0][1]['system_prompt']
    assert 'm1' in json.dumps(model.calls[1])
    assert ACCESS not in json.dumps(model.calls)
    ctrl.execute_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_registry_exposes_true_accounts_not_database_flags(env):
    await add_integration({'id':'gmail_primary','plugin_name':'gmail','account_id':'primary','status':'active'})
    from hub.system_registry import SystemRegistry
    rows=await SystemRegistry.get_instance().list_integrations()
    gmail=next(r for r in rows if r['key']=='gmail')
    assert not gmail['connected'] and gmail['accounts'][0]['auth_state']=='UNAUTHORIZED'
    assert len(rows)==7 and ACCESS not in json.dumps(rows)


@pytest.mark.asyncio
async def test_oauth_pkce_consent_is_minimal_and_scoped(env):
    from hub.api_integrations import _plugin
    from urllib.parse import urlparse
    plugin=_plugin('gmail','email.search')
    result=await plugin.get_authorization_url('school','http://localhost:8000/integrations/callback')
    params=parse_qs(urlparse(result['url']).query)
    assert set(params['scope'][0].split())=={G+'gmail.readonly','openid','email'}
    assert params['code_challenge_method']==['S256']
    assert 'code_verifier' not in params and result['code_verifier'] not in result['url']
    assert _plugin('gmail','email.send').scopes==(G+'gmail.send',)


@pytest.mark.asyncio
async def test_refresh_expiry_preserves_grants_and_rotation(env):
    await env.connect(expired=True)
    seen=[]
    def response(request):
        seen.append(parse_qs(request.content.decode()))
        return httpx.Response(200,json={'access_token':'new-safe-access','refresh_token':'new-safe-refresh','expires_in':3600})
    oauth=GoogleOAuth('gmail',transport=httpx.MockTransport(response))
    assert await oauth.get_valid_token('primary')=='new-safe-access'
    assert env.vault['gmail_primary']['scopes']==[G+'gmail.readonly']
    assert env.vault['gmail_primary']['refresh_token']=='new-safe-refresh'
    assert seen[0]['grant_type']==['refresh_token']


@pytest.mark.asyncio
async def test_refresh_revocation_is_persisted_without_token_or_body_leak(env,caplog):
    await env.connect(expired=True)
    oauth=GoogleOAuth('gmail',transport=httpx.MockTransport(lambda request:httpx.Response(400,json={'error':'invalid_grant','error_description':REFRESH})))
    with pytest.raises(IntegrationError) as error:await oauth.get_valid_token('primary')
    assert error.value.category=='AUTH_REQUIRED' and error.value.diagnostic=='REVOKED'
    assert env.vault['gmail_primary']['revoked'] and 'access_token' not in env.vault['gmail_primary']
    assert REFRESH not in caplog.text


@pytest.mark.asyncio
async def test_maps_disabled_or_missing_key_does_not_claim_authorization(env):
    state=env.registry.capability_status(env.registry.get('maps.geocode'))
    assert not state['available'] and state['auth_state']=='UNCONFIGURED'


@pytest.mark.asyncio
@pytest.mark.parametrize('capability,args,response',[
    ('email.search',{'query':'fixture'},{'messages':[{'id':'m1','threadId':'t1'}]}),
    ('email.read',{'message_id':'m1'},{'id':'m1','payload':{'headers':[{'name':'Subject','value':'Fixture'}]}}),
    ('email.send',{'to':'a@example.invalid','subject':'Fixture','body':'Synthetic'},{'id':'m1'}),
    ('calendar.list',{'time_min':'2026-10-20T00:00:00Z','time_max':'2026-10-21T00:00:00Z'},{'items':[{'id':'e1','summary':'Fixture'}]}),
    ('calendar.create',{'summary':'Fixture','start':'2026-10-20T10:00:00Z','end':'2026-10-20T11:00:00Z','time_zone':'UTC'},{'id':'e1'}),
    ('calendar.update',{'event_id':'e1','summary':'Fixture','start':'2026-10-20T10:00:00Z','end':'2026-10-20T11:00:00Z','time_zone':'UTC'},{'id':'e1'}),
    ('contacts.search',{'query':'Fixture'},{'results':[{'person':{'resourceName':'people/p1','names':[{'displayName':'Fixture','metadata':{'source':'strip'}}]}}]}),
    ('drive.search',{'query':"name contains 'fixture'"},{'files':[{'id':'f1','name':'Fixture','mimeType':'text/plain'}]}),
    ('drive.read',{'file_id':'f1'},{'id':'f1','name':'Fixture','mimeType':'text/plain','size':'4'}),
    ('drive.upload',{'name':'fixture.txt','content':'Synthetic'},{'id':'f1','name':'fixture.txt'}),
    ('maps.geocode',{'address':'Synthetic address'},{'results':[{'placeId':'p1','formattedAddress':'Synthetic','location':{'latitude':1.,'longitude':2.}}]}),
    ('maps.place_search',{'query':'Synthetic museum'},{'places':[{'id':'p1','displayName':{'text':'Museum'}}]}),
    ('routes.compute',{'origin':'Synthetic A','destination':'Synthetic B'},{'routes':[{'distanceMeters':123,'duration':'45s'}]}),
])
async def test_every_initial_capability_is_wired_to_its_structured_adapter(env,capability,args,response):
    cap=env.registry.get(capability);pid=cap.provider_id
    from hub.integrations.definitions import SERVICES
    service=SERVICES[pid][0]
    if cap.requirement.scopes:await env.connect(service=service,scopes=list(cap.requirement.scopes))
    else:
        env.monkeypatch.setenv('GOOGLE_MAPS_API_KEY','MAPS_SECRET_CANARY_718')
        env.monkeypatch.setenv('CORTEX_GOOGLE_MAPS_ENABLED','1')
    seen=[]
    adapter(env,pid,lambda request:(seen.append(request) or httpx.Response(200,json=response)))
    if cap.effect!=Effect.READ:await grant(env,capability,args)
    result=await env.registry.execute(capability,args,'asus')
    assert result['status']=='success',result
    assert result['capability']==capability and result['provider']==pid and result['data']['items']
    assert all('MAPS_SECRET_CANARY' not in str(r.url) for r in seen)
    assert 'MAPS_SECRET_CANARY' not in json.dumps(result)
    assert all(r.url.scheme=='https' for r in seen)
    if service=='contacts':assert len(seen)==2


@pytest.mark.asyncio
async def test_account_specific_capability_remains_discoverable_when_default_lacks_scope(env):
    await env.connect(account='school',scopes=[G+'gmail.readonly'])
    await env.connect(account='personal',scopes=[G+'gmail.send'],default=True)
    cap=CanonicalCapabilityRegistry.get_instance().get_capability('email.search')
    assert cap.availability and cap.provider_status['account_selection_required']
    adapter(env,'google_gmail',lambda request:httpx.Response(200,json={'messages':[]}))
    result=await env.registry.execute('email.search',{'query':'fixture','account_id':'school'},'asus')
    assert result['status']=='success' and result['provenance']['account']=='school'


@pytest.mark.asyncio
async def test_default_account_change_invalidates_prior_write_approval(env):
    await env.connect(account='school',scopes=[G+'gmail.send'],default=True)
    await env.connect(account='personal',scopes=[G+'gmail.send'])
    args={'to':'a@example.invalid','subject':'Fixture','body':'Synthetic'}
    await grant(env,'email.send',args)
    await env.registry.set_default('google_gmail','personal')
    result=await env.registry.execute('email.send',args,'asus')
    assert result['status']=='needs_approval'


@pytest.mark.asyncio
async def test_oauth_exchange_binds_identity_and_persists_granted_scopes_in_vault(env):
    scopes=G+'gmail.readonly openid email';seen=[]
    def response(request):
        seen.append(request)
        if request.url.host=='oauth2.googleapis.com':return httpx.Response(200,json={'access_token':ACCESS,'refresh_token':REFRESH,'expires_in':3600,'scope':scopes})
        return httpx.Response(200,json={'sub':'subject-school','email':'school@example.invalid','email_verified':True})
    oauth=GoogleOAuth('gmail',[G+'gmail.readonly'],httpx.MockTransport(response))
    assert await oauth.exchange_code('school','synthetic-code','http://localhost:8000/integrations/callback',{'code_verifier':'fixture-verifier'})
    from hub.database import get_integrations
    db_rows=await get_integrations()
    assert ACCESS not in json.dumps(db_rows) and REFRESH not in json.dumps(db_rows)
    assert env.vault['gmail_school']['identity']['sub']=='subject-school'
    assert G+'gmail.readonly' in env.vault['gmail_school']['scopes']
    assert seen[1].headers['authorization']=='Bearer '+ACCESS


@pytest.mark.asyncio
async def test_oauth_state_is_expiring_and_single_use(env):
    from hub import api_integrations as api
    from fastapi import HTTPException
    class Plugin:
        service='gmail';scopes=(G+'gmail.readonly',)
        async def get_authorization_url(self,*a):return {'url':'https://accounts.google.com/fixture','state':'single-use-state','code_verifier':'private-verifier'}
        async def exchange_code(self,*a):return True
    env.monkeypatch.setattr(api,'_auth_wrapper',AsyncMock(return_value={'device_id':'asus','role':'admin'}))
    env.monkeypatch.setattr(api,'_plugin',lambda *a:Plugin())
    info=await api.start_oauth('gmail','school','email.search','secret','asus',None)
    assert 'private-verifier' not in json.dumps(info)
    await api.oauth_callback('single-use-state','synthetic-code')
    with pytest.raises(HTTPException):await api.oauth_callback('single-use-state','synthetic-code')
    await api.start_oauth('gmail','school','email.search','secret','asus',None)
    api._OAUTH_SESSIONS['single-use-state']['expires_at']=0
    with pytest.raises(HTTPException):await api.oauth_callback('single-use-state','synthetic-code')


@pytest.mark.asyncio
async def test_privacy_restricted_reasoning_provider_never_receives_integration_data(env):
    await env.connect()
    adapter(env,'google_gmail',lambda request:httpx.Response(200,json={'messages':[{'id':'private-id'}]}))
    from test_agent_turn_runtime import ScriptedModel,call,control
    from hub.agent_turn import AgentTurn
    model=ScriptedModel([call('email.search',query='fixture')]);model.locality='cloud_worker';model.max_privacy_class='PUBLIC'
    result=await AgentTurn(control([]),model).run('Find a matching email','asus',{'response_channel':'gui'})
    assert result.status=='denied'
    assert 'private-id' not in json.dumps(model.calls)


@pytest.mark.asyncio
async def test_real_control_approval_binding_and_dispatcher_cannot_bypass_write_gate(env):
    await env.connect(scopes=[G+'gmail.send']);calls=[]
    adapter(env,'google_gmail',lambda request:(calls.append(request) or httpx.Response(200,json={'id':'sent1'})))
    from hub.control_layer import CortexControlLayer
    from hub.pipeline.execution_dispatcher import ExecutionDispatcher
    from hub.pipeline.semantic_interpreter import SemanticIntent
    args={'to':'a@example.invalid','subject':'Fixture','body':'Synthetic'}
    intent=SemanticIntent(type='ACTION',goal='Synthetic send',required_effect='',candidate_capabilities=['email.send'],arguments=args)
    ctrl=CortexControlLayer()
    result=await ExecutionDispatcher.dispatch(intent,'asus',ctrl,source_device='asus')
    assert result['status']=='needs_approval' and not calls
    cid=await ctrl.request_approval('asus','email.send',args,'asus',3)
    await resolve_confirmation(cid,'approved','asus')
    result=await ExecutionDispatcher.dispatch(intent,'asus',ctrl,source_device='asus')
    assert result['status']=='success' and len(calls)==1


@pytest.mark.asyncio
async def test_account_subject_change_invalidates_prior_write_approval(env):
    await env.connect(scopes=[G+'gmail.send'])
    args={'to':'a@example.invalid','subject':'Fixture','body':'Synthetic'}
    await grant(env,'email.send',args)
    env.vault['gmail_primary']['identity']['sub']='a-different-Google-account'
    result=await env.registry.execute('email.send',args,'asus')
    assert result['status']=='needs_approval'


@pytest.mark.asyncio
async def test_concurrent_calls_cannot_consume_one_write_approval_twice(env):
    await env.connect(scopes=[G+'gmail.send']);calls=[]
    async def response(request):
        calls.append(request);await asyncio.sleep(.01)
        return httpx.Response(200,json={'id':'sent1'})
    adapter(env,'google_gmail',response)
    args={'to':'a@example.invalid','subject':'Fixture','body':'Synthetic'}
    await grant(env,'email.send',args)
    results=await asyncio.gather(env.registry.execute('email.send',args,'asus'),env.registry.execute('email.send',args,'asus'))
    assert sorted(r['status'] for r in results)==['needs_approval','success']
    assert len(calls)==1


@pytest.mark.asyncio
async def test_html_only_email_does_not_claim_an_empty_plain_text_body(env):
    await env.connect()
    adapter(env,'google_gmail',lambda request:httpx.Response(200,json={'id':'m1','payload':{'mimeType':'text/html','body':{'data':'PGI+eDwvYj4='}}}))
    result=await env.registry.execute('email.read',{'message_id':'m1','include_body':True},'asus')
    item=result['data']['items'][0]
    assert item['body'] is None and item['body_available'] is False


@pytest.mark.asyncio
async def test_unexpected_adapter_failure_is_normalized_without_secret_exception_text(env,caplog):
    await env.connect()
    broken=SimpleNamespace(rate_limit={},invoke=AsyncMock(side_effect=RuntimeError(ACCESS+REFRESH)))
    env.registry.register('google_gmail',broken)
    result=await env.registry.execute('email.search',{'query':'fixture'},'asus')
    assert result['status']=='failure' and result['error']['category']=='PROVIDER_UNAVAILABLE'
    assert ACCESS not in json.dumps(result)+caplog.text


@pytest.mark.asyncio
async def test_lower_scope_after_refresh_is_rechecked_before_service_request(env):
    await env.connect(expired=True)
    requests=[]
    def response(request):
        requests.append(request)
        if request.url.host!='oauth2.googleapis.com':raise AssertionError('Scope-reduced token reached service')
        return httpx.Response(200,json={'access_token':'new-access','expires_in':3600,'scope':'openid email'})
    adapter(env,'google_gmail',response)
    result=await env.registry.execute('email.search',{'query':'fixture'},'asus')
    assert result['error']['category']=='SCOPE_MISSING' and len(requests)==1


@pytest.mark.asyncio
async def test_api_401_marks_account_unauthorized_without_claiming_revocation(env):
    await env.connect()
    adapter(env,'google_gmail',lambda request:httpx.Response(401,json={'error':ACCESS}))
    await env.registry.execute('email.search',{'query':'fixture'},'asus')
    rows=await env.registry.statuses()
    gmail=next(r for r in rows if r['key']=='gmail')
    assert not gmail['connected'] and gmail['accounts'][0]['auth_state']=='UNAUTHORIZED'


@pytest.mark.asyncio
async def test_integration_cannot_use_hub_shortcut_to_bypass_cloud_privacy(env):
    await env.connect();calls=[]
    adapter(env,'google_gmail',lambda request:(calls.append(request) or httpx.Response(200,json={'messages':[]})))
    env.monkeypatch.setenv('CORTEX_HOST_LOCALITY','cloud_worker')
    result=await env.registry.execute('email.search',{'query':'fixture'},'asus')
    assert result['error']['diagnostic']=='PRIVACY_POLICY' and not calls


@pytest.mark.asyncio
async def test_missing_token_expiry_cannot_invent_valid_authorization(env):
    await env.connect(expired=True)
    oauth=GoogleOAuth('gmail',transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'access_token':ACCESS})))
    with pytest.raises(IntegrationError) as error:await oauth.get_valid_token('primary')
    assert error.value.diagnostic=='INVALID_TOKEN_RESPONSE'


@pytest.mark.asyncio
async def test_agentturn_write_waits_for_real_approval_then_executes_once(env):
    await env.connect(scopes=[G+'gmail.send']);calls=[]
    adapter(env,'google_gmail',lambda request:(calls.append(request) or httpx.Response(200,json={'id':'sent1'})))
    from test_agent_turn_runtime import ScriptedModel,call,control
    from hub.agent_turn import AgentTurn
    from hub.control_layer import CortexControlLayer
    from hub.database import get_pending_confirmations
    from hub.main import PENDING_FUTURES,_execute_resolve_confirmation
    model=ScriptedModel([call('email.send',to='a@example.invalid',subject='Fixture',body='Synthetic')],answer='The provider accepted the test message.')
    ctrl=control([]);ctrl.request_approval=CortexControlLayer().request_approval
    agent=AgentTurn(ctrl,model);agent.ACK_SECONDS=.01
    ack=await agent.run('Send the specified synthetic message','asus',{'response_channel':'gui'})
    job=AgentTurn._running[ack.task_id]
    row=None
    for _ in range(100):
        rows=await get_pending_confirmations()
        row=next((r for r in rows if r['command']=='email.send' and r['command_id'] in PENDING_FUTURES),None)
        if row:break
        await asyncio.sleep(.005)
    assert row and not calls
    assert (await _execute_resolve_confirmation(row['command_id'],'approve','asus'))[0]
    result=await job
    await asyncio.gather(*list(AgentTurn._running.values()))
    assert result.status=='success' and len(calls)==1
    assert result.message=='The provider accepted the test message.'
    assert ACCESS not in json.dumps(model.calls)
