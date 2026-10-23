"""Bounded A14 acceptance on normal local Hub/ASUS/native runtime.

No injected plans or provider responses. Only public analysis and ui.wait are
requested. Credentials remain in Python. Use command.json in the artifact folder
for submit_cancel/stop; native cancellation is exercised in the actual app.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROMPT = ('Use separate independent workers to assess spaced retrieval practice and worked-example practice '
          'for introductory programming, then have a dependent worker compare their tradeoffs from both analyses. '
          'In parallel, perform a one-second wait on ASUS through Cortex and report only what its actual receipt proves. '
          'Coordinate these workers and give one concise, verified recommendation. No browsing, files, account access or writes.')
CANCEL_PROMPT = ('Coordinate two independent reasoning workers comparing iterative and recursive approaches to traversing '
                 'a small tree. Separately wait 40 seconds on ASUS, then combine the two analyses and the wait receipt. '
                 'Use only reasoning and the wait capability. No other computer or external account actions.')


def read_owned(root_id):
    with sqlite3.connect('file:' + str(ROOT/'data'/'cortex.db') + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute('SELECT * FROM tasks WHERE assigned_device=? ORDER BY created_at DESC LIMIT 100', ('asus',))]
    for row in rows:
        try: row['context'] = json.loads(row.get('context') or '{}')
        except ValueError: row['context'] = {}
    ids = {root_id}
    for _ in range(5):
        ids |= {r['id'] for r in rows if r['context'].get('parent_request_id') in ids}
    return [r for r in rows if r['id'] in ids]


def evidence(rows):
    out = []
    for r in rows:
        ctx = r['context']; g = ctx.get('orchestration') or {}
        item = {k:r[k] for k in ('id','type','status','created_at','updated_at')}
        item['parent_id'] = ctx.get('parent_request_id')
        item['result_status'] = (ctx.get('result') or {}).get('status')
        item['error'] = ((ctx.get('result') or {}).get('data') or {}).get('error_code')
        if g:
            item.update(phase=g.get('phase'), verification=g.get('verification'), events=g.get('events'),
                planner_route=g.get('planner_route'), synthesis_route=g.get('synthesis_route'),
                nodes={name:{k:n.get(k) for k in ('role','kind','dependencies','required_capabilities','state','started_at','finished_at','error')}
                    | {'provenance':(n.get('result') or {}).get('provenance'),
                       'receipts':(n.get('result') or {}).get('receipts')} for name,n in g.get('nodes',{}).items()})
        out.append(item)
    return out


async def security_probe(out):
    """Real authenticated HTTP rejection probes; no inference or new identities."""
    secret=(Path.home()/'.cortex'/'asus_secret').read_text().strip()
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8000', timeout=8, trust_env=False,
            headers={'X-Cortex-Device':'asus','X-Cortex-Secret':secret}) as client:
        forged=await client.post('/api/control/message',json={'device_id':'a14-forged-source',
            'text':'Delegate an unauthorized action.'})
        executable=await client.post('/api/workspace/action',json={'kind':'delegate','task_id':'forged',
            'action_id':'0'*32,'role':'admin','command':'shell.execute'})
        foreign=await client.post('/api/workspace/action',json={'kind':'cancel_task','task_id':'a14-unowned-task',
            'action_id':'0'*32})
        unauth=await client.post('/api/workspace/sync',json={'revision':0},headers={'X-Cortex-Secret':'invalid-fixture'})
        result={'forged_request_source':forged.status_code,'executable_workspace_content':executable.status_code,
            'unowned_task_control':foreign.status_code,'invalid_credential':unauth.status_code,
            'provider_calls_required':False,'permission_changes':False}
        (out/'live-security.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result))


async def main(out):
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **{k:v for k,v in dotenv_values(ROOT/'.env').items() if v is not None})
    secret = (Path.home()/'.cortex'/'asus_secret').read_text().strip()
    env.update(CORTEX_SECRET=secret, USE_MOCK_VOICE='1', CORTEX_HUB_DEVICE_ID='asus')
    env.pop('CORTEX_DB_PATH',None); env.pop('CORTEX_STATE_DIR',None)
    children, streams, requests = {}, [], []
    def save(name, value):
        (out/name).write_text(json.dumps(value,indent=2),encoding='utf-8')
    def spawn(kind):
        args = {'hub':['-m','uvicorn','hub.main:app','--host','127.0.0.1','--port','8000'],
            'agent':['-m','agent.agent','--hub','ws://127.0.0.1:8000','--device','asus','--no-ui'],
            'desktop':['-m','agent.ui.main','--hub','http://127.0.0.1:8000','--device','asus','--no-auto-start']}[kind]
        stream=(out/(kind+'.log')).open('a',encoding='utf-8');streams.append(stream)
        children[kind]=subprocess.Popen([sys.executable,'-X','utf8',*args],cwd=ROOT,env=env,
            stdout=stream,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8000',
                headers={'X-Cortex-Device':'asus','X-Cortex-Secret':secret},timeout=8,trust_env=False) as client:
            try:
                response=await client.get('/health')
                if response.status_code==200:
                    raise RuntimeError('Existing Hub detected; do not launch duplicate runtime')
            except httpx.ConnectError: pass
            spawn('hub')
            for _ in range(120):
                try:
                    if (await client.get('/health')).status_code==200:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.25)
            else:raise RuntimeError('Hub startup deadline')
            spawn('agent')
            for _ in range(120):
                response=await client.get('/api/discovery');response.raise_for_status()
                if any(r['provider_id']=='asus' and r['capability_id']=='ui.wait' and r['executable'] for r in response.json()['candidates']):break
                await asyncio.sleep(.25)
            else:raise RuntimeError('Fresh ASUS capability advertisement missing')
            spawn('desktop')
            save('runtime.json',{'pids':{k:p.pid for k,p in children.items()},'normal_database':True,
                'voice_mocked':True,'provider_responses_injected':False,'plans_injected':False})
            async def submit(text, label):
                key='a14-live-'+uuid.uuid4().hex
                response=await client.post('/api/control/message',json={'text':text,'device_id':'asus','request_id':key})
                response.raise_for_status();value=response.json()
                requests.append({'label':label,'task_id':value['task_id'],'initial_status':value['status'],
                    'submitted_at':time.time(),'request_id':key})
                save('requests.json',requests)
            await submit(PROMPT,'primary')
            deadline=time.monotonic()+900
            while time.monotonic()<deadline:
                for request in requests:
                    rows=read_owned(request['task_id'])
                    save(request['label']+'-evidence.json',evidence(rows))
                    # Only this public acceptance request's own final response is recorded.
                    root=next((r for r in rows if r['id']==request['task_id']),None)
                    if root and root['context'].get('result'):
                        result=root['context']['result']
                        save(request['label']+'-result.json',{'status':result.get('status'),
                            'message':result.get('message'),'data':result.get('data')})
                command=out/'command.json'
                if command.exists():
                    value=json.loads(command.read_text());command.unlink()
                    if value.get('action')=='stop':break
                    if value.get('action')=='submit_cancel':await submit(CANCEL_PROMPT,'cancellation')
                await asyncio.sleep(1)
    finally:
        for kind in ('desktop','agent','hub'):
            p=children.get(kind)
            if p and p.poll() is None:
                p.terminate()
                try:await asyncio.to_thread(p.wait,8)
                except subprocess.TimeoutExpired:p.kill();await asyncio.to_thread(p.wait,5)
        for stream in streams:stream.close()
        save('cleanup.json',{'all_owned_processes_stopped':all(p.poll() is not None for p in children.values())})


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--security-only',action='store_true');args=parser.parse_args()
    asyncio.run(security_probe(args.out) if args.security_only else main(args.out))
