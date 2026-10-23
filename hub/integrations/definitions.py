"""Service protocol metadata, never natural-language routing rules."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator


class AuthState(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    UNAUTHORIZED = "UNAUTHORIZED"
    AUTHORIZED = "AUTHORIZED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    SCOPE_MISSING = "SCOPE_MISSING"


class Effect(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    SEND = "SEND"
    DELETE = "DELETE"
    SHARE = "SHARE"
    PERMISSION_CHANGE = "PERMISSION_CHANGE"


@dataclass(frozen=True)
class ScopeRequirement:
    scopes: tuple[str, ...] = ()


@dataclass
class ProviderConnection:
    provider_id: str
    account_id: str
    auth_state: AuthState
    scopes: tuple[str, ...] = ()
    identity: dict = field(default_factory=dict)
    default: bool = False
    last_verified: str | None = None


@dataclass(frozen=True)
class IntegrationCapability:
    capability_id: str
    provider_id: str
    description: str
    arguments: type[BaseModel]
    requirement: ScopeRequirement
    effect: Effect = Effect.READ
    data_class: str = "SENSITIVE"
    result_fields: tuple[str, ...] = ("id",)

    @property
    def permission_tier(self): return 1 if self.effect == Effect.READ else 3

    def argument_schema(self): return self.arguments.model_json_schema()


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def valid_calendar_window(self):
        from datetime import datetime
        for first,last in (("time_min","time_max"),("start","end")):
            if hasattr(self,first) and hasattr(self,last):
                a,b=datetime.fromisoformat(getattr(self,first)),datetime.fromisoformat(getattr(self,last))
                if a.tzinfo is None or b.tzinfo is None or a>=b:raise ValueError("A bounded timezone-aware time window is required")
        return self


def schema(model_name, **fields):
    return create_model(model_name, __base__=Arguments, **fields)


Text = (str, Field(min_length=1, max_length=2000))
Time = (str, Field(min_length=1, max_length=80, json_schema_extra={"format":"date-time"},
                   description="RFC3339 timestamp with an explicit timezone offset or Z; end must be after start."))
ID = (str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$"))
Limit = (int, Field(default=10, ge=1, le=25))
G = "https://www.googleapis.com/auth/"
SERVICES = {
    "google_gmail": ("gmail", "Google Gmail"),
    "google_calendar": ("calendar", "Google Calendar"),
    "google_drive": ("drive", "Google Drive"),
    "google_contacts": ("contacts", "Google Contacts"),
    "google_places": ("places", "Google Places"),
    "google_geocoding": ("geocoding", "Google Geocoding"),
    "google_routes": ("routes", "Google Routes"),
}


def definitions():
    def cap(cid, provider, desc, args, scope=(), effect=Effect.READ, fields=("id",), privacy="SENSITIVE"):
        return IntegrationCapability(cid,provider,desc,args,ScopeRequirement(tuple(scope)),effect,privacy,fields)
    return [
        cap("email.search","google_gmail","Search email and return message identifiers",schema("EmailSearch",query=Text,max_results=Limit),[G+"gmail.readonly"],fields=("id","threadId")),
        cap("email.read","google_gmail","Read one email's requested metadata or bounded plain text",schema("EmailRead",message_id=ID,include_body=(bool,False)),[G+"gmail.readonly"],fields=("id","subject","from","to","date","body","body_available")),
        cap("email.send","google_gmail","Send a plain-text email after explicit action approval",schema("EmailSend",to=Text,subject=Text,body=(str,Field(max_length=20000))),[G+"gmail.send"],Effect.SEND,("id","threadId")),
        cap("calendar.list","google_calendar","List events in an explicit bounded time window",schema("CalendarList",time_min=Time,time_max=Time,max_results=Limit,calendar_id=(str,Field(default="primary",max_length=256))),[G+"calendar.events.readonly"],fields=("id","summary","start","end","status"),privacy="PERSONAL"),
        cap("calendar.create","google_calendar","Create a calendar event after explicit action approval",schema("CalendarCreate",summary=Text,start=Time,end=Time,time_zone=Text,calendar_id=(str,"primary")),[G+"calendar.events"],Effect.WRITE,("id","summary","start","end","status"),"PERSONAL"),
        cap("calendar.update","google_calendar","Update specified event fields after explicit action approval",schema("CalendarUpdate",event_id=ID,summary=Text,start=Time,end=Time,time_zone=Text,calendar_id=(str,"primary")),[G+"calendar.events"],Effect.WRITE,("id","summary","start","end","status"),"PERSONAL"),
        cap("contacts.search","google_contacts","Search contacts with minimized names and email fields",schema("ContactSearch",query=Text,max_results=Limit),[G+"contacts.readonly"],fields=("resourceName","names","emailAddresses")),
        cap("drive.search","google_drive","Search Drive file metadata; no file contents",schema("DriveSearch",query=Text,max_results=Limit),[G+"drive.metadata.readonly"],fields=("id","name","mimeType","modifiedTime")),
        cap("drive.read","google_drive","Read file metadata or explicitly requested bounded plain-text content",schema("DriveRead",file_id=ID,include_content=(bool,False)),[G+"drive.readonly"],fields=("id","name","mimeType","size","content")),
        cap("drive.upload","google_drive","Upload supplied plain text to a new Drive file after approval",schema("DriveUpload",name=Text,content=(str,Field(max_length=20000))),[G+"drive.file"],Effect.WRITE,("id","name","mimeType")),
        cap("maps.geocode","google_geocoding","Geocode an address using a configured Maps project",schema("Geocode",address=Text),fields=("place_id","formatted_address","location"),privacy="PERSONAL"),
        cap("maps.place_search","google_places","Search places using a configured Maps project",schema("PlaceSearch",query=Text,max_results=(int,Field(default=10,ge=1,le=20))),fields=("id","displayName","formattedAddress","location"),privacy="PERSONAL"),
        cap("routes.compute","google_routes","Compute a route between explicit addresses",schema("RouteCompute",origin=Text,destination=Text,travel_mode=(str,Field(default="DRIVE",pattern="^(DRIVE|WALK|BICYCLE|TRANSIT)$"))),fields=("distanceMeters","duration"),privacy="PERSONAL"),
    ]
