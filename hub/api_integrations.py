"""
hub/api_integrations.py
-----------------------
REST API for managing integrations.
"""

from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Cookie
from pydantic import BaseModel
from .database import get_integrations, add_integration, update_integration


def _auth_wrapper(*args, **kwargs):
    from hub.main import _auth_wrapper
    return _auth_wrapper(*args, **kwargs)

router = APIRouter(prefix="/integrations", tags=["integrations"])

@router.get("/")
async def list_integrations(
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    # The integration configs might have non-sensitive data, but we omit credentials as they are in keyring.
    return {"integrations": await get_integrations()}

from fastapi import Request
from .integrations.gmail import GmailIntegration
from .integrations.calendar import CalendarIntegration

integration_plugins = {
    "gmail": GmailIntegration(),
    "calendar": CalendarIntegration()
}

_OAUTH_SESSIONS = {}

@router.get("/{plugin_name}/auth")
async def start_oauth(
    plugin_name: str,
    account_id: str = "primary",
    x_cortex_secret: str = Header(None, alias="X-Cortex-Secret"),
    x_cortex_device: str = Header(None, alias="X-Cortex-Device"),
    cortex_session: str = Cookie(None),
):
    await _auth_wrapper(x_cortex_device, x_cortex_secret, cortex_session)
    plugin = integration_plugins.get(plugin_name)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
        
    url_info = await plugin.get_authorization_url(account_id, "http://localhost:8000/integrations/callback")
    _OAUTH_SESSIONS[url_info["state"]] = {
        "plugin_name": plugin_name,
        "account_id": account_id,
        "code_verifier": url_info["code_verifier"]
    }
    return {"url": url_info["url"]}

from fastapi.responses import RedirectResponse

@router.get("/{plugin_name}/connect")
async def connect_oauth_browser(
    plugin_name: str,
    account_id: str = "primary",
):
    """Direct browser entrypoint for OAuth authorization (redirects immediately to Google consent)."""
    plugin = integration_plugins.get(plugin_name)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
        
    url_info = await plugin.get_authorization_url(account_id, "http://localhost:8000/integrations/callback")
    _OAUTH_SESSIONS[url_info["state"]] = {
        "plugin_name": plugin_name,
        "account_id": account_id,
        "code_verifier": url_info["code_verifier"]
    }
    return RedirectResponse(url_info["url"])

@router.get("/callback")
async def oauth_callback(state: str, code: str):
    session = _OAUTH_SESSIONS.get(state)
    if not session:
        raise HTTPException(status_code=400, detail="Invalid state parameter")
        
    plugin = integration_plugins.get(session["plugin_name"])
    if not plugin:
        raise HTTPException(status_code=400, detail="Invalid plugin in session")
        
    success = await plugin.exchange_code(
        session["account_id"], 
        code, 
        "http://localhost:8000/integrations/callback", 
        {"code_verifier": session["code_verifier"]}
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="OAuth code exchange failed")
        
    del _OAUTH_SESSIONS[state]
    return RedirectResponse("/dashboard#integrations")
