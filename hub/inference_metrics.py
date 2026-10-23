"""Content-free wire measurements, not a tokenizer or quota authority."""
import json
import re

import httpx


def safe_rate_headers(headers):
    """Allow protocol numbers/durations only; a header is untrusted text too."""
    result = {}
    for key,value in headers.items():
        if not isinstance(value,str) or len(value)>64:
            continue
        if key=='x-ratelimit-scope':
            if value in {'model','provider'}: result[key]=value
        elif re.fullmatch(r'\d+(?:\.\d+)?',value) or (
            key in {'x-ratelimit-reset-requests','x-ratelimit-reset-tokens'} and
            re.fullmatch(r'(?:\d+(?:\.\d+)?[dhms])+',value)):
            result[key]=value
    return result


def payload_metrics(payload):
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())
    messages = payload.get('messages') or []
    raw_text = [m['content'] for m in messages if isinstance(m.get('content'), str)]
    system = next((m['content'] for m in messages if m.get('role')=='system' and isinstance(m.get('content'),str)), '')
    catalog = state = ''
    if '\nCAPABILITIES:\n' in system and '\nSTATE:\n' in system:
        _, tail = system.split('\nCAPABILITIES:\n', 1)
        catalog, state = tail.split('\nSTATE:\n', 1)
    wire = len(httpx.Request('POST', 'https://metrics.invalid/', json=payload).content)
    text_bytes = sum(len(text.encode()) for text in raw_text)
    return {'wire_bytes':wire, 'system_bytes':len(system.encode()),
        'catalog_bytes':len(catalog.encode()), 'state_bytes':len(state.encode()),
        'message_text_bytes':text_bytes, 'messages_serialized_bytes':size(messages),
        'tools_serialized_bytes':size(payload.get('tools', [])),
        'schema_bytes':sum(size(t.get('function',{}).get('parameters',{})) for t in payload.get('tools',[])),
        'wire_minus_raw_text_bytes':wire-text_bytes,
        'output_token_allowance':payload.get('max_tokens'),
        'wire_token_estimate':(wire+3)//4, 'token_measurement':'byte_estimate_not_tokenizer'}
