"""A12: bounded, device-scoped projections of durable execution facts.

This module presents existing task state. It neither plans nor executes model
instructions. Action descriptors are affordances, never authority.
"""
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

ID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")]
Text = Annotated[str, Field(max_length=2000)]
Status = Literal['PENDING','RUNNING','WAITING','COMPLETED','FAILED','CANCELLED','PAUSED','DENIED','UNKNOWN']
TERMINAL = {'COMPLETED','FAILED','CANCELLED','DENIED'}


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Provenance(Model):
    source: Literal['task_db','capability_receipt','execution_trace','capability_discovery']
    task_id: ID
    updated_at: Text = ''


class Action(Model):
    action_id: ID
    kind: Literal['cancel_task']
    task_id: ID
    label: Literal['Cancel task'] = 'Cancel task'


class Component(Model):
    id: ID
    title: Annotated[str, Field(max_length=240)]
    provenance: Provenance


class Card(Component):
    kind: Literal['card'] = 'card'
    text: Text


class Table(Component):
    kind: Literal['table'] = 'table'
    columns: Annotated[list[Annotated[str, Field(max_length=80)]], Field(max_length=6)]
    rows: Annotated[list[Annotated[list[Text], Field(max_length=6)]], Field(max_length=20)]


class Progress(Component):
    kind: Literal['progress'] = 'progress'
    status: Status
    value: Annotated[float, Field(ge=0, le=1)] | None = None
    label: Text


class TimelineEntry(Model):
    at: Text = ''
    stage: Text
    detail: Text = ''


class Timeline(Component):
    kind: Literal['timeline'] = 'timeline'
    entries: Annotated[list[TimelineEntry], Field(max_length=32)]


class TaskDetails(Component):
    kind: Literal['task'] = 'task'
    status: Status
    description: Text = ''
    result: Text = ''
    actions: Annotated[list[Action], Field(max_length=1)] = Field(default_factory=list)


class Notice(Component):
    kind: Literal['status','error']
    text: Text


Item = Annotated[Card | Table | Progress | Timeline | TaskDetails | Notice, Field(discriminator='kind')]


class Envelope(Model):
    protocol_version: Literal[1] = 1
    epoch: ID
    revision: Annotated[int, Field(ge=0)]
    mode: Literal['snapshot','delta']
    base_revision: Annotated[int, Field(ge=0)] | None = None
    generated_at: str
    items: Annotated[list[Item], Field(max_length=128)]
    removed: Annotated[list[ID], Field(max_length=128)] = Field(default_factory=list)


class Cursor(Model):
    epoch: ID | None = None
    revision: Annotated[int, Field(ge=0)] = 0


class ActionRequest(Model):
    kind: Literal['cancel_task']
    task_id: ID
    action_id: ID


def clean(value, limit=2000):
    from .tracing import scrub_secrets
    text = value if isinstance(value, str) else ''
    text = scrub_secrets(text)
    text = re.sub(r'ya29\.[A-Za-z0-9._-]+', '[redacted]', text)
    text = re.sub(r'(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password)\s*[:=]\s*\S+', r'\1=[redacted]', text)
    for key, secret in os.environ.items():
        if len(secret) >= 6 and (key.endswith('API_KEY') or 'SECRET' in key or key.endswith('_TOKEN')):
            text = text.replace(secret, '[redacted]')
    return text[:limit]


def context(row):
    try:
        value = row.get('context') or {}
        value = json.loads(value) if isinstance(value, str) else value
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def state(row):
    raw = str(row.get('status') or '').upper()
    return raw if raw in {'PENDING','RUNNING','WAITING','COMPLETED','FAILED','CANCELLED','PAUSED','DENIED'} else 'UNKNOWN'


def action_id(source, row):
    raw = json.dumps([source, row['id'], row.get('status'), row.get('updated_at'), 'cancel_task'])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def owned_tasks(source):
    from .database import get_db
    # Admin authentication does not imply access to another device's workspace.
    import aiosqlite
    async with get_db() as db:
        db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM tasks WHERE assigned_device = ? ORDER BY created_at DESC LIMIT 100',(source,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


def project(source, rows):
    result = []
    known = {row['id']: row for row in rows}
    roots = [row for row in rows if context(row).get('parent_request_id') not in known]
    roots.sort(key=lambda row: row.get('updated_at') or row.get('created_at') or '', reverse=True)
    for row in roots[:20]:
        tid = row['id']
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', tid): continue
        children = [r for r in rows if context(r).get('parent_request_id') == tid]
        child = max(children, key=lambda r: r.get('updated_at') or '', default=row)
        saved = context(child)
        receipt = context(row).get('result') or saved.get('result') or {}
        receipt = receipt if isinstance(receipt, dict) else {}
        status = state(row)
        provenance = Provenance(source='task_db', task_id=tid, updated_at=clean(row.get('updated_at') or ''))
        actions = [] if status in TERMINAL else [Action(action_id=action_id(source,row),kind='cancel_task',task_id=tid)]
        result.append(TaskDetails(id='task:'+tid,title=clean(row.get('title') or 'Task',240),provenance=provenance,
            status=status,description=clean(row.get('last_error') or child.get('last_error') or ''),
            result=clean(receipt.get('message') or ''),actions=actions))
        if receipt.get('message'):
            result.append(Card(id='answer:'+tid,title='Recorded response',provenance=provenance,
                text=clean(receipt['message'])))
        elif row.get('last_error') or child.get('last_error'):
            result.append(Notice(kind='status',id='status:'+tid,title='Current activity',provenance=provenance,
                text=clean(row.get('last_error') or child.get('last_error'))))
        result.append(Progress(id='progress:'+tid,title='Execution state',provenance=provenance,status=status,
            value=1.0 if status=='COMPLETED' else None,
            label='Completion recorded by Cortex' if status=='COMPLETED' else 'Progress total not reported' if status not in TERMINAL else 'Task ended without successful completion'))
        traces = saved.get('compound_ui',{}).get('operational_trace',[]) if isinstance(saved.get('compound_ui'),dict) else []
        if not isinstance(traces,list):traces=[]
        entries = [TimelineEntry(at=clean(e.get('at') or ''),stage=clean(e.get('next_state') or e.get('stage') or 'UNKNOWN'),
            detail=clean(e.get('capability') or e.get('reason') or e.get('result') or '')) for e in traces[-32:] if isinstance(e,dict)]
        if not entries:
            entries=[TimelineEntry(at=clean(row.get('updated_at') or row.get('created_at') or ''),stage=status)]
        result.append(Timeline(id='timeline:'+tid,title='Recorded lifecycle',provenance=provenance,entries=entries))
        steps=saved.get('completed_steps',[])
        rows_out=[]
        for step in steps[-20:] if isinstance(steps,list) else []:
            if not isinstance(step,dict):continue
            outcome=step.get('result') if isinstance(step.get('result'),dict) else {}
            rows_out.append([clean(step.get('capability')),clean(outcome.get('status')),clean(step.get('executor_device'))])
        result.append(Table(id='receipts:'+tid,title='Capability receipts',
            provenance=Provenance(source='capability_receipt',task_id=tid,updated_at=provenance.updated_at),
            columns=['Capability','Outcome','Executor'],rows=rows_out))
        # A14 uses existing declarative components. Walk bounded owned ancestry:
        # request -> AgentTurn -> graph -> delegated AgentTurn. No cross-device join.
        family = {tid}
        for _ in range(4):
            family |= {r['id'] for r in rows if context(r).get('parent_request_id') in family}
        graphs = [r for r in rows if r['id'] in family and r.get('type') == 'orchestration']
        for graph_row in graphs[:1]:
            graph = context(graph_row).get('orchestration') or {}
            graph_nodes = graph.get('nodes') or {}
            node_rows = []
            for name, node in list(graph_nodes.items())[:8]:
                output = (node.get('result') or {}).get('output') or {}
                evidence = (node.get('result') or {}).get('receipts') or []
                worker_id = graph_row['id']+':'+name
                worker = next((r for r in rows if context(r).get('parent_request_id') == worker_id and state(r)=='WAITING'), {})
                if not worker:
                    worker = next((r for r in rows if r['id']==worker_id), {})
                node_state = 'WAITING' if state(worker) == 'WAITING' else node.get('state') or 'UNKNOWN'
                node_rows.append([clean(name), clean(node.get('role')), clean(', '.join(node.get('dependencies') or []) or 'Independent'),
                    clean(node_state), clean(str(output.get('value', '')) or node.get('error') or worker.get('last_error') or ''),
                    clean(', '.join(str(e.get('capability'))+': '+str(e.get('status')) for e in evidence) or
                          ('Model analysis' if node.get('kind') == 'reasoning' else 'No verified receipt'))])
            result.append(Table(id='agents:'+tid,title='Multi-agent subtasks',provenance=provenance,
                columns=['Subtask','Role','Dependencies','State','Result / issue','Evidence'],rows=node_rows))
            complete = sum(n.get('state') == 'COMPLETED' for n in graph_nodes.values())
            result.append(Progress(id='agents-progress:'+tid,title='Multi-agent progress',provenance=provenance,
                status=state(graph_row),value=float(complete/len(graph_nodes)) if graph_nodes else None,
                label=clean(f"{complete}/{len(graph_nodes)} subtasks completed. {graph.get('phase','UNKNOWN')}. Verification: " +
                    ('passed' if graph.get('verification',{}).get('verified') is True else 'not established'))))
            result.append(Timeline(id='agents-timeline:'+tid,title='Worker lifecycle',provenance=provenance,
                entries=[TimelineEntry(at=clean(e.get('at')),stage=clean(e.get('stage')),detail=clean(e.get('node')))
                    for e in graph.get('events', [])[-32:] if isinstance(e,dict)]))
            outcome = context(graph_row).get('result') or {}
            if outcome.get('message'):
                result.append(Card(id='agents-answer:'+tid,title='Unified multi-agent result',provenance=provenance,
                    text=clean(outcome.get('message'))))
        if status in {'FAILED','DENIED'}:
            result.append(Notice(kind='error',id='error:'+tid,title='Task did not complete',provenance=provenance,
                text=clean(receipt.get('message') or row.get('last_error') or 'No successful outcome was recorded.')))
    # Reserve seven slots for A13 discovery. Keep each task's leading details
    # before older history when a graph adds components to a busy workspace.
    return result[:121]


class WorkspaceStore:
    """One previous revision per device; lagging/restarted clients receive snapshots."""
    def __init__(self):
        self.epoch=uuid4().hex
        self.views=OrderedDict()

    async def sync(self, source, cursor):
        items=project(source,await owned_tasks(source))
        from .executor_registry import ExecutorRegistry
        if any(h.metadata.get('capability_discovery') for h in ExecutorRegistry.get_instance().list_device_handles()):
            from .discovery import candidates
            rows = await candidates(source)
            tag = hashlib.sha256(source.encode()).hexdigest()[:16]
            provenance = Provenance(source='capability_discovery',task_id='catalog:'+tag)
            for offset in range(0,min(len(rows),120),20):
                items.append(Table(id=f'discovery:{tag}:{offset}',title='Discovered capabilities',provenance=provenance,
                    columns=['Capability','Executor','Availability','Authorization','Execution','Reason'],
                    rows=[[clean(c['capability_id']),clean(c['provider_id']+(' / '+c['account_ref'] if 'account_ref' in c else '')+' / '+c['executor_location']),
                        'Available' if c['available'] else 'Unavailable',
                        ('Authorized' if c['authorized'] else 'Unauthorized')+f" (Tier {c['permission_tier']})",
                        clean(c['state']),clean(c['reason'])]
                        for c in rows[offset:offset+20]]))
            items.append(Notice(kind='status',id='discovery-summary:'+tag,title='Capability discovery',provenance=provenance,
                text=f'Showing {min(len(rows),120)} of {len(rows)} candidates. Discovery never grants permission. Full details are available from the authenticated discovery API.'))
        current={item.id:item.model_dump() for item in items}
        old=self.views.get(source)
        if old is None:
            entry={'revision':1,'items':current,'previous':{},'previous_revision':0}
        elif old['items']!=current:
            entry={'revision':old['revision']+1,'items':current,'previous':old['items'],'previous_revision':old['revision']}
        else:entry=old
        self.views[source]=entry
        self.views.move_to_end(source)
        while len(self.views)>64:self.views.popitem(last=False)
        mode='snapshot'; removed=[]; outgoing=list(current.values())
        if cursor.epoch==self.epoch and cursor.revision==entry['revision']:
            mode='delta';outgoing=[]
        elif cursor.epoch==self.epoch and cursor.revision==entry['previous_revision'] and cursor.revision>0:
            mode='delta'
            outgoing=[value for key,value in current.items() if entry['previous'].get(key)!=value]
            removed=[key for key in entry['previous'] if key not in current]
        return Envelope(epoch=self.epoch,revision=entry['revision'],mode=mode,
            base_revision=cursor.revision if mode=='delta' else None,
            generated_at=datetime.now(timezone.utc).isoformat(),items=outgoing,removed=removed)
