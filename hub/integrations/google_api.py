"""Structured Google service adapters. Fixed origins; bounded, minimized reads."""
from abc import ABC, abstractmethod
import base64
from email.message import EmailMessage
from email.utils import parseaddr
import json
import logging
import os
from urllib.parse import quote
import httpx
from .google_auth import GoogleOAuth, IntegrationError


class _PrivateServiceURLs(logging.Filter):
    def filter(self,record):
        if isinstance(record.args,tuple) and len(record.args)>1:
            url=record.args[1]
            if isinstance(url,httpx.URL) and url.host in {"gmail.googleapis.com","www.googleapis.com",
                "people.googleapis.com","places.googleapis.com","geocode.googleapis.com","routes.googleapis.com"}:
                args=list(record.args);args[1]=url.scheme+"://"+url.host+"/[integration-resource]"
                record.args=tuple(args)
        return True


logging.getLogger("httpx").addFilter(_PrivateServiceURLs())


class IntegrationProvider(ABC):
    @abstractmethod
    async def invoke(self, capability, account, arguments): ...


def http_error(status):
    return {400:"INVALID_ARGUMENT",401:"AUTH_REQUIRED",403:"PERMISSION_DENIED",404:"NOT_FOUND",
            409:"CONFLICT",412:"CONFLICT",429:"RATE_LIMITED"}.get(status,"PROVIDER_UNAVAILABLE")


def _plain_body(payload, depth=0):
    if depth>5:return None
    if payload.get("mimeType")=="text/plain":
        encoded=payload.get("body",{}).get("data","")
        try: return base64.urlsafe_b64decode(encoded[:28000]+"="*((-len(encoded))%4)).decode("utf-8",errors="replace")[:6000]
        except (ValueError,TypeError): return None
    for part in payload.get("parts",[])[:15]:
        value=_plain_body(part,depth+1)
        if value is not None:return value
    return None


class GoogleAPI(IntegrationProvider):
    def __init__(self, provider_id, service, transport=None):
        self.provider_id,self.service,self.transport=provider_id,service,transport
        self.oauth=GoogleOAuth(service,transport=transport)
        self.rate_limit={}

    async def _request(self, method, url, **kwargs):
        try:
            async with httpx.AsyncClient(transport=self.transport,timeout=20,follow_redirects=False,trust_env=False) as client:
                async with client.stream(method,url,**kwargs) as response:
                    self.rate_limit={k:response.headers[k] for k in ("retry-after","x-ratelimit-limit","x-ratelimit-remaining") if k in response.headers}
                    if not 200<=response.status_code<300:
                        raise IntegrationError(http_error(response.status_code),http_status=response.status_code)
                    chunks=[];size=0
                    async for chunk in response.aiter_bytes():
                        size+=len(chunk)
                        if size>512000: raise IntegrationError("PROVIDER_UNAVAILABLE",diagnostic="RESPONSE_TOO_LARGE")
                        chunks.append(chunk)
                    body=b"".join(chunks)
                    try:
                        parsed=json.loads(body) if body else {}
                        if not isinstance(parsed,dict) or "error" in parsed:raise ValueError()
                        return parsed
                    except (ValueError,UnicodeError): raise IntegrationError("PROVIDER_UNAVAILABLE",diagnostic="INVALID_RESPONSE") from None
        except httpx.HTTPError: raise IntegrationError("NETWORK_ERROR") from None

    async def invoke(self, capability, account, a):
        cid=capability.capability_id
        maps=self.service in {"places","geocoding","routes"}
        token=os.getenv("GOOGLE_MAPS_API_KEY","") if maps else await self.oauth.get_valid_token(account.account_id)
        if not token: raise IntegrationError("AUTH_REQUIRED")
        if not maps:
            from .ecosystem import IntegrationRegistry
            from ..credentials import CredentialStore
            from .google_auth import account_key
            granted=(CredentialStore.get_tokens(account_key(self.service,account.account_id)) or {}).get("scopes",[])
            if not IntegrationRegistry.scopes_satisfy(capability.requirement.scopes,granted):raise IntegrationError("SCOPE_MISSING")
        headers={"X-Goog-Api-Key":token} if maps else {"Authorization":"Bearer "+token}
        q=lambda value:quote(value,safe="")
        request=lambda method,url,**kw:self._request(method,url,headers=headers,**kw)
        if cid=="email.search":
            data=await request("GET","https://gmail.googleapis.com/gmail/v1/users/me/messages",params={"q":a["query"],"maxResults":a["max_results"],"fields":"messages(id,threadId),nextPageToken"})
            items=data.get("messages",[])
        elif cid=="email.read":
            data=await request("GET","https://gmail.googleapis.com/gmail/v1/users/me/messages/"+q(a["message_id"]),params={"format":"full" if a["include_body"] else "metadata","metadataHeaders":["Subject","From","To","Date"]})
            if not isinstance(data.get("id"),str) or not data["id"]:
                raise IntegrationError("PROVIDER_UNAVAILABLE",diagnostic="INVALID_RESPONSE")
            item={"id":data["id"]};payload=data.get("payload",{})
            item.update({h["name"].lower():h.get("value","") for h in payload.get("headers",[]) if h.get("name","").lower() in {"subject","from","to","date"}})
            if a["include_body"]:
                item["body"]=_plain_body(payload)
                item["body_available"]=item["body"] is not None
            items=[item]
        elif cid=="email.send":
            if not parseaddr(a["to"])[1] or any(c in a["to"]+a["subject"] for c in "\r\n"): raise IntegrationError("INVALID_ARGUMENT")
            mail=EmailMessage();mail["To"]=a["to"];mail["Subject"]=a["subject"];mail.set_content(a["body"])
            items=[await request("POST","https://gmail.googleapis.com/gmail/v1/users/me/messages/send",json={"raw":base64.urlsafe_b64encode(mail.as_bytes()).decode()})]
        elif cid.startswith("calendar."):
            base="https://www.googleapis.com/calendar/v3/calendars/"+q(a["calendar_id"])+"/events"
            if cid=="calendar.list":
                data=await request("GET",base,params={"timeMin":a["time_min"],"timeMax":a["time_max"],"singleEvents":"true","orderBy":"startTime","maxResults":a["max_results"],"fields":"items(id,summary,start,end,status)"})
                items=data.get("items",[])
            else:
                body={"summary":a["summary"],"start":{"dateTime":a["start"],"timeZone":a["time_zone"]},"end":{"dateTime":a["end"],"timeZone":a["time_zone"]}}
                items=[await request("PATCH" if cid=="calendar.update" else "POST",base+("/"+q(a["event_id"]) if cid=="calendar.update" else ""),json=body)]
        elif cid=="contacts.search":
            base="https://people.googleapis.com/v1/people:searchContacts"
            # Google requires a cache warm-up before searching contacts.
            await request("GET",base,params={"query":"","readMask":"names,emailAddresses","pageSize":1})
            data=await request("GET",base,params={"query":a["query"],"readMask":"names,emailAddresses","pageSize":a["max_results"]})
            items=[{k:v for k,v in r.get("person",{}).items() if k in capability.result_fields} for r in data.get("results",[])]
        elif cid=="drive.search":
            data=await request("GET","https://www.googleapis.com/drive/v3/files",params={"q":a["query"],"pageSize":a["max_results"],"fields":"files(id,name,mimeType,modifiedTime)"})
            items=data.get("files",[])
        elif cid=="drive.read":
            base="https://www.googleapis.com/drive/v3/files/"+q(a["file_id"])
            item=await request("GET",base,params={"fields":"id,name,mimeType,size"})
            if a["include_content"]:
                # No binary downloads, arbitrary exports, local paths or attachment execution.
                if item.get("mimeType")!="text/plain": raise IntegrationError("INVALID_ARGUMENT",diagnostic="PLAIN_TEXT_ONLY")
                if int(item.get("size",0))>20000: raise IntegrationError("INVALID_ARGUMENT",diagnostic="CONTENT_TOO_LARGE")
                try:
                    async with httpx.AsyncClient(transport=self.transport,timeout=20,follow_redirects=False,trust_env=False) as client:
                        async with client.stream("GET",base,params={"alt":"media"},headers=headers) as response:
                            if response.status_code!=200: raise IntegrationError(http_error(response.status_code),http_status=response.status_code)
                            raw=b""
                            async for chunk in response.aiter_bytes():
                                raw+=chunk
                                if len(raw)>20000: raise IntegrationError("INVALID_ARGUMENT",diagnostic="CONTENT_TOO_LARGE")
                    item["content"]=raw.decode("utf-8",errors="replace")[:6000]
                except httpx.HTTPError: raise IntegrationError("NETWORK_ERROR") from None
            items=[item]
        elif cid=="drive.upload":
            import secrets
            boundary="cortex_"+secrets.token_hex(16)
            body=("--"+boundary+"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"+json.dumps({"name":a["name"],"mimeType":"text/plain"})+"\r\n--"+boundary+"\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n"+a["content"]+"\r\n--"+boundary+"--\r\n").encode()
            headers["Content-Type"]="multipart/related; boundary="+boundary
            items=[await request("POST","https://www.googleapis.com/upload/drive/v3/files",params={"uploadType":"multipart","fields":"id,name,mimeType"},content=body)]
        elif cid=="maps.geocode":
            headers["X-Goog-FieldMask"]="results.placeId,results.formattedAddress,results.location"
            data=await request("GET","https://geocode.googleapis.com/v4/geocode/address/"+q(a["address"]))
            items=[{"place_id":r.get("placeId"),"formatted_address":r.get("formattedAddress"),"location":r.get("location")} for r in data.get("results",[])]
        elif cid=="maps.place_search":
            headers["X-Goog-FieldMask"]="places.id,places.displayName,places.formattedAddress,places.location"
            data=await request("POST","https://places.googleapis.com/v1/places:searchText",json={"textQuery":a["query"],"pageSize":min(a["max_results"],20)})
            items=data.get("places",[])
        elif cid=="routes.compute":
            headers["X-Goog-FieldMask"]="routes.distanceMeters,routes.duration"
            data=await request("POST","https://routes.googleapis.com/directions/v2:computeRoutes",json={"origin":{"address":a["origin"]},"destination":{"address":a["destination"]},"travelMode":a["travel_mode"]})
            items=data.get("routes",[])
        else: raise IntegrationError("INVALID_ARGUMENT")
        # Only explicitly declared fields cross the adapter boundary; never raw bodies.
        return [{k:v for k,v in row.items() if k in capability.result_fields} for row in items[:a.get("max_results",10)]]
