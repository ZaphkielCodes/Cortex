"""Scoped Google OAuth using existing PKCE helpers and OS credential storage."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlencode
import httpx

from .base import IntegrationPlugin
from .calendar import generate_pkce, _get_client_id, _get_client_secret
from .definitions import AuthState
from ..credentials import CredentialStore


class IntegrationError(Exception):
    def __init__(self, category, *, http_status=None, diagnostic=None):
        self.category,self.http_status,self.diagnostic=category,http_status,diagnostic
        super().__init__(category)


def account_key(service, account):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",account):
        raise IntegrationError("INVALID_ARGUMENT")
    return service+"_"+account


def token_state(tokens):
    if not tokens: return AuthState.UNAUTHORIZED
    if tokens.get("revoked"): return AuthState.REVOKED
    if tokens.get("authorization_required"):return AuthState.UNAUTHORIZED
    try:
        expires=datetime.fromisoformat(tokens.get("expires_at", ""))
        if expires.tzinfo is None: return AuthState.EXPIRED
        if expires <= datetime.now(timezone.utc): return AuthState.EXPIRED
    except (ValueError,TypeError): return AuthState.EXPIRED
    return AuthState.AUTHORIZED if tokens.get("access_token") else AuthState.UNAUTHORIZED


class GoogleOAuth(IntegrationPlugin):
    def __init__(self, service, scopes=(), transport=None):
        self.service,self.scopes,self.transport=service,tuple(scopes),transport
        self._locks={}

    @property
    def name(self): return self.service

    async def get_authorization_url(self, account_id, redirect_uri):
        account_key(self.service,account_id)
        if not _get_client_id() or not _get_client_secret(): raise IntegrationError("AUTH_REQUIRED")
        verifier,challenge=generate_pkce()
        import secrets
        state=secrets.token_urlsafe(32)
        scopes=sorted(set(self.scopes)|{"openid","email"})
        return {"url":"https://accounts.google.com/o/oauth2/v2/auth?"+urlencode({
            "client_id":_get_client_id(),"redirect_uri":redirect_uri,"response_type":"code",
            "scope":" ".join(scopes),"state":state,"access_type":"offline","prompt":"consent",
            "code_challenge":challenge,"code_challenge_method":"S256"}),
            "state":state,"code_verifier":verifier}

    async def _post_token(self, data):
        try:
            async with httpx.AsyncClient(transport=self.transport,timeout=20,follow_redirects=False,trust_env=False) as client:
                response=await client.post("https://oauth2.googleapis.com/token",data=data)
        except httpx.HTTPError: raise IntegrationError("NETWORK_ERROR") from None
        if response.status_code != 200:
            # Never retain a provider body: OAuth errors may echo credentials.
            revoked=False
            try: revoked=response.json().get("error")=="invalid_grant"
            except (ValueError,AttributeError): pass
            raise IntegrationError("AUTH_REQUIRED" if response.status_code in (400,401) else "PROVIDER_UNAVAILABLE",
                                   http_status=response.status_code,diagnostic="REVOKED" if revoked else None)
        try:
            result=response.json()
            if (not isinstance(result,dict) or not isinstance(result.get("access_token"),str) or
                not result["access_token"] or type(result.get("expires_in")) not in (int,float) or
                not 0<result["expires_in"]<=86400):raise ValueError()
            return result
        except (ValueError,TypeError):raise IntegrationError("PROVIDER_UNAVAILABLE",diagnostic="INVALID_TOKEN_RESPONSE") from None

    async def exchange_code(self, account_id, code, redirect_uri, metadata):
        key=account_key(self.service,account_id)
        data=await self._post_token({"client_id":_get_client_id(),"client_secret":_get_client_secret(),
            "grant_type":"authorization_code","code":code,"redirect_uri":redirect_uri,
            "code_verifier":metadata["code_verifier"]})
        scopes=tuple(data.get("scope","").split())
        if not set(self.scopes).issubset(scopes): raise IntegrationError("SCOPE_MISSING")
        try:
            async with httpx.AsyncClient(transport=self.transport,timeout=20,follow_redirects=False,trust_env=False) as client:
                response=await client.get("https://openidconnect.googleapis.com/v1/userinfo",
                    headers={"Authorization":"Bearer "+data["access_token"]})
        except httpx.HTTPError:raise IntegrationError("NETWORK_ERROR") from None
        if response.status_code!=200: raise IntegrationError("AUTH_REQUIRED",http_status=response.status_code)
        try:user=response.json()
        except (ValueError,UnicodeError):raise IntegrationError("AUTH_REQUIRED",diagnostic="INVALID_IDENTITY_RESPONSE") from None
        if not isinstance(user,dict) or not isinstance(user.get("sub"),str) or not user["sub"] or user.get("email_verified") is not True: raise IntegrationError("AUTH_REQUIRED")
        prior=CredentialStore.get_tokens(key) or {}
        if prior.get("identity",{}).get("sub") not in (None,user["sub"]): raise IntegrationError("CONFLICT")
        tokens={"access_token":data["access_token"],"refresh_token":data.get("refresh_token") or prior.get("refresh_token"),
            "expires_at":(datetime.now(timezone.utc)+timedelta(seconds=data.get("expires_in",3600))).isoformat(),
            "scopes":scopes,"identity":{k:user[k] for k in ("sub","email") if k in user}}
        if not CredentialStore.store_tokens(key,tokens): raise IntegrationError("AUTH_REQUIRED",diagnostic="SECURE_STORAGE_UNAVAILABLE")
        from ..database import get_integrations,add_integration,update_integration
        rows=await get_integrations()
        existing=next((r for r in rows if r["id"]==key),None)
        config=json.loads(existing.get("config") or "{}") if existing else {}
        config.update(identity=tokens["identity"],last_verified=datetime.now(timezone.utc).isoformat())
        if existing: await update_integration(key,{"status":"active","config":config})
        else: await add_integration({"id":key,"plugin_name":self.service,"account_id":account_id,"status":"active","config":config})
        return True

    async def get_valid_token(self, account_id):
        async with self._locks.setdefault(account_id,asyncio.Lock()):
            key=account_key(self.service,account_id);tokens=CredentialStore.get_tokens(key)
            state=token_state(tokens)
            if state==AuthState.REVOKED or not tokens: raise IntegrationError("AUTH_REQUIRED")
            if tokens.get("authorization_required"):raise IntegrationError("AUTH_REQUIRED")
            if state==AuthState.AUTHORIZED: return tokens["access_token"]
            if not tokens.get("refresh_token"): raise IntegrationError("AUTH_REQUIRED",diagnostic="EXPIRED")
            if not _get_client_id() or not _get_client_secret(): raise IntegrationError("AUTH_REQUIRED")
            try:
                data=await self._post_token({"client_id":_get_client_id(),"client_secret":_get_client_secret(),
                    "grant_type":"refresh_token","refresh_token":tokens["refresh_token"]})
            except IntegrationError as exc:
                if exc.diagnostic=="REVOKED":
                    tokens["revoked"]=True;tokens.pop("access_token",None);CredentialStore.store_tokens(key,tokens)
                    from ..database import update_integration
                    await update_integration(key,{"status":"revoked"})
                raise
            tokens.update(access_token=data["access_token"],expires_at=(datetime.now(timezone.utc)+timedelta(seconds=data.get("expires_in",3600))).isoformat())
            if data.get("scope"): tokens["scopes"]=data["scope"].split()
            if data.get("refresh_token"): tokens["refresh_token"]=data["refresh_token"]
            if not CredentialStore.store_tokens(key,tokens): raise IntegrationError("AUTH_REQUIRED")
            return tokens["access_token"]

    async def revoke(self, account_id):
        key=account_key(self.service,account_id);tokens=CredentialStore.get_tokens(key) or {}
        token=tokens.get("refresh_token") or tokens.get("access_token")
        if token:
            async with httpx.AsyncClient(transport=self.transport,timeout=20,trust_env=False) as client:
                response=await client.post("https://oauth2.googleapis.com/revoke",data={"token":token})
            if response.status_code not in (200,400): raise IntegrationError("PROVIDER_UNAVAILABLE")
        if not CredentialStore.delete_tokens(key): raise IntegrationError("AUTH_REQUIRED")
        from ..database import update_integration
        await update_integration(key,{"status":"revoked"})
        return True

    async def execute_action(self, account_id, action, **kwargs):
        raise IntegrationError("PERMISSION_DENIED",diagnostic="USE_CANONICAL_EXECUTION_PATH")
