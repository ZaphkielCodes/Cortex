"""Integration lifecycle, authenticated browser transport and truthful failures."""
import asyncio
import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs
import httpx
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock
from test_a11_integrations import env, adapter, ACCESS, REFRESH
from hub.integrations.google_auth import GoogleOAuth, IntegrationError
from hub.integrations.definitions import G


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['timeout', 'invalid_json', 'invalid_identity'])
async def test_identity_failure_is_normalized_without_storing_partial_tokens(env, kind):
    def response(request):
        if request.url.host == 'oauth2.googleapis.com':
            return httpx.Response(200, json={'access_token': ACCESS, 'expires_in': 3600, 'scope': G+'gmail.readonly'})
        if kind == 'timeout': raise httpx.ReadTimeout(ACCESS, request=request)
        if kind == 'invalid_json': return httpx.Response(200, text=REFRESH)
        return httpx.Response(200, json=[])
    oauth = GoogleOAuth('gmail', [G+'gmail.readonly'], httpx.MockTransport(response))
    with pytest.raises(IntegrationError) as error:
        await oauth.exchange_code('school', 'code', 'http://localhost/callback', {'code_verifier': 'verifier'})
    assert error.value.category in ('NETWORK_ERROR', 'AUTH_REQUIRED')
    assert not env.vault and ACCESS not in str(error.value) and REFRESH not in str(error.value)


@pytest.mark.asyncio
async def test_cancelled_consent_consumes_state_without_exchange(env):
    from hub import api_integrations as api
    plugin = GoogleOAuth('gmail', [G+'gmail.readonly'])
    plugin.exchange_code = AsyncMock()
    env.monkeypatch.setattr(api, '_auth_wrapper', AsyncMock(return_value={'role': 'admin', 'device_id': 'asus'}))
    env.monkeypatch.setattr(api, '_plugin', lambda *a: plugin)
    info = await api.start_oauth('gmail', 'school', 'email.search', 'fixture', 'asus', None)
    state = parse_qs(httpx.URL(info['url']).query.decode())['state'][0]
    with pytest.raises(HTTPException) as error: await api.oauth_callback(state, None, 'access_denied')
    assert error.value.status_code == 400 and state not in api._OAUTH_SESSIONS
    plugin.exchange_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_accounts_401_does_not_hide_another_accounts_capability(env):
    await env.connect(account='school')
    await env.connect(account='personal')
    adapter(env, 'google_gmail', lambda request: httpx.Response(401))
    denied = await env.registry.execute('email.search', {'query': 'fixture', 'account_id': 'school'}, 'asus')
    assert denied['error']['category'] == 'AUTH_REQUIRED'
    assert env.vault['gmail_school']['authorization_required'] is True
    assert not env.vault['gmail_personal'].get('authorization_required')
    status = env.registry.capability_status(env.registry.get('email.search'))
    assert status['available']
    adapter(env, 'google_gmail', lambda request: httpx.Response(200, json={'messages': [{'id': 'other'}]}))
    result = await env.registry.execute('email.search', {'query': 'fixture', 'account_id': 'personal'}, 'asus')
    assert result['status'] == 'success' and result['provenance']['account'] == 'personal'


@pytest.mark.asyncio
async def test_authorization_required_token_cannot_refresh_behind_consent_gate(env):
    await env.connect()
    env.vault['gmail_primary']['authorization_required'] = True
    calls = []
    oauth = GoogleOAuth('gmail', transport=httpx.MockTransport(lambda r: calls.append(r)))
    with pytest.raises(IntegrationError): await oauth.get_valid_token('primary')
    assert calls == []


@pytest.mark.asyncio
async def test_missing_message_receipt_does_not_invent_success(env):
    await env.connect()
    adapter(env, 'google_gmail', lambda request: httpx.Response(200, json={}))
    result = await env.registry.execute('email.read', {'message_id': 'missing'}, 'asus')
    assert result['status'] == 'failure' and result['error']['diagnostic'] == 'INVALID_RESPONSE'


@pytest.mark.asyncio
async def test_service_timeout_is_retry_safe_without_invented_items(env):
    await env.connect()
    def timeout(request): raise httpx.ReadTimeout(ACCESS, request=request)
    adapter(env, 'google_gmail', timeout)
    result = await env.registry.execute('email.search', {'query': 'fixture'}, 'asus')
    assert result['error']['category'] == 'NETWORK_ERROR' and result['retry_safe']
    assert not result['data'] and ACCESS not in json.dumps(result)


@pytest.mark.asyncio
async def test_refresh_is_serialized_per_account(env):
    await env.connect(expired=True)
    calls = []
    async def respond(request):
        calls.append(request)
        await asyncio.sleep(.01)
        return httpx.Response(200, json={'access_token': 'rotated', 'expires_in': 3600})
    oauth = GoogleOAuth('gmail', transport=httpx.MockTransport(respond))
    assert await asyncio.gather(*(oauth.get_valid_token('primary') for _ in range(3))) == ['rotated']*3
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_integration_http_contract_requires_auth_and_matches_browser(env, monkeypatch):
    from hub.main import app
    from hub import api_integrations as api
    await env.connect(account='school')
    async def reject(*args): raise HTTPException(401, 'Not authenticated')
    monkeypatch.setattr(api, '_auth_wrapper', reject)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        assert (await client.get('/integrations/')).status_code == 401
        monkeypatch.setattr(api, '_auth_wrapper', AsyncMock(return_value={'role': 'admin', 'device_id': 'asus'}))
        rows = (await client.get('/integrations/')).json()['integrations']
    gmail = next(row for row in rows if row['key'] == 'gmail')
    assert gmail['name'] == 'Google Gmail' and gmail['accounts'][0]['account_id'] == 'school'
    assert gmail['accounts'][0]['identity']['email'] == 'school@example.invalid'
    assert ACCESS not in json.dumps(rows) and REFRESH not in json.dumps(rows)


def test_browser_integration_rendering_and_session_reconnection():
    result = subprocess.run(['node', '--test', 'tests/dashboard_integrations.test.cjs'], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_model_catalog_exposes_implicit_account_without_private_identity(env):
    await env.connect()
    cap = next(c for c in env.registry.canonical_capabilities() if c.name == 'email.search')
    assert 'account=primary' in cap.description[:32]
    assert cap.arguments['account_id']['default'] == 'primary'
    selection = cap.arguments['account_id']['x-account-selection']
    assert selection == {'implicit_account': 'primary', 'eligible_accounts': ['primary'], 'explicit_required': False}
    assert '@example.invalid' not in json.dumps(cap.arguments)
    await env.connect(account='school')
    cap = next(c for c in env.registry.canonical_capabilities() if c.name == 'email.search')
    assert cap.arguments['account_id']['x-account-selection']['explicit_required']


@pytest.mark.asyncio
async def test_browser_login_cookie_authenticates_integrations_after_refresh(env):
    from hub.main import app
    from hub.database import enroll_device
    from hub.auth import hash_secret
    await env.connect()
    await enroll_device('asus', 'Fixture', 'admin', hash_secret('browser-fixture-secret'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        login = await client.post('/login', json={'device_id': 'asus', 'secret': 'browser-fixture-secret'})
        assert login.status_code == 200 and client.cookies.get('cortex_session')
        assert (await client.get('/integrations/')).status_code == 200
        await client.post('/logout')
        assert (await client.get('/integrations/')).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['invalid_arguments', 'provider_error'])
async def test_agentturn_reports_integration_failures_without_executor_type_error(env, kind):
    from test_agent_turn_runtime import ScriptedModel, call, control
    from hub.agent_turn import AgentTurn
    if kind == 'invalid_arguments':
        await env.connect(service='calendar', scopes=[G+'calendar.events.readonly'])
        proposal=call('calendar.list', time_min='2026-10-21', time_max='2026-10-28')
        expected='INVALID_ARGUMENT'
    else:
        await env.connect()
        adapter(env, 'google_gmail', lambda request: httpx.Response(503))
        proposal=call('email.search', query='fixture')
        expected='PROVIDER_UNAVAILABLE'
    agent=AgentTurn(control([]), ScriptedModel([proposal]))
    result=await agent.run('Run the requested read', 'asus', {'response_channel':'gui'})
    if result.is_async:
        result=await AgentTurn._running[result.task_id]
        await asyncio.gather(*list(AgentTurn._running.values()))
    assert result.status == 'error' and expected in result.message
    assert result.data['integration_error']['category'] == expected
    assert result.data.get('error_code') != 'agent_runtime_error'
    if kind == 'invalid_arguments':
        issues=result.data['integration_error']['validation_issues']
        assert issues and '2026-10-21' not in json.dumps(issues)


def test_calendar_time_schema_declares_the_existing_timezone_requirement():
    from hub.integrations.definitions import definitions
    for cap in definitions():
        if cap.capability_id.startswith('calendar.'):
            fields=cap.argument_schema()['properties']
            for key in ('time_min','time_max') if cap.capability_id == 'calendar.list' else ('start','end'):
                assert fields[key]['format'] == 'date-time'
                assert 'timezone' in fields[key]['description']
