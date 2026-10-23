"""A14 v1: bounded declarative graphs. Provider output never grants authority."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(min_length=1, max_length=48, pattern=r'^[a-zA-Z][a-zA-Z0-9_-]*$')]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


def delegation_forbidden(name):
    # Exact system-control namespaces, not natural-language routing. Delegated
    # work cannot spawn legacy goals/tasks/routines or control unrelated tasks.
    return name.startswith(('agent.', 'task.', 'routine.', 'routines.')) or (
        name.startswith('goal.') and name != 'goal.wait')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class WorkerRole(Strict):
    name: Name
    purpose: Text
    category: Literal['general', 'reasoning', 'coding', 'computer_use', 'planning'] = 'reasoning'


DEFAULT_ROLES = (
    WorkerRole(name='research', purpose='Gather and distinguish sourced information from assumptions.'),
    WorkerRole(name='reasoning', purpose='Analyze supplied information and identify limitations.'),
    WorkerRole(name='coding', purpose='Reason about code; do not execute generated code.', category='coding'),
    WorkerRole(name='execution', purpose='Use only declared Cortex capabilities and verify their receipts.', category='computer_use'),
    WorkerRole(name='verification', purpose='Check evidence, contradictions and unsupported claims.'),
    WorkerRole(name='synthesis', purpose='Combine supported results and preserve limitations.'),
)


class Node(Strict):
    id: Name
    role: Name
    objective: Text
    kind: Literal['reasoning', 'capability'] = 'reasoning'
    dependencies: Annotated[list[Name], Field(max_length=8)] = Field(default_factory=list)
    memory_keys: Annotated[list[Annotated[str, Field(min_length=1, max_length=120)]], Field(max_length=4)] = Field(default_factory=list)
    required_capabilities: Annotated[list[Annotated[str, Field(min_length=1, max_length=120)]], Field(max_length=6)] = Field(default_factory=list)
    target_device: Annotated[str, Field(max_length=80)] | None = None
    output_type: Literal['text', 'number', 'boolean'] = 'text'
    required: bool = True
    timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 90
    # Criteria are declarations to verify, never authorization or executable code.
    success_criteria: Annotated[list[Text], Field(min_length=1, max_length=4)]


class TaskGraph(Strict):
    protocol_version: Literal[1] = 1
    nodes: Annotated[list[Node], Field(min_length=2, max_length=8)]

    @model_validator(mode='after')
    def valid_graph(self):
        ids = {n.id for n in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError('duplicate_node')
        if not any(n.required for n in self.nodes):
            raise ValueError('no_required_work')
        done = set()
        for n in self.nodes:
            if len(set(n.dependencies)) != len(n.dependencies) or n.id in n.dependencies or not set(n.dependencies) <= ids:
                raise ValueError('invalid_dependency')
            if n.kind == 'reasoning' and (n.required_capabilities or n.target_device):
                raise ValueError('reasoning_has_no_execution_authority')
            if n.kind == 'capability' and not n.required_capabilities:
                raise ValueError('capability_scope_required')
        while len(done) < len(ids):
            ready = {n.id for n in self.nodes if n.id not in done and set(n.dependencies) <= done}
            if not ready:
                raise ValueError('cyclic_graph')
            done |= ready
        return self


class Claim(Strict):
    key: Name
    value: Text


class WorkerOutput(Strict):
    value: Annotated[str, Field(max_length=4000)] | float | bool
    claims: Annotated[list[Claim], Field(max_length=12)] = Field(default_factory=list)
    limitations: Annotated[list[Text], Field(max_length=8)] = Field(default_factory=list)

    def validate_type(self, expected):
        valid = (isinstance(self.value, str) and bool(self.value.strip()) if expected == 'text' else
                 isinstance(self.value, float) if expected == 'number' else isinstance(self.value, bool))
        if not valid:
            raise ValueError('worker_output_type_mismatch')
        return self


class Synthesis(Strict):
    answer: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence_nodes: Annotated[list[Name], Field(min_length=1, max_length=8)]


class Verification(Strict):
    supported: bool
    criteria_satisfied: bool
    checked_nodes: Annotated[list[Name], Field(max_length=8)]
    issues: Annotated[list[Text], Field(max_length=8)] = Field(default_factory=list)
