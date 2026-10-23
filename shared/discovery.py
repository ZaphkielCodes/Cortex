"""Portable A13 data-only capability advertisement protocol (no executable UI/code)."""
import hashlib
import json
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')]


class Advertisement(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    capability_id: Identifier
    version: Annotated[str, Field(pattern=r'^[0-9]{1,3}\.[0-9]{1,3}$')] = '1.0'
    input_schema: dict
    output_schema: dict
    availability: Literal['available', 'unavailable', 'degraded'] = 'available'
    evidence: Literal['handler_registered', 'session_handler', 'legacy_report']
    permission_tier: Annotated[int, Field(ge=1, le=3)] = 1
    risk: Literal['low', 'medium', 'high'] = 'low'
    required_permissions: Annotated[list[Identifier], Field(max_length=8)] = Field(default_factory=list)
    privacy_class: Literal['PUBLIC','INTERNAL','PERSONAL','SENSITIVE','HIGHLY_SENSITIVE'] = 'INTERNAL'
    last_verified_at: None = None  # Peers cannot assert a successful physical verification.

    @field_validator('input_schema', 'output_schema')
    @classmethod
    def bounded_data_schema(cls, value):
        if len(json.dumps(value)) > 16384:
            raise ValueError('Schema is too large')
        def walk(node, depth=0):
            if depth > 10: raise ValueError('Schema nesting limit')
            if isinstance(node, dict):
                if any(k in node for k in ('$ref', '$dynamicRef', '$id', '$schema', 'pattern')):
                    raise ValueError('External references and executable schema extensions are not permitted')
                for v in node.values(): walk(v, depth+1)
            elif isinstance(node, list):
                if len(node) > 128: raise ValueError('Schema collection limit')
                for v in node: walk(v, depth+1)
        walk(value)
        return value


class AdvertisementBatch(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    protocol_version: Literal[1] = 1
    device_id: Identifier
    executor_location: Literal['device'] = 'device'
    generation: Annotated[int, Field(ge=0)] = 0
    ttl_seconds: Annotated[int, Field(ge=15, le=120)] = 90
    capabilities: Annotated[list[Advertisement], Field(max_length=96)]

    @model_validator(mode='after')
    def no_duplicates(self):
        ids = [c.capability_id for c in self.capabilities]
        if len(ids) != len(set(ids)): raise ValueError('Duplicate capability IDs')
        if len(self.model_dump_json()) > 262144: raise ValueError('Advertisement limit')
        return self


def schemas(capability):
    types = {'string', 'number', 'integer', 'boolean', 'object', 'array', 'null'}
    properties = {}
    for key, spec in capability.arguments.items():
        value_type = spec.get('type', 'string')
        properties[key] = {'type': value_type if value_type in types else 'string'}
        if 'enum' in spec: properties[key]['enum'] = spec['enum']
    inputs = {'type': 'object', 'properties': properties,
              'required': capability.required_arguments, 'additionalProperties': True}
    outputs = {'type': 'object', 'properties': {'status': {'type': 'string'},
        'message': {'type': 'string'}, 'data': {'type': ['object', 'array', 'null']}},
        'required': ['status', 'message'], 'additionalProperties': True}
    return inputs, outputs


def schema_hash(advertisement):
    return hashlib.sha256(json.dumps([advertisement.version, advertisement.input_schema,
        advertisement.output_schema], sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def describe(capability, evidence='handler_registered'):
    a, b = schemas(capability)
    return Advertisement(capability_id=capability.name, input_schema=a, output_schema=b, evidence=evidence,
        permission_tier=capability.permission_tier, risk=capability.risk, required_permissions=[capability.name],
        privacy_class=capability.privacy_metadata.get('data_class','INTERNAL'))
