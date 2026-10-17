"""
hub/intent_schema.py
--------------------
Schema and validation models for Phase 6 Conversational Intent.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class ActionPlan(BaseModel):
    intent: str = Field(description="The canonical name of the intent (e.g. open_app, store_memory)")
    source_device: str = Field(description="The device originating the request")
    target_device: Optional[str] = Field(None, description="The resolved device id to execute the action on")
    target_device_reference: Optional[str] = Field(None, description="The natural language reference used for the device, e.g. 'this laptop'")
    capability: str = Field(description="The underlying Cortex capability required (e.g. 'shell.execute', 'gmail.send', 'ui.notify', 'memory.write')")
    action: str = Field(description="The specific action or command name")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Arguments for the action")
    requires_confirmation: bool = Field(False, description="Whether the LLM believes this needs explicit user approval")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in intent and device resolution")
    user_facing_summary: str = Field(description="A short summary for the user of what will be done")
    owning_device: Optional[str] = Field(None, description="The device that owns the target resource (e.g. file or active window)")

class ModelResult(BaseModel):
    type: str = Field(description="One of: 'RESPONSE', 'ACTION', 'TASK', 'TASK_STEP'")
    response_text: Optional[str] = Field(None, description="Conversational text if type is RESPONSE")
    action_plan: Optional[ActionPlan] = Field(None, description="Action details if type is ACTION")
    step_decision: Optional[Dict[str, Any]] = Field(None, description="Task step decision if type is TASK_STEP")
    plan: Optional[Dict[str, Any]] = Field(None, description="Goal plan details if type is PLAN")

class ConversationalResponse(BaseModel):
    status: str
    message: str
    data: Optional[Dict[str, Any]] = None
    action_plan: Optional[ActionPlan] = None
    command_id: Optional[str] = None
    latency_metrics: Optional[Dict[str, Any]] = None
    provenance: Optional[Dict[str, Any]] = Field(None, description="Internal source provenance metadata (provider, source_urls, retrieved_at, source_type)")
