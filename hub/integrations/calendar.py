"""
hub/integrations/calendar.py
----------------------------
Calendar integration supporting multiple accounts.
Capabilities: calendar.read, calendar.create
"""

import os
import base64
import hashlib
import logging
from typing import Optional, Any
import httpx
from datetime import datetime, timezone, timedelta

from .base import IntegrationPlugin
from ..credentials import CredentialStore
from ..database import update_integration, add_integration, get_integrations

log = logging.getLogger(__name__)

def _get_client_id() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return os.getenv("GOOGLE_CLIENT_ID", "").strip()

def _get_client_secret() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

def _check_oauth_config():
    if not _get_client_id() or not _get_client_secret():
        raise ValueError("Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET in .env file.")

def generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    m = hashlib.sha256()
    m.update(verifier.encode('utf-8'))
    challenge = base64.urlsafe_b64encode(m.digest()).decode('utf-8').rstrip('=')
    return verifier, challenge

class CalendarIntegration(IntegrationPlugin):
    @property
    def name(self) -> str:
        return "calendar"

    def _get_key(self, account_id: str) -> str:
        return f"{self.name}_{account_id}"

    async def get_authorization_url(self, account_id: str, redirect_uri: str) -> dict:
        _check_oauth_config()
        verifier, challenge = generate_pkce()
        state = base64.urlsafe_b64encode(os.urandom(16)).decode('utf-8').rstrip('=')
        
        scopes = [
            "https://www.googleapis.com/auth/calendar.events"
        ]
        
        params = {
            "client_id": _get_client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256"
        }
        
        from urllib.parse import urlencode
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
            
        return {
            "url": url,
            "state": state,
            "code_verifier": verifier
        }

    async def exchange_code(self, account_id: str, code: str, redirect_uri: str, metadata: dict) -> bool:
        _check_oauth_config()
        verifier = metadata.get("code_verifier")
        async with httpx.AsyncClient() as client:
                resp = await client.post("https://oauth2.googleapis.com/token", data={
                    "client_id": _get_client_id(),
                    "client_secret": _get_client_secret(),
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier
                })
                if resp.status_code != 200:
                    log.error("OAuth exchange rejected status=%s", resp.status_code)
                    return False
                data = resp.json()
                expires_at = (datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600))).isoformat()
                tokens = {
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token"),
                    "expires_at": expires_at
                }

        CredentialStore.store_tokens(self._get_key(account_id), tokens)
        
        integration_id = self._get_key(account_id)
        existing = [i for i in await get_integrations() if i["id"] == integration_id]
        if existing:
            await update_integration(integration_id, {"status": "active"})
        else:
            await add_integration({
                "id": integration_id,
                "plugin_name": self.name,
                "account_id": account_id,
                "status": "active"
            })
            
        return True

    async def revoke(self, account_id: str) -> bool:
        token = await self.get_valid_token(account_id)
        if token:
            async with httpx.AsyncClient() as client:
                await client.post("https://oauth2.googleapis.com/revoke", params={"token": token})
                
        CredentialStore.delete_tokens(self._get_key(account_id))
        await update_integration(self._get_key(account_id), {"status": "revoked"})
        return True

    async def get_valid_token(self, account_id: str) -> Optional[str]:
        tokens = CredentialStore.get_tokens(self._get_key(account_id))
        if not tokens:
            return None
            
        expires_at_str = tokens.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now(timezone.utc) < (expires_at - timedelta(minutes=5)):
                return tokens.get("access_token")
                
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return None
            
        _check_oauth_config()
        async with httpx.AsyncClient() as client:
            resp = await client.post("https://oauth2.googleapis.com/token", data={
                "client_id": _get_client_id(),
                "client_secret": _get_client_secret(),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            })
            if resp.status_code != 200:
                return None
            data = resp.json()
            tokens["access_token"] = data["access_token"]
            if "refresh_token" in data:
                tokens["refresh_token"] = data["refresh_token"]
            tokens["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600))).isoformat()
                
        CredentialStore.store_tokens(self._get_key(account_id), tokens)
        return tokens["access_token"]

    async def execute_action(self, account_id: str, action: str, **kwargs) -> Any:
        token = await self.get_valid_token(account_id)
        if not token:
            raise ValueError(f"No valid token for {self.name} account {account_id}")

        headers = {"Authorization": f"Bearer {token}"}

        if action == "calendar.read":
            time_min = kwargs.get("time_min")
            time_max = kwargs.get("time_max")
            params = {
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 250
            }
            if time_min:
                params["timeMin"] = time_min
            if time_max:
                params["timeMax"] = time_max
            async with httpx.AsyncClient() as client:
                resp = await client.get("https://www.googleapis.com/calendar/v3/calendars/primary/events", params=params, headers=headers)
                return resp.json()

        elif action == "calendar.create":
            event_data = kwargs.get("event_data", {})
            async with httpx.AsyncClient() as client:
                resp = await client.post("https://www.googleapis.com/calendar/v3/calendars/primary/events", headers=headers, json=event_data)
                return resp.json()

        else:
            raise NotImplementedError(f"Action {action} not supported")
