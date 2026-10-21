"""One integration catalog: adapters, account/scopes, health, and safe execution."""
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
import time
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .definitions import AuthState, Effect, ProviderConnection, SERVICES, definitions
from .google_auth import IntegrationError, account_key, token_state, _get_client_id, _get_client_secret
from .google_api import GoogleAPI
from ..credentials import CredentialStore


class NormalizedResult(BaseModel):
    model_config=ConfigDict(extra="forbid")
    status: Literal["success","failure","needs_approval"]
    provider: str
    capability: str
    message: str
    data: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
    retrieved_at: str
    error: dict | None = None
    retry_safe: bool = False
    side_effects_possible: bool = False


def minimized(value, secrets=(), depth=0):
    """Provider contents are untrusted data. Strip metadata and all credential keys."""
    if depth>5:return None
    if isinstance(value,dict):
        return {k:minimized(v,secrets,depth+1) for k,v in value.items() if k.lower() not in
            {"metadata","etag","access_token","refresh_token","authorization","api_key","client_secret","credentials","token","cookie"}}
    if isinstance(value,list):return [minimized(x,secrets,depth+1) for x in value[:25]]
    if isinstance(value,str):
        for secret in secrets:
            if isinstance(secret,str) and len(secret)>3:value=value.replace(secret,"[redacted]")
        return value[:6000]
    return value if isinstance(value,(int,float,bool)) or value is None else None


class IntegrationRegistry:
    _instance=None

    def __init__(self):
        self.capabilities={c.capability_id:c for c in definitions()}
        self._argument_schemas={c.capability_id:c.argument_schema() for c in self.capabilities.values()}
        self._result_schemas={c.capability_id:self.result_schema(c) for c in self.capabilities.values()}
        self.oauth_configured=bool(_get_client_id() and _get_client_secret())
        self.providers={pid:GoogleAPI(pid,service) for pid,(service,_) in SERVICES.items()}
        self.connections={}
        self.health={pid:{"state":"unknown","cooldown_until":0,"last_error":None,"rate_limit":{}} for pid in self.providers}
        self.recent=deque(maxlen=64)
        self._approval_bindings={}

    @classmethod
    def get_instance(cls):
        if cls._instance is None:cls._instance=cls()
        return cls._instance

    def register(self, provider_id, adapter):
        if provider_id not in SERVICES:raise ValueError("Unknown integration provider")
        self.providers[provider_id]=adapter

    def unregister(self, provider_id):self.providers.pop(provider_id,None)

    def get(self, capability):return self.capabilities.get(capability)

    async def sync_connections(self):
        from ..database import get_integrations
        rows=await get_integrations();connections={}
        self.oauth_configured=bool(_get_client_id() and _get_client_secret())
        for row in rows:
            pid=next((pid for pid,(service,_) in SERVICES.items() if service==row.get("plugin_name")),None)
            if not pid:continue
            try:
                key=account_key(row["plugin_name"],row["account_id"])
                tokens=CredentialStore.get_tokens(key)
                config=json.loads(row.get("config") or "{}")
                state=AuthState.REVOKED if row.get("status")=="revoked" else token_state(tokens)
                connections[(pid,row["account_id"])]=ProviderConnection(pid,row["account_id"],state,
                    tuple((tokens or {}).get("scopes",[])),(tokens or {}).get("identity",{}),
                    config.get("default") is True,config.get("last_verified"))
            except (ValueError,TypeError,KeyError):continue
        self.connections=connections

    def accounts(self, provider_id):
        return [c for (p,_),c in self.connections.items() if p==provider_id]

    @staticmethod
    def scopes_satisfy(required, granted):
        effective=set(granted)
        # Known OAuth permission implications; never requested automatically.
        prefix="https://www.googleapis.com/auth/"
        implications={"gmail.modify":["gmail.readonly","gmail.send"],
            "calendar.events":["calendar.events.readonly"],"calendar.readonly":["calendar.events.readonly"],
            "drive":["drive.readonly","drive.metadata.readonly","drive.file"],"drive.readonly":["drive.metadata.readonly"]}
        for broad,narrow in implications.items():
            if prefix+broad in effective:effective.update(prefix+n for n in narrow)
        return set(required).issubset(effective)

    def select_account(self, capability, account_id=None):
        pid=capability.provider_id
        if pid in {"google_places","google_geocoding","google_routes"}:
            if not os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("CORTEX_GOOGLE_MAPS_ENABLED")!="1":raise IntegrationError("AUTH_REQUIRED",diagnostic="UNCONFIGURED")
            if account_id not in (None,"project"):raise IntegrationError("AUTH_REQUIRED")
            return ProviderConnection(pid,"project",AuthState.AUTHORIZED)
        accounts=self.accounts(pid)
        if account_id:
            candidate=next((c for c in accounts if c.account_id==account_id),None)
            if not candidate:raise IntegrationError("AUTH_REQUIRED")
        else:
            usable=[c for c in accounts if c.auth_state in (AuthState.AUTHORIZED,AuthState.EXPIRED)]
            defaults=[c for c in usable if c.default]
            if len(defaults)==1:candidate=defaults[0]
            elif len(usable)==1:candidate=usable[0]
            elif len(usable)>1:raise IntegrationError("INVALID_ARGUMENT",diagnostic="ACCOUNT_SELECTION_REQUIRED")
            else:raise IntegrationError("AUTH_REQUIRED")
        if candidate.auth_state not in (AuthState.AUTHORIZED,AuthState.EXPIRED):raise IntegrationError("AUTH_REQUIRED",diagnostic=candidate.auth_state.value)
        if not self.scopes_satisfy(capability.requirement.scopes,candidate.scopes):raise IntegrationError("SCOPE_MISSING")
        return candidate

    def capability_status(self, cap):
        status={"integration":True,"provider_id":cap.provider_id,"provider":cap.provider_id,"auth_state":AuthState.UNAUTHORIZED.value,
            "available":False,"required_scopes":list(cap.requirement.scopes),"transport":"structured_api",
            "preference":100,"ui_fallback_eligible":True,"health":dict(self.health.get(cap.provider_id,{}))}
        if cap.provider_id not in self.providers:
            return dict(status,auth_state="UNCONFIGURED")
        try:
            account=self.select_account(cap)
            active=self.health[cap.provider_id]
            # OAuth rejection belongs to the affected account, not every account
            # on the same service. sync_connections carries each account's state.
            if active["cooldown_until"]>time.time():raise IntegrationError("RATE_LIMITED")
            status.update(auth_state=account.auth_state.value,available=True,ui_fallback_eligible=False)
        except IntegrationError as exc:
            status["reason"]=exc.category
            eligible=[c for c in self.accounts(cap.provider_id) if c.auth_state in (AuthState.AUTHORIZED,AuthState.EXPIRED)
                and self.scopes_satisfy(cap.requirement.scopes,c.scopes)]
            if eligible and (exc.category=="SCOPE_MISSING" or exc.diagnostic=="ACCOUNT_SELECTION_REQUIRED"):
                status.update(available=True,auth_state="AUTHORIZED",account_selection_required=True,
                    ui_fallback_eligible=False,reason="EXPLICIT_ACCOUNT_REQUIRED")
                if self.health[cap.provider_id]["cooldown_until"]>time.time():
                    status.update(available=False,ui_fallback_eligible=True,reason="PROVIDER_UNAVAILABLE")
                return status
            if exc.category=="SCOPE_MISSING":status["auth_state"]=AuthState.SCOPE_MISSING.value
            elif exc.diagnostic in ("UNCONFIGURED","REVOKED","EXPIRED"):status["auth_state"]=exc.diagnostic
            elif not self.accounts(cap.provider_id) and not self.oauth_configured:status["auth_state"]="UNCONFIGURED"
            elif self.accounts(cap.provider_id) and all(c.auth_state==AuthState.REVOKED for c in self.accounts(cap.provider_id)):
                status["auth_state"]="REVOKED"
        return status

    def canonical_capabilities(self):
        from ..capability_registry import RegisteredCapability
        rows=[]
        for cap in self.capabilities.values():
            props=self._argument_schemas[cap.capability_id];required=props.get("required",[])
            args={k:dict(v,required=k in required) for k,v in props["properties"].items()}
            status=self.capability_status(cap)
            account_hint=""
            if "account_id" in args and cap.provider_id not in {"google_places","google_geocoding","google_routes"}:
                eligible=[a.account_id for a in self.accounts(cap.provider_id)
                    if a.auth_state in (AuthState.AUTHORIZED,AuthState.EXPIRED)
                    and self.scopes_satisfy(cap.requirement.scopes,a.scopes)]
                selected=None
                try:selected=self.select_account(cap).account_id
                except IntegrationError:pass
                args["account_id"]["description"]="Omit to use the default or only authorized account; otherwise select an eligible account alias."
                args["account_id"]["x-account-selection"]={"implicit_account":selected,"eligible_accounts":eligible,
                    "explicit_required":bool(eligible and selected is None)}
                if selected:
                    args["account_id"]["default"]=selected
                    account_hint="account="+selected+"; "
                elif eligible:account_hint="choose account; "
            rows.append(RegisteredCapability(name=cap.capability_id,
                description="API preferred: "+account_hint+cap.description,
                arguments=args,target_type="hub",risk="low" if cap.effect==Effect.READ else "high",
                classification="read" if cap.effect==Effect.READ else "write",permission_tier=cap.permission_tier,
                availability=status["available"],provider=cap.provider_id,provider_status=status,
                side_effect_class="none" if cap.effect==Effect.READ else cap.effect.value.lower(),
                result_source="integration_api",privacy_metadata={"data_class":cap.data_class,
                    "required_scopes":list(cap.requirement.scopes),"effect":cap.effect.value,
                    "result_schema":self._result_schemas[cap.capability_id]}))
        return rows

    @staticmethod
    def result_schema(cap):
        result=NormalizedResult.model_json_schema()
        result["properties"]["data"]={"type":"object","additionalProperties":False,"properties":{
            "items":{"type":"array","maxItems":25,"items":{"type":"object","additionalProperties":False,
                "properties":{key:{} for key in cap.result_fields}}},"data_class":{"const":cap.data_class},
                "coverage":{"const":"bounded_projection"},"bounds":{"type":"object"}}}
        return result

    def preference(self, capability_id, ui_candidates=()):
        """A semantic need supplies IDs; selection uses availability/transport metadata."""
        cap=self.get(capability_id)
        if cap and self.capability_status(cap)["available"]:
            return {"preferred":capability_id,"transport":"structured_api","ui_fallback_eligible":False}
        return {"preferred":next(iter(ui_candidates),None),"transport":"ui","ui_fallback_eligible":True}

    async def statuses(self):
        await self.sync_connections()
        result=[]
        for pid,(service,label) in SERVICES.items():
            caps=[c for c in self.capabilities.values() if c.provider_id==pid]
            states=[self.capability_status(c) for c in caps]
            accounts=self.accounts(pid)
            result.append({"integration_id":pid,"key":service,"name":label,"provider":"google",
                "provider_id":pid,"connected":any(c.auth_state==AuthState.AUTHORIZED for c in accounts),
                "status":"connected" if any(c.auth_state==AuthState.AUTHORIZED for c in accounts) else "not_connected",
                "auth_state":states[0]["auth_state"],"scopes":sorted(set(s for c in accounts for s in c.scopes)),
                "accounts":[{"account_id":c.account_id,"auth_state":c.auth_state.value,"scopes":list(c.scopes),
                    "identity":{k:v for k,v in c.identity.items() if k in ("sub","email")},"default":c.default,
                    "last_verified":c.last_verified} for c in accounts],
                "health":dict(self.health[pid]),"capabilities":[{"id":c.capability_id,**s} for c,s in zip(caps,states)],
                "recent_actions":[r for r in self.recent if r["provider"]==pid]})
        return result

    async def set_default(self, provider_id, account_id):
        from ..database import get_integrations,update_integration
        if provider_id not in SERVICES:raise IntegrationError("NOT_FOUND")
        rows=[r for r in await get_integrations() if r["plugin_name"]==SERVICES[provider_id][0]]
        if not any(r["account_id"]==account_id for r in rows):raise IntegrationError("NOT_FOUND")
        for row in rows:
            config=json.loads(row.get("config") or "{}");config["default"]=row["account_id"]==account_id
            await update_integration(row["id"],{"config":config})
        await self.sync_connections()

    @staticmethod
    def _binding(capability, account, args, source):
        identity={"provider":account.provider_id,"account":account.account_id,"subject":account.identity.get("sub")}
        return hashlib.sha256(json.dumps([capability,identity,args,source],sort_keys=True,separators=(",",":")).encode()).hexdigest()

    def bind_approval(self, command_id, capability, args, source):
        cap=self.get(capability)
        if not cap:return
        account=self.select_account(cap,args.get("account_id"))
        canonical=cap.arguments.model_validate(args).model_dump()
        self._approval_bindings[command_id]=self._binding(capability,account,canonical,source)
        while len(self._approval_bindings)>128:self._approval_bindings.pop(next(iter(self._approval_bindings)))

    async def _consume_approval(self, cap, account, args, source):
        from ..database import get_confirmation
        wanted=self._binding(cap.capability_id,account,args,source)
        for command_id,binding in list(self._approval_bindings.items()):
            if binding!=wanted:continue
            row=await get_confirmation(command_id)
            if (row and row["status"] in ("approved","approve") and row.get("resolved_by")==source
                and row.get("requested_by")==source and row.get("command")==cap.capability_id):
                if self._approval_bindings.get(command_id)!=wanted:continue
                self._approval_bindings.pop(command_id)
                return True
        return False

    async def execute(self, capability, arguments, source, context=None):
        cap=self.get(capability);account=None;dispatched=False
        provider=cap.provider_id if cap else "unknown"
        now=datetime.now(timezone.utc).isoformat()
        try:
            if not cap or provider not in self.providers:raise IntegrationError("NOT_FOUND")
            args=cap.arguments.model_validate(arguments).model_dump()
            from ..database import get_device
            from ..permissions import check_permission
            caller=await get_device(source)
            if not caller:raise IntegrationError("PERMISSION_DENIED")
            permission=await check_permission(caller["role"],source,source,capability)
            if not permission.allowed:raise IntegrationError("PERMISSION_DENIED")
            from ..goals import GoalManager
            if not await GoalManager.get_instance().is_autonomy_enabled():raise IntegrationError("PERMISSION_DENIED",diagnostic="KILL_SWITCH")
            await self.sync_connections()
            account=self.select_account(cap,args.get("account_id"))
            health=self.health[provider]
            if health["cooldown_until"]>time.time():raise IntegrationError("RATE_LIMITED")
            if ((context or {}).get("privacy_class")=="HIGHLY_SENSITIVE" or
                (context or {}).get("preferred_execution_scope") in ("local_only","private_compute")):
                raise IntegrationError("PERMISSION_DENIED",diagnostic="PRIVACY_POLICY")
            from ..privacy import classify_task_privacy, PrivacyPolicyEngine
            from ..device_handle import DeviceHandle
            from ..config import get_hub_device_id
            locality=os.getenv("CORTEX_HOST_LOCALITY","local_device")
            local=locality in ("local_device","private_server")
            candidate=DeviceHandle(device_id=get_hub_device_id(),display_name="Integration execution host",
                device_type="desktop" if local else "cloud_worker",trust_level="trusted",locality=locality,
                private_compute_allowed=local,cloud_processing_allowed=not local)
            policy=PrivacyPolicyEngine.get_instance().evaluate_candidate(candidate,
                classify_task_privacy(capability,args,context or {}),capability)
            if not policy.is_allowed:raise IntegrationError("PERMISSION_DENIED",diagnostic="PRIVACY_POLICY")
            if cap.effect!=Effect.READ and not await self._consume_approval(cap,account,args,source):
                raise IntegrationError("APPROVAL_REQUIRED")
            dispatched=True
            items=await self.providers[provider].invoke(cap,account,args)
            if not isinstance(items,list) or any(not isinstance(row,dict) for row in items):
                raise IntegrationError("PROVIDER_UNAVAILABLE",diagnostic="INVALID_RESPONSE")
            if cap.effect!=Effect.READ and (not items or not isinstance(items[0].get("id"),str) or not items[0]["id"]):
                raise IntegrationError("PROVIDER_UNAVAILABLE",diagnostic="UNVERIFIED_RESPONSE")
            # Tokens remain inside the adapter/store, never in traces or model-visible results.
            service=SERVICES[provider][0]
            secrets=CredentialStore.get_tokens(account_key(service,account.account_id)) or {}
            hidden=[secrets.get(k) for k in ("access_token","refresh_token")]+[os.getenv("GOOGLE_MAPS_API_KEY"),_get_client_secret()]
            items=minimized([{k:v for k,v in row.items() if k in cap.result_fields} for row in items[:25]],hidden)
            now=datetime.now(timezone.utc).isoformat()
            self.health[provider].update(state="healthy",last_error=None,rate_limit=getattr(self.providers[provider],"rate_limit",{}))
            provenance={"provider":provider,"account":account.account_id,"capability":capability,"retrieved_at":now,
                "resource_ids":[i.get("id") or i.get("resourceName") or i.get("place_id") for i in items if i.get("id") or i.get("resourceName") or i.get("place_id")]}
            result=NormalizedResult(status="success",provider=provider,capability=capability,message="Integration operation completed.",
                data={"items":items,"data_class":cap.data_class,"coverage":"bounded_projection",
                      "bounds":{"items":args.get("max_results",25),"text_chars":6000,"pagination":"first_page_only"}},
                provenance=provenance,retrieved_at=now)
        except Exception as exc:
            now=datetime.now(timezone.utc).isoformat()
            category=exc.category if isinstance(exc,IntegrationError) else "INVALID_ARGUMENT" if isinstance(exc,ValidationError) else "PROVIDER_UNAVAILABLE" if dispatched else "INTERNAL_ERROR"
            status="needs_approval" if category=="APPROVAL_REQUIRED" else "failure"
            result=NormalizedResult(status=status,provider=provider,capability=capability,message="Integration operation unavailable: "+category+".",
                error={"category":category,"http_status":getattr(exc,"http_status",None),"diagnostic":getattr(exc,"diagnostic",None)},retrieved_at=now,
                side_effects_possible=bool(dispatched and cap and cap.effect!=Effect.READ),retry_safe=bool(cap and cap.effect==Effect.READ))
            if provider in self.health and (dispatched or category=="AUTH_REQUIRED") and category not in ("INVALID_ARGUMENT","SCOPE_MISSING","APPROVAL_REQUIRED"):
                health=self.health[provider]
                health.update(last_error=category,state={"AUTH_REQUIRED":"auth_required","RATE_LIMITED":"rate_limited",
                    "PROVIDER_UNAVAILABLE":"unavailable","NETWORK_ERROR":"unavailable","PERMISSION_DENIED":"degraded"}.get(category,"degraded"))
                if category in ("PROVIDER_UNAVAILABLE","NETWORK_ERROR"):
                    health["cooldown_until"]=time.time()+30
                if category=="RATE_LIMITED":
                    limits=getattr(self.providers.get(provider),"rate_limit",{});health["rate_limit"]=limits
                    try:delay=min(3600,max(1,float(limits.get("retry-after",30))))
                    except (TypeError,ValueError):delay=30
                    health["cooldown_until"]=time.time()+delay
                if category=="AUTH_REQUIRED" and getattr(exc,"http_status",None)==401 and account:
                    key=account_key(SERVICES[provider][0],account.account_id)
                    tokens=CredentialStore.get_tokens(key) or {}
                    tokens["authorization_required"]=True
                    CredentialStore.store_tokens(key,tokens)
            if category in ("AUTH_REQUIRED","SCOPE_MISSING"):
                await self.sync_connections()
        self.recent.append({"provider":provider,"account":account.account_id if account else None,
            "capability":capability,"status":result.status,"error_category":result.error.get("category") if result.error else None,"retrieved_at":now})
        return result.model_dump(exclude_none=True)
