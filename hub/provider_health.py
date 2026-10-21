"""Bounded provider/model observations. Quota estimates never create limits."""
from dataclasses import dataclass, asdict
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

log = logging.getLogger("cortex.provider_health")


@dataclass
class Health:
    state: str = "unknown"
    last_success: float | None = None
    last_failure: float | None = None
    last_failure_category: str | None = None
    failure_class: str | None = None
    cooldown_until: float = 0
    quota_reset_at: float | None = None
    quota_scope: str | None = None
    known_limit: float | None = None
    known_used: float | None = None
    known_remaining: float | None = None
    average_latency: float | None = None
    successes: int = 0
    failures: int = 0
    expires_at: float = 0
    statistics_until: float = 0

    def __post_init__(self):
        if self.state not in {"unknown","healthy","degraded","cooldown","quota_exhausted","unavailable"}:
            raise ValueError("Invalid health state")
        for value in (self.last_success,self.last_failure,self.cooldown_until,self.quota_reset_at,
                      self.known_limit,self.known_used,self.known_remaining,self.average_latency,
                      self.expires_at,self.statistics_until):
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value < 0):
                raise ValueError("Invalid health measurement")
        if any(type(value) is not int or not 0 <= value <= 100_000_000 for value in (self.successes,self.failures)):
            raise ValueError("Invalid health count")


class ProviderHealthStore:
    _shared = {}

    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.entries = {}
        self.lock = threading.RLock()
        if self.path and self.path.exists():
            try:
                if self.path.stat().st_size > 256_000: raise ValueError("oversized health file")
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                for key,value in list(saved.get("entries",{}).items())[:128]:
                    row = Health(**value)
                    # An on-disk timestamp never blocks indefinitely after corruption.
                    for stamp in (row.cooldown_until,row.expires_at,row.statistics_until):
                        if not isinstance(stamp,(int,float)) or stamp > self.clock()+172800: raise ValueError("invalid health expiry")
                    self.entries[key] = row
            except (OSError,ValueError,TypeError,AttributeError):
                self.entries = {}
                log.warning("provider_health_load_failed reason=invalid_or_unavailable_state")

    @classmethod
    def default(cls):
        path = Path(os.getenv("CORTEX_STATE_DIR",str(Path.home()/".cortex")))/"provider-health.json"
        key = str(path.resolve())
        if key not in cls._shared: cls._shared[key] = cls(path)
        return cls._shared[key]

    @staticmethod
    def key(provider,model=None): return json.dumps([provider,model],separators=(",",":"))

    def get(self, provider, model=None):
        with self.lock:
            key = self.key(provider,model)
            row = self.entries.setdefault(key,Health())
            now = self.clock()
            if row.expires_at and now >= row.expires_at:
                row.state = "unknown"
                row.cooldown_until = 0
                row.quota_reset_at = None
                row.quota_scope = None
                row.known_limit = row.known_used = row.known_remaining = None
                row.expires_at = 0
            if row.cooldown_until and now >= row.cooldown_until:
                row.cooldown_until = 0
                if row.state in {"cooldown","quota_exhausted","unavailable"}:
                    row.state = "unknown"
                    if row.quota_reset_at and now >= row.quota_reset_at:
                        row.known_limit = row.known_used = row.known_remaining = None
                        row.quota_reset_at = None
                        row.quota_scope = None
            if row.statistics_until and now >= row.statistics_until:
                row.successes = row.failures = 0
                row.average_latency = None
                row.last_success = row.last_failure = None
                row.last_failure_category = row.failure_class = None
                if row.state in {"healthy","degraded"}: row.state = "unknown"
                row.statistics_until = 0
            return row

    def blocked(self, provider, model):
        for row in (self.get(provider),self.get(provider,model)):
            if row.state in {"cooldown","quota_exhausted","unavailable"} and row.cooldown_until > self.clock():
                return row
        return None

    def save(self):
        if not self.path: return
        with self.lock:
            # Load-time and save-time retention are both bounded.
            rows = dict(sorted(self.entries.items(),key=lambda kv:kv[1].last_failure or kv[1].last_success or 0,reverse=True)[:128])
            payload = json.dumps({"version":1,"entries":{k:asdict(v) for k,v in rows.items()}},allow_nan=False)
            temp = self.path.with_name(self.path.name+"."+uuid4().hex+".tmp")
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                fd = os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,"w",encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp,self.path)
            except OSError:
                log.warning("provider_health_persistence_failed state_retained_in_memory=true")
            finally:
                try: temp.unlink(missing_ok=True)
                except OSError: pass

    def clear(self, provider, model=None):
        with self.lock:
            self.entries.pop(self.key(provider,model),None)
            self.save()

    def failed(self, provider, model, failure):
        with self.lock:
            now = self.clock()
            target_model = model if failure.scope == "model" else None
            row = self.get(provider,target_model)
            row.last_failure = now
            row.last_failure_category = failure.category
            row.failure_class = failure.kind
            row.failures += 1
            row.statistics_until = now+86400
            row.state = "quota_exhausted" if failure.kind == "DAILY_QUOTA_EXHAUSTED" else "unavailable" if failure.kind == "AUTH_FAILURE" else "cooldown"
            row.cooldown_until = now+failure.delay
            row.expires_at = row.cooldown_until
            # No declared reset => local knowledge expiry, not an invented quota reset.
            row.quota_reset_at = now+failure.delay if failure.reset_known else None
            for key,value in failure.quota.items():
                if hasattr(row,key): setattr(row,key,value)
            if target_model is None:
                per_model = self.get(provider,model)
                per_model.last_failure = now
                per_model.last_failure_category = failure.category
                per_model.failure_class = failure.kind
                per_model.failures += 1
                per_model.statistics_until = now+86400
                per_model.state = "degraded"
            self.save()

    def succeeded(self, provider, model, latency, headers=None):
        with self.lock:
            now = self.clock()
            for row in (self.get(provider),self.get(provider,model)):
                # An admitted request can finish after a concurrent quota/auth failure.
                # That late success does not prove the active restriction has reset.
                if row.cooldown_until <= now:
                    row.state = "healthy"
                    row.cooldown_until = 0
                row.last_success = now
                row.successes += 1
                row.statistics_until = now+86400
            row = self.get(provider,model)
            row.average_latency = latency if row.average_latency is None else .25*latency+.75*row.average_latency
            # Response headers are observations, never derived from reservations.
            headers = headers or {}
            def number(key):
                try:
                    value=float(headers[key]); return value if 0 <= value < 1e15 else None
                except (KeyError,ValueError,TypeError): return None
            if row.cooldown_until <= now:
                row.known_limit = number("x-ratelimit-limit-tokens")
                row.known_remaining = number("x-ratelimit-remaining-tokens")
                row.known_used = row.known_limit-row.known_remaining if row.known_limit is not None and row.known_remaining is not None and row.known_limit >= row.known_remaining else None
                row.expires_at = now+3600
            balance = "tokens" if row.known_remaining == 0 else "requests" if number("x-ratelimit-remaining-requests") == 0 else None
            if balance:
                from .direct_router import retry_delay
                reset = headers.get("x-ratelimit-reset-"+balance)
                delay = retry_delay(reset,"RATE_LIMIT_429") if reset else 60
                blocked = row if headers.get("x-ratelimit-scope") == "model" else self.get(provider)
                if blocked.cooldown_until <= now:
                    blocked.state = "quota_exhausted"
                    blocked.quota_scope = balance
                    blocked.known_limit = number("x-ratelimit-limit-"+balance)
                    blocked.known_remaining = 0
                    blocked.known_used = blocked.known_limit
                    blocked.quota_reset_at = now+delay if reset else None
                    blocked.cooldown_until = blocked.expires_at = now+delay
            self.save()

    def snapshot(self,provider,model=None):
        return asdict(self.get(provider,model))
