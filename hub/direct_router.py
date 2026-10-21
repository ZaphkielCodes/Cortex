"""Canonical model/provider router: structured ranking, privacy, health, bounded failover."""
import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import os
import re
import time

from .direct_provider import DirectLLMProvider
from .llm_provider import LLMProvider, ReasoningProviderUnavailable
from .routing_profiles import TaskProfile, ModelProfile, ProviderProfile, model_profile
from .model_selection import ModelSelector
from .provider_health import ProviderHealthStore
from .provider_failover import FailoverManager
from .failover_budget import allocate_attempt

log = logging.getLogger("cortex.direct_router")


@dataclass
class Route:
    provider: object
    profile: ModelProfile | None = None
    cooldown_until: float = 0
    calls: deque = field(default_factory=lambda: deque(maxlen=512))
    last_failure_category: str = ""
    last_http_status: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        if self.profile is None: self.profile = model_profile(self.provider)

    def reserve(self, tokens):
        now = time.monotonic()
        while self.calls and self.calls[0][0] <= now-86400: self.calls.popleft()
        if now < self.cooldown_until: return False
        reservation = [now,tokens]
        self.calls.append(reservation)
        return reservation


class DirectModelRouter(LLMProvider):
    name = "direct"
    local = None
    privacy_selects_candidates = True

    def __init__(self, providers=None, *, profiles=None, health=None):
        default_configuration = providers is None
        if providers is None:
            providers = []
            if os.getenv("GROQ_API_KEY"):
                try:
                    providers.extend([DirectLLMProvider("groq",os.getenv("CORTEX_GROQ_MODEL","qwen/qwen3.8-27b")),
                                      DirectLLMProvider("groq","openai/gpt-oss-120b")])
                except ValueError: log.warning("provider_configuration_rejected provider=groq")
            if os.getenv("CORTEX_ENABLE_NVIDIA_TRIAL","0") == "1" and os.getenv("NVIDIA_API_KEY"):
                try:
                    providers.extend([DirectLLMProvider("nvidia",os.getenv("CORTEX_NVIDIA_MODEL","deepseek-ai/deepseek-v4.1-flash")),
                                      DirectLLMProvider("nvidia","z-ai/glm-5.3")])
                except ValueError: log.warning("provider_configuration_rejected provider=nvidia")
            if os.getenv("GEMINI_API_KEY"):
                from .gemini_route import GeminiRoute
                providers.append(GeminiRoute(model_name=os.getenv("CORTEX_GEMINI_MODEL","gemini-3.8-flash")))
        self.health = health if health is not None else ProviderHealthStore.default() if default_configuration else ProviderHealthStore()
        profiles = profiles or self._configured_profiles()
        unique = {(p.name,p.model):p for p in providers}
        self.routes = [Route(p,profiles.get(key)) for key,p in unique.items()]
        self.providers = {p.name:ProviderProfile(p.name,bool(getattr(p,"configured",True)),
            getattr(p,"locality","cloud_worker"),getattr(p,"max_privacy_class","SENSITIVE")) for p in unique.values()}
        self.selector = ModelSelector(self.health)
        self.failover = FailoverManager()
        self.events = deque(maxlen=128)
        self.last_routing = None

    @staticmethod
    def _configured_profiles():
        path = os.getenv("CORTEX_MODEL_PROFILE_PATH")
        if not path: return {}
        from pathlib import Path
        try:
            source=Path(path)
            if source.stat().st_size>65536: raise ValueError("oversized profile file")
            rows=json.loads(source.read_text(encoding="utf-8"))
            return {(r["provider_id"],r["model_id"]):ModelProfile(**r) for r in rows[:32]}
        except (OSError,ValueError,KeyError,TypeError):
            log.warning("model_profiles_invalid using_conservative_defaults=true")
            return {}

    @property
    def model(self): return self.routes[0].provider.model if self.routes else "not-configured"

    @property
    def configured(self): return any(getattr(r.provider,"configured",True) for r in self.routes)

    def candidates(self, context):
        ranked,_ = self.selector.rank(self.routes,TaskProfile.from_context(context),pending_approval=True)
        return [r for r,_ in ranked]

    async def select_for_turn(self, utterance, context, history):
        task=TaskProfile.from_context(context)
        ranked,rejected=self.selector.rank(self.routes,task,pending_approval=True)
        if not ranked:
            prospective,_=self.selector.rank(self.routes,task,pending_approval=True,ignore_health=True)
            if prospective:
                await self.record(context,{"event":"no_eligible_provider","task_profile":task.to_dict(),"rejected_candidates":rejected})
                raise ReasoningProviderUnavailable("PROVIDER_COOLDOWN")
        if not ranked:
            await self.record(context,{"event":"no_eligible_provider","task_profile":task.to_dict(),"rejected_candidates":rejected})
            raise ReasoningProviderUnavailable(self.no_candidate_category(rejected))
        return TurnRoute(self,[r for r,_ in ranked],task.privacy_class,context),context

    def no_candidate_category(self,rejected):
        reasons={row["reason"] for row in rejected}
        if not self.configured: return "PROVIDER_NOT_CONFIGURED"
        if reasons & {"privacy_ineligible","provider_terms","privacy_approval_required"}: return "PRIVATE_MODEL_REQUIRED"
        if reasons & {"context_too_large","context_window_unknown"}: return "CONTEXT_TOO_LARGE"
        if "unsupported_tool_use" in reasons: return "UNSUPPORTED_TOOL_USE"
        return "PROVIDER_COOLDOWN"

    async def generate_result(self,user_text,context,on_chunk=None):
        selected,_=await self.select_for_turn(user_text,context,context.get("conversation_history",[]))
        return await selected.generate_result(user_text,context,on_chunk)

    async def record(self,context,event):
        from .tracing import scrub_secrets
        event=scrub_secrets(dict(event,at=time.time()))
        task_id=context.get("operational_task_id")
        event["task_id"]=task_id
        self.events.append(event)
        self.last_routing=event
        log.info("routing_event event=%s provider=%s model=%s task_id=%s",event.get("event"),
            event.get("selected_provider"),event.get("selected_model"),task_id)
        if task_id:
            from .database import get_task,update_task
            stored=await get_task(task_id)
            if stored:
                saved=stored.get("context") or {}
                saved=json.loads(saved) if isinstance(saved,str) else dict(saved)
                event["parent_task_id"]=saved.get("parent_request_id")
                saved["routing_events"]=(saved.get("routing_events",[])+[event])[-128:]
                await update_task(task_id,{"context":saved})

    def status(self):
        return [{"provider":r.provider.name,"model":r.provider.model,
            "configured":bool(getattr(r.provider,"configured",True)),
            "cooldown_seconds":max(0,round(max(r.cooldown_until-time.monotonic(),
                (self.health.blocked(r.provider.name,r.provider.model).cooldown_until-self.health.clock()) if self.health.blocked(r.provider.name,r.provider.model) else 0))),
            "requests_reserved":len(r.calls),"rate_limits":getattr(r.provider,"last_rate_limits",{}),
            "last_failure_category":r.last_failure_category or self.health.get(r.provider.name,r.provider.model).last_failure_category,
            "health":self.health.snapshot(r.provider.name,r.provider.model),
            "provider_health":self.health.snapshot(r.provider.name)} for r in self.routes]


class TurnRoute(LLMProvider):
    def __init__(self,router,routes,privacy,context=None):
        self.router,self.routes,self.privacy=router,routes,privacy
        self.context=dict(context or {})
        self.primary=routes[0].provider
        self.authorized_key=None
        self.authorized_class=None

    @property
    def name(self): return self.primary.name
    @property
    def model(self): return self.primary.model
    @property
    def locality(self): return getattr(self.primary,"locality","cloud_worker")
    @property
    def max_privacy_class(self): return getattr(self.primary,"max_privacy_class","SENSITIVE")

    def authorize_recipient(self,privacy_class):
        # Called by AgentTurn only after its existing policy/confirmation gate.
        from .privacy import PRIVACY_ORDER
        if privacy_class in PRIVACY_ORDER and PRIVACY_ORDER[privacy_class]>=PRIVACY_ORDER["PERSONAL"]:
            self.authorized_key=(self.name,self.model)
            self.authorized_class=privacy_class

    @staticmethod
    def estimate(user_text,context):
        # Context sizing only; never an invented quota or a local request limiter.
        parts=[user_text,context.get("system_prompt",""),json.dumps(context.get("conversation_history",[])),
               json.dumps(context.get("tool_result",{}),default=str)]
        return sum(len(str(p).encode("utf-8")) for p in parts)//4+1024

    async def generate_result(self,user_text,context,on_chunk=None):
        from .config import get_lane_deadlines
        from .privacy import PRIVACY_ORDER
        ctx=dict(self.context,**context)
        floor=max((self.privacy,self.authorized_class or "PUBLIC"),key=lambda p:PRIVACY_ORDER.get(p,99))
        task=TaskProfile.from_context(ctx,privacy_floor=floor,context_tokens=self.estimate(user_text,ctx))
        ctx["privacy_class"]=task.privacy_class
        authorized_key=self.authorized_key if (self.authorized_class is not None and
            PRIVACY_ORDER.get(task.privacy_class,99)<=PRIVACY_ORDER[self.authorized_class]) else None
        # Each call is a reasoning turn. Successful OS actions stay owned by AgentTurn.
        fallback=ctx.get("allow_provider_fallback",True) and self.authorized_key is None
        allowed=self.router.routes if fallback else [r for r in self.router.routes if r.provider is self.primary]
        attempted,excluded,retries=set(),set(),{}
        failures=[]
        safe_waits={}
        minimum_window=0
        try: max_wait=max(0,min(90,float(os.getenv("CORTEX_PROVIDER_RATE_WAIT_SECONDS","65"))))
        except ValueError: max_wait=65
        active_budget=get_lane_deadlines(ctx.get("routing_lane"))[1]
        if task.background_allowed: active_budget=max(active_budget,60.)
        end=time.monotonic()+active_budget
        waited=0.
        total=0
        attempted_actual=set()
        budget_exhausted=False
        unattempted=[]
        while total < self.router.failover.MAX_ATTEMPTS:
            ranked,rejected=self.router.selector.rank(allowed,task,authorized_key=authorized_key,
                attempted=attempted,excluded_providers=excluded,minimum_window=minimum_window)
            if not ranked:
                # Only an explicit transient Retry-After permits revisiting a candidate.
                waits=[(deadline-self.router.health.clock(),key,route) for key,(deadline,route) in safe_waits.items()
                    if retries.get(key,0)<self.router.failover.MAX_SAFE_RETRIES]
                delay,key,route=min(waits,key=lambda item:item[0]) if waits else (max_wait+1,None,None)
                delay=max(0,delay)+.25
                if key and delay <= max_wait-waited and time.monotonic()+delay < end+max_wait-waited:
                    from .compound_ui import provider_wait
                    await self.router.record(ctx,{"event":"provider_retry_wait","delay_seconds":round(delay,3),"task_profile":task.to_dict()})
                    await provider_wait(ctx.get("operational_task_id"),True)
                    try: await asyncio.sleep(delay)
                    finally: await provider_wait(ctx.get("operational_task_id"),False)
                    waited+=delay
                    end+=delay
                    retries[key]=retries.get(key,0)+1
                    attempted.discard(key)
                    excluded.discard(key[0])
                    safe_waits.pop(key,None)
                    continue
                await self.router.record(ctx,{"event":"no_eligible_provider","task_profile":task.to_dict(),"rejected_candidates":rejected,"attempts":total})
                break
            route,score=ranked[0]
            provider=route.provider
            key=(provider.name,provider.model)
            allocation=allocate_attempt(end-time.monotonic(),ranked,self.router.health,
                background=task.background_allowed,attempts_left=self.router.failover.MAX_ATTEMPTS-total)
            if allocation is None:
                budget_exhausted=True
                unattempted=[{"provider":r.provider.name,"model":r.provider.model,
                    "outcome":"NOT_ATTEMPTED_BUDGET_EXHAUSTED"} for r,_ in ranked]
                break
            await self.router.record(ctx,{"event":"model_selected","task_profile":task.to_dict(),
                "selected_provider":provider.name,"selected_model":provider.model,
                "eligible_candidates":[row for _,row in ranked],"rejected_candidates":rejected,
                "attempt_budget":allocation.to_dict(),
                "routing_reason":"highest_eligible_score" if not failures else "recomputed_after_failure"})
            # Lock/event overhead belongs to the same deadline as inference.
            try:
                async with asyncio.timeout(max(0.,end-time.monotonic())):
                    async with route.lock:
                        reservation=False if self.router.health.blocked(*key) else route.reserve(task.context_tokens)
            except TimeoutError:
                reservation=False
            allocation=allocate_attempt(end-time.monotonic(),ranked,self.router.health,
                background=task.background_allowed,attempts_left=self.router.failover.MAX_ATTEMPTS-total)
            if allocation is None:
                budget_exhausted=True
                unattempted=[{"provider":r.provider.name,"model":r.provider.model,
                    "outcome":"NOT_ATTEMPTED_BUDGET_EXHAUSTED"} for r,_ in ranked]
                break
            attempted.add(key)
            total+=1
            if not reservation: continue
            await self.router.record(ctx,{"event":"provider_attempt_started","task_profile":task.to_dict(),
                "selected_provider":provider.name,"selected_model":provider.model,
                "attempt_budget":allocation.to_dict()})
            # Preserve reserved windows even if persisting the event took time.
            timeout=min(allocation.seconds,max(0.,end-time.monotonic()-allocation.reserved_seconds))
            if timeout < allocation.minimum_window_seconds:
                budget_exhausted=True
                unattempted=[{"provider":r.provider.name,"model":r.provider.model,
                    "outcome":"NOT_ATTEMPTED_BUDGET_EXHAUSTED"} for r,_ in ranked]
                break
            started=time.monotonic()
            attempted_actual.add(key)
            try:
                async with asyncio.timeout(timeout):
                    result=await provider.generate_result(user_text,dict(ctx,_router_timeout_seconds=timeout))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure=self.router.failover.classify(exc)
                status=self.router.failover.http_status(exc)
                failures.append((failure.category,status))
                route.last_failure_category=failure.category
                route.last_http_status=status
                self.router.health.failed(provider.name,provider.model,failure)
                if failure.scope=="provider": excluded.add(provider.name)
                if failure.safe_retry_after:
                    blocked=self.router.health.blocked(*key)
                    safe_waits[key]=(blocked.cooldown_until,route)
                if failure.kind=="CONTEXT_TOO_LARGE":
                    minimum_window=max(task.context_tokens,(route.profile.context_window or task.context_tokens)+1)
                    task=replace(task,requires_long_context=True)
                await self.router.record(ctx,{"event":"provider_failed","task_profile":task.to_dict(),
                    "selected_provider":provider.name,"selected_model":provider.model,
                    "failover_trigger":{"kind":failure.kind,"category":failure.category,"scope":failure.scope},
                    "attempt_outcome":"PROVIDER_TIMEOUT" if failure.kind=="TIMEOUT" else
                        "PROVIDER_UNAVAILABLE" if failure.kind in {"AUTH_FAILURE","DAILY_QUOTA_EXHAUSTED","TEMPORARY_COOLDOWN"} else "PROVIDER_ERROR",
                    "attempt_budget_seconds":timeout,"latency_seconds":round(time.monotonic()-started,3),
                    "http_status":status,
                    "health":self.router.health.snapshot(provider.name,provider.model),
                    "provider_health":self.router.health.snapshot(provider.name)})
                if not failure.retryable: break
                continue
            self.primary=provider
            self.router.health.succeeded(provider.name,provider.model,time.monotonic()-started,getattr(provider,"last_rate_limits",{}))
            usage=getattr(provider,"last_usage",None)
            if isinstance(usage,dict) and type(usage.get("total_tokens")) is int:
                reservation[1]=usage["total_tokens"]
            trace=ctx.get("trace")
            if trace:
                trace.model_provider=provider.name
                trace.fallback_occurred=len(attempted_actual)>1
            await self.router.record(ctx,{"event":"routing_succeeded","task_profile":task.to_dict(),
                "selected_provider":provider.name,"selected_model":provider.model,"attempts":total,
                "attempt_budget_seconds":timeout,"latency_seconds":round(time.monotonic()-started,3),
                "failover":len(attempted_actual)>1,"retried":total>1})
            if on_chunk: await on_chunk(result.response_text or "")
            return result
        outcome="NO_USABLE_RESPONSE_WITHIN_FAILOVER_BUDGET" if budget_exhausted else "NO_USABLE_RESPONSE"
        await self.router.record(ctx,{"event":"routing_failed","task_profile":task.to_dict(),
            "outcome":outcome,"unattempted_candidates":unattempted,"attempts":len(attempted_actual)})
        if failures:
            unavailable=ReasoningProviderUnavailable(failures[0][0],failures[-1][0] if len(failures)>1 else None,
                failures[-1][1] if len(failures)>1 else None)
        else:
            unavailable=ReasoningProviderUnavailable(self.router.no_candidate_category(rejected))
        unavailable.routing_outcome=outcome
        unavailable.unattempted_candidates=unattempted
        raise unavailable


def retry_delay(value, category):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            # Provider reset headers use protocol durations such as 1m2.5s.
            # This parses response metadata, never user requests or account quotas.
            parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", value or "")
            if parts and "".join(number+unit for number,unit in parts) == value:
                factors = {"ms": .001, "s": 1, "m": 60, "h": 3600, "d": 86400}
                delay = sum(float(number)*factors[unit] for number,unit in parts)
            else:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = 300 if category == "RATE_LIMIT_429" else 60
    return max(1, min(delay, 86400))
