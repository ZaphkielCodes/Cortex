"""Compatibility response envelope; AgentTurn owns result interpretation."""
from ..intent_schema import ConversationalResponse


class ResponseComposer:
    @classmethod
    def compose(cls, intent, raw_result, grounded_target=None):
        # Execution messages and provenance are evidence. Intent text is not.
        status = raw_result.get("status", "error")
        if status == "failure":
            status = "error"
        message = raw_result.get("message")
        if not isinstance(message, str) or not message.strip():
            status = "error"
            message = "The result needs interpretation before completion can be confirmed."
        return ConversationalResponse(status=status, message=message,
            data=raw_result.get("data"), provenance=raw_result.get("provenance"),
            command_id=raw_result.get("command_id"))
