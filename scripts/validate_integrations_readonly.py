"""Opt-in Google read validation on a disposable Hub + native ASUS agent.

Uses existing secure credentials, never authorizes new scopes, and never writes
service data. Reports contain counts/status only. No pending approval is accepted
automatically. The production database and running processes are left untouched.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
READS = {'email.search', 'email.read', 'calendar.list'}


def serve(port):
    """Diagnostic boundaries only; selection and execution inside remain real."""
    import uvicorn
    from fastapi import Request, HTTPException
    from hub.main import app, _authenticate_http_headers
    from hub.pipeline.execution_dispatcher import ExecutionDispatcher
    import hub.agent_turn as turns
    observations = []
    original = ExecutionDispatcher.dispatch
    original_decision=turns._get_model_decision
    async def decision(*args, **kwargs):
        chosen=await original_decision(*args, **kwargs)
        observations.append({'decision': chosen.decision, 'capability': chosen.capability,
                             'argument_names': sorted(chosen.arguments)})
        return chosen
    turns._get_model_decision=decision

    async def guarded(cls, intent, target_device, control_layer, request_id='unknown', source_device=None, privacy_context=None):
        caps = intent.candidate_capabilities
        if len(caps) != 1 or caps[0] not in READS:
            observations.append({'capability': caps[0] if caps else 'unknown', 'status': 'blocked_by_read_only_harness'})
            return {'status': 'denied', 'message': 'This validation session permits integration reads only.'}
        result = await original(intent, target_device, control_layer, request_id, source_device, privacy_context)
        items = result.get('data', {}).get('items', [])
        observations.append({'capability': caps[0], 'status': result.get('status'),
            'item_count': len(items), 'field_names': sorted({key for item in items for key in item}),
            'provider': result.get('provider'), 'provenance_present': bool(result.get('provenance')),
            'error': result.get('error', {}), 'request_id': request_id})
        return result
    ExecutionDispatcher.dispatch = classmethod(guarded)

    async def caller(request):
        return await _authenticate_http_headers(request.headers.get('X-Cortex-Device'),
            request.headers.get('X-Cortex-Secret'), request.cookies.get('cortex_session'))

    @app.post('/__validation/read/{capability}')
    async def read(capability: str, request: Request):
        identity = await caller(request)
        if capability not in READS: raise HTTPException(403, 'Read-only probe')
        from hub import main
        result = await ExecutionDispatcher.dispatch(SimpleNamespace(candidate_capabilities=[capability], arguments=await request.json()),
            identity['device_id'], main._control_layer, 'structured-read-probe', identity['device_id'])
        # Metadata only: service contents never enter this diagnostic response.
        receipt = dict(observations[-1])
        if capability == 'email.search' and result.get('status') == 'success':
            items = result.get('data', {}).get('items', [])
            if items and items[0].get('id'):
                await ExecutionDispatcher.dispatch(SimpleNamespace(candidate_capabilities=['email.read'],
                    arguments={'message_id': items[0]['id'], 'include_body': False}),
                    identity['device_id'], main._control_layer, 'structured-read-probe', identity['device_id'])
                receipt['metadata_read'] = observations[-1]
        return receipt

    @app.get('/__validation/status')
    async def status(request: Request):
        await caller(request)
        from hub.database import get_tasks, get_pending_confirmations
        from hub.agent_turn import AgentTurn
        tasks = []
        for row in await get_tasks():
            try: context = json.loads(row.get('context') or '{}')
            except (ValueError, TypeError): context = {}
            result = context.get('result') or {}
            routing = [{key: event[key] for key in ('event', 'selected_provider', 'selected_model',
                'failover_trigger', 'http_status', 'latency_seconds') if key in event}
                for event in context.get('routing_events', [])[-12:]]
            tasks.append({'id': row['id'], 'status': row['status'], 'result_status': result.get('status'),
                          'response_received': bool(result.get('message')), 'routing': routing})
        pending = [{'command_id': row['command_id'], 'command': row['command']} for row in await get_pending_confirmations()]
        return {'observations': observations, 'tasks': tasks, 'pending_approvals': pending}

    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning', access_log=False)


async def validate(output, deadline):
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    import httpx
    from hub.database import get_db_path
    production = get_db_path().resolve()
    # Copy only enrollment-independent account metadata. Tokens stay in keyring.
    with sqlite3.connect('file:' + production.as_posix() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM integrations WHERE plugin_name IN ('gmail','calendar')")]
    output.mkdir(parents=True, exist_ok=False)
    database = output / 'validation.db'
    os.environ['CORTEX_DB_PATH'] = str(database)
    from hub.database import init_db, close_db, add_integration
    await init_db()
    for row in rows:
        # Avoid the unrelated legacy Calendar startup prefetch in this disposable
        # DB. Canonical authorization derives from keyring scopes, not this flag.
        if row['status'] != 'revoked': row['status'] = 'validation_only'
        row['config'] = json.loads(row.get('config') or '{}')
        await add_integration(row)
    await close_db()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    child_env = dict(os.environ, CORTEX_DB_PATH=str(database), CORTEX_STATE_DIR=str(output / 'state'),
        CORTEX_HUB_DEVICE_ID='asus', CORTEX_ENABLE_HUB_DESKTOP='0', USE_MOCK_VOICE='1')
    children = []
    report = {'mode': 'DISPOSABLE_HUB_NATIVE_ASUS_EXISTING_GOOGLE_AUTH', 'service_writes_permitted': False,
        'production_db_used_for_execution': False, 'diagnostic_reads': [], 'agentturn': [], 'auto_approved': 0}
    def spawn(args):
        process = subprocess.Popen([sys.executable, '-X', 'utf8', *args], cwd=ROOT, env=child_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        children.append(process); return process
    try:
        hub = spawn([str(Path(__file__).resolve()), '--serve', '--port', str(port)])
        async with httpx.AsyncClient(timeout=35, trust_env=False, follow_redirects=True) as client:
            for _ in range(120):
                try:
                    response = await client.get(url + '/health')
                    if response.status_code == 200: break
                except httpx.HTTPError: pass
                if hub.poll() is not None: raise RuntimeError('Hub startup failed')
                await asyncio.sleep(.25)
            else: raise RuntimeError('Hub startup deadline')
            response = await client.post(url + '/bootstrap', json={'device_id': 'asus', 'device_name': 'ASUS read-only validation'})
            response.raise_for_status()
            secret = response.json()['secret']
            child_env['CORTEX_SECRET'] = secret
            client.headers.update({'X-Cortex-Device': 'asus', 'X-Cortex-Secret': secret})
            agent = spawn(['-m', 'agent.agent', '--hub', url.replace('http:', 'ws:'), '--device', 'asus', '--no-ui'])
            for _ in range(100):
                state = (await client.get(url + '/health')).json()
                if 'asus' in state.get('connected_devices', []): break
                if agent.poll() is not None: raise RuntimeError('Native agent startup failed')
                await asyncio.sleep(.25)
            else: raise RuntimeError('Native agent connection deadline')
            report['native_asus_authenticated'] = True
            # Verify the same cookie path used by the browser, without outputting
            # its session cookie or service account identifiers.
            logged_in = await client.post(url + '/login', json={'device_id': 'asus', 'secret': secret})
            logged_in.raise_for_status()
            import websockets
            cookie = '; '.join(f'{key}={value}' for key, value in client.cookies.items())
            async with websockets.connect(url.replace('http:', 'ws:') + '/ws-control', additional_headers={'Cookie': cookie}) as ws:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
                report['cookie_control_initial_state'] = frame.get('event') == 'initial_state'
                await asyncio.sleep(2)
                report['cookie_control_stays_connected'] = True
            accounts = (await client.get(url + '/integrations/')).json()['integrations']
            report['service_auth'] = [{ 'service': row['key'], 'auth_state': row['auth_state'],
                'account_count': len(row['accounts']), 'read_available': any(c['available'] for c in row['capabilities'] if c['id'] in READS)}
                for row in accounts if row['key'] in ('gmail', 'calendar')]
            now = datetime.now(timezone.utc)
            specs = [('email.search', {'query': 'newer_than:14d', 'max_results': 1}),
                     ('calendar.list', {'time_min': now.isoformat(), 'time_max': (now + timedelta(days=7)).isoformat(), 'max_results': 3})]
            # Select only an unambiguous/default account with actual read grants.
            # The registry enforces all scope and policy gates again on dispatch.
            for cap, args in specs:
                started = time.monotonic()
                receipt = (await client.post(url + '/__validation/read/' + cap, json=args)).json()
                receipt['seconds'] = round(time.monotonic() - started, 3)
                report['diagnostic_reads'].append(receipt)
            # Real free-form text input and real model, with no scripted choices.
            # Never approve data sharing or side effects on the user's behalf.
            prompts = ["Check the newest email from the last two weeks. Read its headers only and tell me whether a message was found. Do not reveal the subject or sender.",
                       "Check my calendar for the next seven days and tell me how many upcoming events were returned, without event titles or details."]
            for prompt in prompts:
                response = await client.post(url + '/api/control/message', json={'device_id': 'asus', 'text': prompt})
                response.raise_for_status()
                body = response.json(); tid = body.get('task_id')
                case = {'task_id': tid, 'initial_status': body.get('status'), 'ack_received': bool(body.get('response_text')),
                        'final_response_received': body.get('status') == 'success'}
                until = time.monotonic() + deadline
                while time.monotonic() < until:
                    snapshot = (await client.get(url + '/__validation/status')).json()
                    if snapshot['pending_approvals']:
                        case['blocked'] = 'HUMAN_PRIVACY_APPROVAL_REQUIRED'
                        case['pending_commands'] = [r['command'] for r in snapshot['pending_approvals']]
                        break
                    task = next((r for r in snapshot['tasks'] if r['id'] == tid), None)
                    if task and task['status'].upper() in ('COMPLETED', 'FAILED', 'DENIED', 'CANCELLED'):
                        case.update(final_status=task['status'], final_result_status=task['result_status'],
                                    final_response_received=task['response_received']); break
                    if body.get('status') not in ('in_progress', 'background'): break
                    await asyncio.sleep(.5)
                else: case['blocked'] = 'VALIDATION_DEADLINE'
                report['agentturn'].append(case)
                if case.get('blocked'): break
            report['final_diagnostics'] = (await client.get(url + '/__validation/status')).json()
    except Exception as error:
        # Exception messages/provider bodies may contain secrets; never report them.
        report['blocker'] = type(error).__name__
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try: await asyncio.to_thread(child.wait, 8)
                except subprocess.TimeoutExpired: child.kill(); await asyncio.to_thread(child.wait, 5)
        (output / 'live-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--deadline', type=float, default=45)
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve: serve(args.port)
    elif args.output: asyncio.run(validate(args.output.resolve(), args.deadline))
    else: parser.error('--output must name a new validation directory')
