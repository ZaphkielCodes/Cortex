"""Stage 1 opt-in, durable admission accounting. Never stores request/response content."""
import json
import hashlib
import asyncio
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


def denied():
    # Existing frozen failover treats 400 as non-retryable: never route around a cap.
    from .llm_provider import ProviderHTTPError
    return ProviderHTTPError('budget_guard', 400)


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 10**15:
        raise ValueError('Invalid budget quantity')
    return value


class BudgetLedger:
    def __init__(self, path, daily_limit, *, initialize=False, clock=time.time, total_limit=None, experiment="legacy", policy_version="legacy", price=None, task_id=None):
        self.path = Path(path).resolve()
        self.limit = integer(daily_limit)
        self.clock = clock
        self.total_limit = integer(total_limit) if total_limit is not None else 10**15
        self.experiment = hashlib.sha256(experiment.encode()).hexdigest()
        self.task_id = hashlib.sha256(str(task_id or "unattributed").encode()).hexdigest()
        self.policy = json.dumps(dict(version=policy_version,daily_limit=self.limit,total_limit=self.total_limit),sort_keys=True)
        self.price = json.dumps(price or {},sort_keys=True)
        self.price_id = hashlib.sha256(self.price.encode()).hexdigest()
        if initialize:
            with sqlite3.connect(self.path) as db:
                db.execute('CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, day TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, reserved INTEGER NOT NULL, charged INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER, status TEXT NOT NULL, latency_ms INTEGER)')
                db.execute('CREATE TABLE IF NOT EXISTS policies (experiment TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS prices (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS call_meta (id TEXT PRIMARY KEY, experiment TEXT NOT NULL, task TEXT NOT NULL, price TEXT NOT NULL, reasoning_tokens INTEGER)')
                db.execute('CREATE TABLE IF NOT EXISTS reconciliation (id TEXT PRIMARY KEY, prior_charge INTEGER, new_charge INTEGER, evidence_hash TEXT NOT NULL, timestamp REAL)')
        if not self.path.is_file():
            raise ValueError('Ledger must be explicitly initialized')

    def connect(self):
        # mode=rw prevents silent reset if the ledger is removed or unavailable.
        return sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, timeout=2)

    def reserve(self, provider, model, amount):
        amount = integer(amount)
        day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            stored = db.execute('SELECT snapshot FROM policies WHERE experiment=?',(self.experiment,)).fetchone()
            if stored and stored[0] != self.policy:
                raise denied()
            db.execute('INSERT OR IGNORE INTO policies VALUES (?,?)',(self.experiment,self.policy))
            db.execute('INSERT OR IGNORE INTO prices VALUES (?,?)',(self.price_id,self.price))
            total = db.execute('SELECT COALESCE(SUM(c.charged),0) FROM calls c JOIN call_meta m ON c.id=m.id WHERE m.experiment=?',(self.experiment,)).fetchone()[0]
            if total + amount > self.total_limit:
                raise denied()
            # Uncertain charges survive midnight and process restart until reconciled.
            if db.execute("SELECT 1 FROM calls WHERE status='overrun' LIMIT 1").fetchone():
                raise denied()
            used = db.execute("SELECT COALESCE(SUM(charged),0) FROM calls WHERE day=? OR status IN ('reserved','unknown','overrun')", (day,)).fetchone()[0]
            if used + amount > self.limit:
                raise denied()
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,NULL,NULL,?,NULL)',
                       (ident, day, provider, model, amount, amount, 'reserved'))
            db.execute("INSERT INTO call_meta VALUES (?,?,?,?,NULL)",(ident,self.experiment,self.task_id,self.price_id))
            return ident

    def settle(self, ident, usage, price, elapsed):
        status, inp, out = 'unknown', None, None
        reasoning = None
        charge = None
        try:
            inp = integer(usage['prompt_tokens'])
            out = integer(usage['completion_tokens'])
            total = integer(usage['total_tokens'])
            reasoning = integer((usage.get('completion_tokens_details') or {}).get('reasoning_tokens',0))
            if reasoning > out:
                raise ValueError('Reasoning usage inconsistent')
            if total != inp + out:
                raise ValueError('Inconsistent usage')
            charge = inp * price['input_nusd_per_token'] + out * price['output_nusd_per_token']
            integer(charge)
            status = 'accounted'
        except (ValueError, KeyError, TypeError):
            inp = out = reasoning = None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT reserved,status FROM calls WHERE id=?',(ident,)).fetchone()
            if not row or row[1] != 'reserved':
                raise ValueError('Settlement must occur once')
            reserved = row[0]
            if charge is not None and charge > reserved:
                status = 'overrun'
            db.execute('UPDATE calls SET charged=?,input_tokens=?,output_tokens=?,status=?,latency_ms=? WHERE id=?',
                       (reserved if charge is None else charge, inp, out, status, max(0,int(elapsed*1000)),ident))
            db.execute('UPDATE call_meta SET reasoning_tokens=? WHERE id=?',(reasoning,ident))


    def aggregate(self):
        with self.connect() as db:
            return [dict(task=row[0], attempts=row[1], charged_nusd=row[2], unknown=row[3]) for row in db.execute(
                "SELECT m.task,COUNT(*),SUM(c.charged),SUM(c.status IN ('unknown','reserved','overrun')) FROM calls c JOIN call_meta m ON c.id=m.id WHERE m.experiment=? GROUP BY m.task",(self.experiment,))]

    def attempts(self):
        """Content-free audit rows, including transport failures and retained charges."""
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT c.*,m.task,m.price,m.reasoning_tokens FROM calls c JOIN call_meta m ON c.id=m.id WHERE m.experiment=? ORDER BY c.rowid",(self.experiment,))]

    def reconcile(self, ident, charged, evidence_hash):
        # Offline operator-only procedure, never exposed as a model capability.
        import re
        integer(charged)
        if not re.fullmatch('[0-9a-f]{64}',evidence_hash):
            raise ValueError('A digest of authoritative billing evidence is required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT charged,status FROM calls WHERE id=?",(ident,)).fetchone()
            if not row or row[1] not in {'reserved','unknown','overrun'}:
                raise ValueError('Only uncertain charges may be reconciled once')
            db.execute('INSERT INTO reconciliation VALUES (?,?,?,?,?)',(ident,row[0],charged,evidence_hash,self.clock()))
            db.execute("UPDATE calls SET charged=?,status='reconciled' WHERE id=?",(charged,ident))


class BudgetTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner, ledger, provider, model, price):
        self.inner, self.ledger = inner, ledger
        self.provider, self.model = provider, model
        self.price = dict(price)
        for k in ('input_nusd_per_token','output_nusd_per_token','max_input_tokens','max_output_tokens'):
            integer(self.price[k], 1)

    async def handle_async_request(self, request):
        try:
            payload = json.loads(request.content)
            if payload.get('model') != self.model or payload.get('stream') is not False:
                raise ValueError('Unsupported request')
            if any(not isinstance(m.get('content'),str) for m in payload['messages']):
                raise ValueError('Multimodal pricing is not verified')
            # Reserve full reviewed model limits, not byte/4 guesses. Payload bytes
            # additionally bound text admission; pricing must include hidden tokens.
            if len(request.content) > self.price['max_input_tokens']:
                raise ValueError('Input exceeds conservative admission bound')
            output = integer(payload['max_tokens'],1)
            if output > self.price['max_output_tokens']:
                raise ValueError('Output exceeds reviewed limit')
            reserve = (self.price['max_input_tokens']*self.price['input_nusd_per_token'] +
                       self.price['max_output_tokens']*self.price['output_nusd_per_token'])
            ident = self.ledger.reserve(self.provider,self.model,reserve)
        except Exception:
            raise denied() from None
        start = time.monotonic()
        usage = None
        try:
            response = await self.inner.handle_async_request(request)
            await response.aread()
            if response.status_code == 200:
                try: usage = response.json().get('usage')
                except (ValueError,AttributeError): pass
            return response
        finally:
            # Network errors, cancellation, missing usage and malformed bodies keep
            # the full reservation; a crash before here leaves it reserved on disk.
            try: self.ledger.settle(ident,usage,self.price,time.monotonic()-start)
            except Exception: raise denied() from None

    async def aclose(self):
        await self.inner.aclose()


def configured_transport(provider, model, inner, context=None):
    config_path = os.getenv('CORTEX_BUDGET_CONFIG')
    if not config_path:
        return inner  # Legacy mode is not a paid-evaluation authorization.
    try:
        config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        if config['version'] != 2:
            raise ValueError('Unsupported config')
        price = config['models'][provider+'/'+model]
        # Explicit expiry; do not treat outdated/missing prices as zero cost.
        expiry = datetime.fromisoformat(price['valid_until'])
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError('Price evidence expired')
        if price.get('text_only_all_tokens') is not True:
            raise ValueError('Unverified billing coverage')
        if not config.get('experiment_id') or not config.get('policy_version') or not price.get('version'):
            raise ValueError('Versioned policy and price required')
        # Store only the reviewed numeric contract, never arbitrary admin fields.
        price = {k:price[k] for k in ('version','valid_until','text_only_all_tokens','input_nusd_per_token',
            'output_nusd_per_token','max_input_tokens','max_output_tokens')}
        ledger = BudgetLedger(config['ledger_path'],config['daily_limit_nusd'],
            total_limit=config['total_limit_nusd'],experiment=config['experiment_id'],policy_version=config['policy_version'],price=price,
            task_id=(context or {}).get('operational_task_id') or (context or {}).get('request_id'))
        return BudgetTransport(inner or httpx.AsyncHTTPTransport(),ledger,provider,model,price)
    except Exception:
        raise denied() from None


async def metered_gemini(provider, context, input_text, output_limit, call):
    """One SDK request; SDK retries disabled by caller, usage includes thoughts."""
    if not os.getenv('CORTEX_BUDGET_CONFIG'):
        return await call()
    wrapper = configured_transport('gemini',provider.model,httpx.MockTransport(lambda r: None),context)
    price,ledger=wrapper.price,wrapper.ledger
    if len(input_text.encode()) > price['max_input_tokens'] or output_limit > price['max_output_tokens']:
        raise denied()
    amount=price['max_input_tokens']*price['input_nusd_per_token']+price['max_output_tokens']*price['output_nusd_per_token']
    try:ident=ledger.reserve('gemini',provider.model,amount)
    except Exception:raise denied() from None
    start=time.monotonic();usage=None
    try:
        response=await call()
        meta=response.usage_metadata
        inp=integer(meta.prompt_token_count)
        visible=integer(meta.candidates_token_count)
        thoughts=integer(meta.thoughts_token_count or 0)
        total=integer(meta.total_token_count)
        usage=dict(prompt_tokens=inp,completion_tokens=visible+thoughts,total_tokens=total,
            completion_tokens_details={'reasoning_tokens':thoughts})
        provider.last_usage=usage
        return response
    finally:
        try:ledger.settle(ident,usage,price,time.monotonic()-start)
        except Exception:raise denied() from None
