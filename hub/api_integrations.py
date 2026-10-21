"""Authenticated integration account management; explicit scoped OAuth consent."""
import logging
import os
import time
from fastapi import APIRouter, Header, HTTPException, Cookie
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from .integrations.ecosystem import IntegrationRegistry
from .integrations.definitions import SERVICES, Effect
from .integrations.google_auth import GoogleOAuth, IntegrationError, account_key

router=APIRouter(prefix="/integrations",tags=["integrations"])
_OAUTH_SESSIONS={}

class _NoOAuthCodes(logging.Filter):
    def filter(self,record):
        return "/integrations/callback" not in record.getMessage()
logging.getLogger("uvicorn.access").addFilter(_NoOAuthCodes())


def _auth_wrapper(*args,**kwargs):
    from hub.main import _auth_wrapper
    return _auth_wrapper(*args,**kwargs)


def _plugin(service, capabilities=None):
    registry=IntegrationRegistry.get_instance()
    pid=next((p for p,(s,_) in SERVICES.items() if s==service),None)
    if not pid or service in {"places","geocoding","routes"}:raise HTTPException(404,"OAuth provider not found")
    caps=[c for c in registry.capabilities.values() if c.provider_id==pid]
    requested=capabilities.split(",") if capabilities else [c.capability_id for c in caps if c.effect==Effect.READ][:1]
    if not requested or any(not registry.get(c) or registry.get(c).provider_id!=pid for c in requested):raise HTTPException(400,"Invalid capability scope selection")
    scopes=set(s for c in requested for s in registry.get(c).requirement.scopes)
    return GoogleOAuth(service,scopes)

# Compatibility names remain; requests create fresh scope-specific plugin instances.
integration_plugins={service:_plugin(service) for service in ("gmail","calendar","drive","contacts")}


@router.get("/")
async def list_integrations(x_cortex_secret: str=Header(None,alias="X-Cortex-Secret"),x_cortex_device: str=Header(None,alias="X-Cortex-Device"),cortex_session: str=Cookie(None)):
    await _auth_wrapper(x_cortex_device,x_cortex_secret,cortex_session)
    return {"integrations":await IntegrationRegistry.get_instance().statuses()}


@router.get("/{plugin_name}/auth")
async def start_oauth(plugin_name: str,account_id: str="primary",capabilities: str|None=None,
    x_cortex_secret: str=Header(None,alias="X-Cortex-Secret"),x_cortex_device: str=Header(None,alias="X-Cortex-Device"),cortex_session: str=Cookie(None)):
    caller=await _auth_wrapper(x_cortex_device,x_cortex_secret,cortex_session)
    if caller.get("role") not in ("admin","primary_admin"):raise HTTPException(403,"Administrator consent required")
    plugin=_plugin(plugin_name,capabilities)
    redirect=os.getenv("CORTEX_GOOGLE_REDIRECT_URI","http://localhost:8000/integrations/callback")
    try:info=await plugin.get_authorization_url(account_id,redirect)
    except IntegrationError as exc:raise HTTPException(409,exc.category) from None
    now=time.time()
    for state,session in list(_OAUTH_SESSIONS.items()):
        if session["expires_at"]<=now:_OAUTH_SESSIONS.pop(state,None)
    if len(_OAUTH_SESSIONS)>=32:raise HTTPException(429,"Too many pending authorization requests")
    _OAUTH_SESSIONS[info["state"]]={"plugin":plugin,"account_id":account_id,"code_verifier":info["code_verifier"],
        "redirect_uri":redirect,"requesting_device":caller["device_id"],"expires_at":now+600}
    return {"url":info["url"],"scopes":list(plugin.scopes),"account_id":account_id}


@router.get("/{plugin_name}/connect")
async def connect_oauth_browser(plugin_name: str,account_id: str="primary",capabilities: str|None=None,
    x_cortex_secret: str=Header(None,alias="X-Cortex-Secret"),x_cortex_device: str=Header(None,alias="X-Cortex-Device"),cortex_session: str=Cookie(None)):
    info=await start_oauth(plugin_name,account_id,capabilities,x_cortex_secret,x_cortex_device,cortex_session)
    return RedirectResponse(info["url"])


@router.get("/callback")
async def oauth_callback(state: str,code: str|None=None,error: str|None=None):
    session=_OAUTH_SESSIONS.pop(state,None)
    if not session or session["expires_at"]<=time.time():raise HTTPException(400,"Invalid or expired OAuth state")
    if error or not code:raise HTTPException(400,"Authorization was not completed")
    try:
        await session["plugin"].exchange_code(session["account_id"],code,session["redirect_uri"],{"code_verifier":session["code_verifier"]})
    except IntegrationError as exc:raise HTTPException(409,exc.category) from None
    registry=IntegrationRegistry.get_instance()
    await registry.sync_connections()
    pid=next(p for p,(service,_) in SERVICES.items() if service==session["plugin"].service)
    registry.health[pid].update(state="unknown",last_error=None,cooldown_until=0)
    return RedirectResponse("/dashboard#integrations")


class AccountSelection(BaseModel):
    model_config=ConfigDict(extra="forbid")
    provider_id: str
    account_id: str=Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


@router.post("/default-account")
async def default_account(selection: AccountSelection,x_cortex_secret: str=Header(None,alias="X-Cortex-Secret"),x_cortex_device: str=Header(None,alias="X-Cortex-Device"),cortex_session: str=Cookie(None)):
    caller=await _auth_wrapper(x_cortex_device,x_cortex_secret,cortex_session)
    if caller.get("role") not in ("admin","primary_admin"):raise HTTPException(403,"Administrator required")
    try:await IntegrationRegistry.get_instance().set_default(selection.provider_id,selection.account_id)
    except IntegrationError as exc:raise HTTPException(409,exc.category) from None
    return {"status":"success"}
