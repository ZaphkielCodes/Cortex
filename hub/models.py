"""
hub/models.py
-------------
Pydantic models for every message type that flows between the hub and agents.

Message types:
  Hub → Agent:
    - CommandMessage       : hub sends a command to execute
    - AuthChallenge        : hub sends a nonce for authentication

  Agent → Hub:
    - ResultMessage        : agent reports execution outcome
    - StatusMessage        : heartbeat / presence signal
    - AuthResponse         : agent proves identity via HMAC

  Hub → CLI:
    - ConfirmationRequest  : hub asks admin to approve a sensitive action

  CLI → Hub:
    - ConfirmationResponse : admin approves or denies
"""

from typing import Any, Dict, List, Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Hub → Agent
# ---------------------------------------------------------------------------

class CommandMessage(BaseModel):
    """Sent by the hub to an agent instructing it to run a command."""
    type: Literal["command"] = "command"
    command_id: str = Field(default_factory=lambda: uuid4().hex)
    command: str          # e.g. "take_screenshot", "open_app", "move_file"
    args: Union[Dict[str, Any], List[Any]] = Field(default_factory=dict)
    latency_metrics: dict = Field(default_factory=dict)


class AuthChallenge(BaseModel):
    """Sent by the hub immediately after WebSocket accept.
    Agent must respond with an AuthResponse within 10 seconds."""
    type: Literal["auth_challenge"] = "auth_challenge"
    challenge: str  # random hex nonce


class AuthResult(BaseModel):
    """Sent by the hub after verifying the agent's AuthResponse."""
    type: Literal["auth_result"] = "auth_result"
    success: bool
    message: str = ""
    role: Optional[str] = None  # Phase 3: tells CLI its authenticated role


# ---------------------------------------------------------------------------
# Agent → Hub
# ---------------------------------------------------------------------------

class ResultMessage(BaseModel):
    """Sent by the agent after executing a command."""
    type: Literal["result"] = "result"
    command_id: str = ""                      # echoes the command's unique ID
    command: str                              # mirrors the command that was run
    status: Literal["success", "failure"]     # outcome
    message: str = ""                         # human-readable detail or error text
    data: Optional[Any] = None
    error: Optional[str] = None



class VoiceRequestMessage(BaseModel):
    """Sent by the agent to process conversational input over the WebSocket."""
    type: Literal["voice_request"] = "voice_request"
    text: str
    command_id: str = Field(default_factory=lambda: uuid4().hex)
    latency_metrics: dict = Field(default_factory=dict)
    speaker_id: Optional[str] = None
    speaker_label: Optional[str] = None
    speaker_metadata: Optional[dict] = None

class VoicePartialMessage(BaseModel):
    """Sent by the agent to provide ongoing partial transcripts."""
    type: Literal["voice_partial"] = "voice_partial"
    text: str
    command_id: str

class VoiceResponseMessage(BaseModel):
    """Returned by the hub after processing conversational input."""
    type: Literal["voice_response"] = "voice_response"
    command_id: str
    message: str
    summary: Optional[str] = None
    status: str = "success"
    is_post_approval: bool = False
    is_task_result: bool = False
    task_id: Optional[str] = None
    request_id: Optional[str] = None
    data: Optional[dict] = None
    latency_metrics: dict = Field(default_factory=dict)
    action_plan_debug: Optional[dict] = None

class StatusMessage(BaseModel):
    """Periodic heartbeat / presence signal from agent to hub."""
    type: Literal["status"] = "status"
    status: Literal["online", "offline"] = "online"
    telemetry: dict = Field(default_factory=dict)


class HeartbeatMessage(BaseModel):
    """Periodic lightweight heartbeat from agent to hub."""
    type: Literal["heartbeat"] = "heartbeat"
    device_id: str
    timestamp: Optional[float] = None
    agent_version: Optional[str] = None
    state: str = "online"
    cpu_load: Optional[float] = None
    memory_load: Optional[float] = None
    battery: Optional[dict] = None
    active_task_count: int = 0
    telemetry: dict = Field(default_factory=dict)


class CapabilityAdvertisementMessage(BaseModel):
    """Explicit capability advertisement sent by agent upon reconnect or change."""
    type: Literal["capability_advertisement"] = "capability_advertisement"
    device_id: str
    capabilities: List[str] = Field(default_factory=list)
    device_type: Optional[str] = None
    platform: Optional[str] = None
    agent_version: Optional[str] = None
    transports: Optional[Dict[str, bool]] = None


class AuthResponse(BaseModel):
    """Agent's reply to an AuthChallenge — HMAC(secret, challenge)."""
    type: Literal["auth_response"] = "auth_response"
    device_id: str
    response: str  # hex-encoded HMAC-SHA256
    platform: str = "unknown"
    capabilities: List[str] = []
    device_type: Optional[str] = None
    agent_version: Optional[str] = None
    transports: Optional[Dict[str, bool]] = None


# ---------------------------------------------------------------------------
# Enrollment (HTTP)
# ---------------------------------------------------------------------------

class EnrollRequest(BaseModel):
    """Body for POST /enroll — register a new device."""
    device_id: str
    device_name: str = ""
    role: str = "standard"
    device_type: Optional[str] = None
    trust_level: Optional[str] = None


class EnrollResponse(BaseModel):
    """Returned by POST /enroll — contains the one-time secret."""
    device_id: str
    device_name: str
    role: str
    secret: str  # shown once, never stored in plaintext


class BootstrapRequest(BaseModel):
    """Body for POST /bootstrap — first-run admin creation."""
    device_id: str
    device_name: str = ""


# ---------------------------------------------------------------------------
# Secret rotation (Phase 3)
# ---------------------------------------------------------------------------

class RotateSecretRequest(BaseModel):
    """Body for POST /rotate-secret — admin rotates a device's secret."""
    target_device_id: str


class RotateSecretResponse(BaseModel):
    """Returned by POST /rotate-secret — contains the new one-time secret."""
    device_id: str
    new_secret: str  # shown once, never stored in plaintext


# ---------------------------------------------------------------------------
# Confirmation flow (Tier 2/3 commands)
# ---------------------------------------------------------------------------

class ConfirmationRequest(BaseModel):
    """Pushed to admin CLI sessions when a Tier 2/3 command needs approval."""
    type: Literal["confirmation_request"] = "confirmation_request"
    command_id: str
    target_device: str
    command: str
    args: List[str] = []
    requested_by: str  # device_id or "cli"
    tier: int
    timeout_seconds: int = 60


class ConfirmationResponse(BaseModel):
    """Admin's decision on a pending confirmation."""
    type: Literal["confirmation_response"] = "confirmation_response"
    command_id: str
    decision: Literal["approve", "deny"]


# ---------------------------------------------------------------------------
# Generic inbound envelope (used by hub to parse unknown incoming JSON)
# ---------------------------------------------------------------------------

class InboundEnvelope(BaseModel):
    """Minimal parse to determine message type before full deserialization."""
    type: str
    model_config = {"extra": "allow"}

# ---------------------------------------------------------------------------
# Phase 5: Memory, Tasks, Integrations
# ---------------------------------------------------------------------------

class MemoryItem(BaseModel):
    id: str
    category: str
    key: str
    value: str
    source: str = "user"
    confidence: float = 1.0
    last_accessed: str = ""
    relationship_entity_id: str = ""
    created_at: str
    updated_at: str

class TaskItem(BaseModel):
    id: str
    title: str
    type: Literal["one_time", "scheduled", "recurring", "background"]
    status: Literal["pending", "waiting", "waiting_for_approval", "running", "paused", "completed", "failed", "cancelled"]
    trigger_time: str = ""
    cron_expression: str = ""
    timezone: str = "UTC"
    assigned_device: str = ""
    command: str
    args: List[str] = []
    required_capability: str = ""
    reroutable: bool = False
    retry_count: int = 0
    max_retries: int = 0
    last_error: str = ""
    execution_id: str = ""
    context: str = "{}"
    created_at: str
    updated_at: str

class IntegrationItem(BaseModel):
    id: str
    plugin_name: str
    account_id: str = ""
    config: str = "{}"
    status: str = "active"
    created_at: str

# ---------------------------------------------------------------------------
# Phase 6: Continuity Sessions
# ---------------------------------------------------------------------------

class ContinuitySessionCreate(BaseModel):
    active_task: str = ""
    active_project: str = ""
    active_file: str = ""
    source_device: str
    current_device: str
    required_capabilities: List[str] = []
    last_known_state: dict = {}
    continuation_metadata: dict = {}

class ContinuitySessionUpdate(BaseModel):
    active_task: Optional[str] = None
    active_project: Optional[str] = None
    active_file: Optional[str] = None
    current_device: Optional[str] = None
    last_known_state: Optional[dict] = None
    continuation_metadata: Optional[dict] = None


# ---------------------------------------------------------------------------
# Phase 7A: People Memory and Relationship Model
# ---------------------------------------------------------------------------

class PersonFact(BaseModel):
    id: str
    person_id: str
    key: str
    value: str
    confidence: float = 1.0
    source: str = "user"
    timestamp: str

class Person(BaseModel):
    person_id: str
    display_name: str
    aliases: List[str] = []
    relationship_type: str = ""
    notes: str = ""
    created_at: str
    updated_at: str
    last_interaction_at: str = ""
    facts: List[PersonFact] = []

class Relationship(BaseModel):
    id: str
    source_person_id: str
    target_person_id: str
    relationship_type: str
    metadata: Dict[str, Any] = {}
    created_at: str

class PersonCreate(BaseModel):
    display_name: str
    aliases: List[str] = []
    relationship_type: str = ""
    notes: str = ""

class PersonUpdate(BaseModel):
    display_name: Optional[str] = None
    aliases: Optional[List[str]] = None
    relationship_type: Optional[str] = None
    notes: Optional[str] = None
    last_interaction_at: Optional[str] = None

class PersonFactCreate(BaseModel):
    key: str
    value: str
    confidence: float = 1.0
    source: str = "user"

class RelationshipCreate(BaseModel):
    source_person_id: str
    target_person_id: str
    relationship_type: str
    metadata: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Phase 7B: Speaker Identity and Multi-Speaker Context
# ---------------------------------------------------------------------------

class SpeakerProfile(BaseModel):
    speaker_id: str
    person_id: Optional[str] = None
    label: str = "Unknown Speaker"
    is_primary_user: bool = False
    confidence: float = 1.0
    metadata: Dict[str, Any] = {}
    created_at: str
    updated_at: str

class SpeakerProfileCreate(BaseModel):
    speaker_id: Optional[str] = None
    person_id: Optional[str] = None
    label: str = "Unknown Speaker"
    is_primary_user: bool = False
    confidence: float = 1.0
    metadata: Dict[str, Any] = {}

class SpeakerProfileUpdate(BaseModel):
    person_id: Optional[str] = None
    label: Optional[str] = None
    is_primary_user: Optional[bool] = None
    confidence: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None

class SpeakerTurn(BaseModel):
    turn_id: str = Field(default_factory=lambda: uuid4().hex)
    speaker_id: str
    speaker_label: str
    person_id: Optional[str] = None
    display_name: Optional[str] = None
    relationship_type: Optional[str] = None
    text: str
    timestamp: float

from .intent_schema import ActionPlan, ModelResult, ConversationalResponse


