"""Real transport contracts with deterministic responses; never live acceptance."""
import json
from pathlib import Path

import httpx
import pytest

from hub.direct_provider import DirectLLMProvider
from hub.inference_metrics import payload_metrics, safe_rate_headers
from hub.llm_provider import ProviderHTTPError
from hub.orchestration_inference import inference_prompt, proposal_schema, compact_schema
from hub.orchestration_protocol import TaskGraph, WorkerRole
from hub.provider_failover import FailoverManager


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code,scope,expected',[
    (413,'rate_limit_exceeded','TPM','CONTEXT_TOO_LARGE'),
    (413,'context_length_exceeded','','CONTEXT_TOO_LARGE'),
    (429,'rate_limit_exceeded','TPD','RATE_LIMIT_429'),
    (503,'private-unrecognized-body','','SERVICE_UNAVAILABLE_503'),
])
async def test_sanitized_evidence_preserves_status_and_does_not_echo_body(caplog,status,code,scope,expected):
    secret='do-not-log-provider-body-or-org-id'
    def respond(request):
        return httpx.Response(status,headers={'x-ratelimit-limit-tokens':'8000',
            'x-ratelimit-reset-tokens':secret},json={'error':{'code':code,
                'message':f'{secret} {scope}: Limit 8000 Used 400 Requested 9000'}})
    provider=DirectLLMProvider('groq',api_key='never-log-key',transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderHTTPError) as caught:
        await provider.generate_result('never-log-request',{'mode':'agent_decision'})
    observed=provider.last_error_metadata
    assert observed['http_status']==caught.value.status_code==status
    assert observed['quota_metadata']['known_requested']==9000
    assert observed['quota_metadata']['known_limit']==8000
    assert FailoverManager.classify(caught.value).category==expected
    assert observed['rate_limit_headers']=={'x-ratelimit-limit-tokens':'8000'}
    assert secret not in json.dumps(observed) and secret not in caplog.text
    assert 'never-log-key' not in caplog.text and 'never-log-request' not in caplog.text


def test_wire_metrics_match_actual_httpx_bytes_without_exposing_contents():
    payload={'model':'fixture','max_tokens':1024,'messages':[{'role':'system',
        'content':'Policy\nCAPABILITIES:\n{"rows":[]}\nSTATE:\n{"fact":"π"}'},
        {'role':'user','content':'Sensitive supplied text'}],
        'tools':[{'function':{'parameters':{'type':'object','additionalProperties':False}}}]}
    metrics=payload_metrics(payload)
    assert metrics['wire_bytes']==len(httpx.Request('POST','https://fixture.invalid',json=payload).content)
    assert metrics['catalog_bytes']==len('{"rows":[]}'.encode())
    assert metrics['state_bytes']==len('{"fact":"π"}'.encode())
    assert metrics['wire_minus_raw_text_bytes']==metrics['wire_bytes']-metrics['message_text_bytes']
    assert metrics['output_token_allowance']==1024 and metrics['token_measurement']=='byte_estimate_not_tokenizer'
    assert 'Sensitive' not in json.dumps(metrics) and 'fact' not in json.dumps(metrics)


def test_a14_native_schema_deduplication_keeps_full_contract_for_text_providers():
    schema=proposal_schema(TaskGraph)
    prompt=inference_prompt(WorkerRole(name='planner',purpose='Preserve all requested criteria.'),schema)
    ctx={'mode':'agent_decision','system_prompt':prompt,'decision_schema':schema,
         'a14_inference_contract':True,'privacy_class':'PUBLIC'}
    groq=DirectLLMProvider('groq',api_key='fixture').payload('Original request.',ctx)
    nvidia=DirectLLMProvider('nvidia',api_key='fixture').payload('Original request.',ctx)
    assert 'A14_OUTPUT_SCHEMA' not in groq['messages'][0]['content']
    assert 'call the provided agent_decision function exactly once' in groq['messages'][0]['content']
    assert 'Preserve all requested criteria.' in groq['messages'][0]['content']
    assert groq['tools'][0]['function']['parameters']==schema
    assert 'A14_OUTPUT_SCHEMA' in nvidia['messages'][0]['content']
    assert json.loads(groq['messages'][1]['content'])['request']=='Original request.'
    assert ctx['system_prompt']==prompt and ctx['decision_schema']==schema
    legacy=DirectLLMProvider('groq',api_key='fixture').payload('Original request.',dict(ctx,a14_inference_contract=False))
    assert 'A14_OUTPUT_SCHEMA' in legacy['messages'][0]['content']
    assert payload_metrics(groq)['wire_bytes']<payload_metrics(legacy)['wire_bytes']


def test_schema_annotation_compaction_does_not_change_validation_contract():
    from hub.agent_turn import AgentDecision
    schema=AgentDecision.model_json_schema()
    ctx={'mode':'agent_decision','system_prompt':'All safety instructions.','decision_schema':schema,
         'a14_inference_contract':True}
    wire=DirectLLMProvider('groq',api_key='fixture').payload('request',ctx)
    parameters=wire['tools'][0]['function']['parameters']
    assert compact_schema(parameters)==compact_schema(schema)
    assert parameters['title']=='AgentDecision'
    assert len(json.dumps(parameters))<len(json.dumps(schema))


def test_only_exact_schema_duplicate_is_removed():
    schema=proposal_schema(TaskGraph)
    different=dict(schema,additionalProperties=True)
    prompt='Retain policy.\nA14_OUTPUT_SCHEMA:\n'+json.dumps(different)
    wire=DirectLLMProvider('groq',api_key='fixture').payload('request',{
        'mode':'agent_decision','system_prompt':prompt,'decision_schema':schema,'a14_inference_contract':True})
    assert 'A14_OUTPUT_SCHEMA' in wire['messages'][0]['content']


def test_verified_profile_uses_existing_loader_and_preserves_privacy_eligibility(monkeypatch):
    from hub.direct_router import DirectModelRouter
    from hub.routing_profiles import TaskProfile, model_profile
    path=Path(__file__).resolve().parents[1]/'shared'/'inference'/'model-profiles.verified.json'
    monkeypatch.setenv('CORTEX_MODEL_PROFILE_PATH',str(path))
    providers=[DirectLLMProvider('groq',model,api_key='fixture')
               for model in ('qwen/qwen3.8-27b','openai/gpt-oss-120b')]
    providers.append(DirectLLMProvider('nvidia',api_key='fixture'))
    router=DirectModelRouter(providers)
    assert [r.profile.context_window for r in router.routes]==[131042,131072,1048576]
    from dataclasses import asdict
    for route in router.routes:
        baseline=asdict(model_profile(route.provider));baseline.pop('context_window')
        configured=asdict(route.profile);configured.pop('context_window')
        # JSON arrays roundtrip profile tuples; semantic modalities are unchanged.
        assert json.loads(json.dumps(baseline))==json.loads(json.dumps(configured))
    task=TaskProfile(privacy_class='INTERNAL',requires_tools=True,requires_long_context=True,context_tokens=9000)
    ranked,rejected=router.selector.rank(router.routes,task,minimum_window=9000)
    assert [r.provider.name for r,_ in ranked]==['groq','groq']
    assert any(r['provider']=='nvidia' and r['reason']=='provider_terms' for r in rejected)


def test_rate_headers_accept_only_protocol_numbers_and_durations():
    assert safe_rate_headers({'retry-after':'2.5','x-ratelimit-scope':'model',
        'x-ratelimit-reset-tokens':'1m2.3s','x-ratelimit-limit-tokens':'8000',
        'x-ratelimit-remaining-tokens':'echo secret','x-ratelimit-reset-requests':'credentials'})=={
            'retry-after':'2.5','x-ratelimit-scope':'model','x-ratelimit-reset-tokens':'1m2.3s',
            'x-ratelimit-limit-tokens':'8000'}


def test_delegated_catalog_contains_only_declared_tools_without_losing_constraints_or_evidence():
    from hub.orchestration_inference import scoped_prompt
    from hub.context_compression import pack_catalog
    cap={'name':'ui.wait','description':'Wait without input.','arguments':{'seconds':'required number {"minimum":0,"maximum":60}'},'devices':['asus']}
    other={'name':'not.allowed','description':'Unrelated tool','arguments':{'secret':'required string'},'devices':['other-device']}
    state={'loaded_schemas':{'ui.wait':{'arguments':{'seconds':{'minimum':0,'maximum':60}}},'not.allowed':{'arguments':{}}},
           'tool_results':[{'status':'failure','verified':False}],'task_id':'worker','unavailable_capabilities':['ui.wait']}
    prompt='Preserve all safety instructions.\nCAPABILITIES:\n'+json.dumps(pack_catalog([cap,other]))+'\nSTATE:\n'+json.dumps(state)
    smaller=scoped_prompt(prompt,{'ui.wait'})
    assert smaller.startswith('Preserve all safety instructions.')
    packed=json.loads(smaller.split('\nCAPABILITIES:\n')[1].split('\nSTATE:\n')[0])
    assert packed==pack_catalog([cap])
    preserved=json.loads(smaller.split('\nSTATE:\n')[1])
    assert preserved['tool_results']==state['tool_results'] and preserved['unavailable_capabilities']==['ui.wait']
    assert preserved['loaded_schemas']=={'ui.wait':state['loaded_schemas']['ui.wait']}
    assert scoped_prompt('Unknown prompt format.',{'ui.wait'})=='Unknown prompt format.'


@pytest.mark.asyncio
async def test_invalid_native_call_shape_diagnostic_never_echoes_model_arguments(caplog):
    body={'error':{'code':'tool_use_failed','failed_generation':json.dumps({
        'decision':'RESPOND','response_text':'never-echo-generated-content'})}}
    p=DirectLLMProvider('groq',api_key='fixture',transport=httpx.MockTransport(lambda _:httpx.Response(400,json=body)))
    with pytest.raises(ProviderHTTPError):
        await p.generate_result('unseen request',{'mode':'agent_decision'})
    assert p.last_error_metadata['failed_generation_shape']['recognized_fields']==['decision','response_text']
    assert 'never-echo-generated-content' not in caplog.text
    assert 'never-echo-generated-content' not in json.dumps(p.last_error_metadata)


@pytest.mark.parametrize('provider',['groq','nvidia'])
def test_a14_transport_instructions_preserve_trailing_state_json(provider):
    from hub.agent_turn import AgentDecision
    wire=DirectLLMProvider(provider,api_key='fixture').payload('Keep original request.',{
        'mode':'agent_decision','decision_schema':AgentDecision.model_json_schema(),
        'a14_inference_contract':True,
        'system_prompt':'Existing policies.\nCAPABILITIES:\n{}\nSTATE:\n{"verified":false}'})
    prompt=wire['messages'][0]['content']
    assert prompt.startswith('Existing policies.')
    assert json.loads(prompt.split('\nSTATE:\n')[1])=={'verified':False}
    assert json.loads(prompt.split('\nCAPABILITIES:\n')[1].split('\nSTATE:\n')[0])=={}


def test_full_planning_catalog_packs_losslessly_under_existing_context_limit():
    from hub.capability_registry import CanonicalCapabilityRegistry
    from hub.orchestration_protocol import delegation_forbidden,DEFAULT_ROLES
    from hub.orchestration_inference import pack_planning_catalog
    caps=[c for c in CanonicalCapabilityRegistry.get_instance().list_capabilities()
          if not delegation_forbidden(c.name) and not c.privacy_metadata.get('delegates_work')]
    rows=[{'name':c.name,'description':c.description[:180],
        'arguments':{k:{field:v[field] for field in ('type','required') if field in v}
                     for k,v in c.arguments.items() if isinstance(v,dict)},
        'targets':['asus'],'target_type':c.target_type} for c in caps]
    packed=pack_planning_catalog(rows)
    restored=[{'name':name,'description':description,
        'arguments':{key:packed['argument_definitions'][index] for key,index in arguments.items()},
        'targets':packed['target_sets'][target],'target_type':kind}
        for name,description,arguments,target,kind in packed['rows']]
    assert restored==rows
    common={'request':'Preserve the full task requirements.','roles':[r.model_dump() for r in DEFAULT_ROLES]}
    before=len(json.dumps(dict(common,eligible_capabilities=rows)).encode())
    after=len(json.dumps(dict(common,eligible_capabilities=packed),separators=(',',':')).encode())
    assert before>24576 and after<24576


@pytest.mark.asyncio
async def test_context_rejection_is_explicit_and_does_not_call_a_provider():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from hub.orchestration_workers import Workers,CallBudget
    from hub.orchestration_inference import OrchestrationContextError
    p=SimpleNamespace(generate_result=AsyncMock())
    worker=Workers(None,p,{},CallBudget())
    # Many complete fields exceed the bound; no user/criteria strings are cut.
    payload={'required_criteria':['x'*4000 for _ in range(8)]}
    with pytest.raises(OrchestrationContextError) as error:
        await worker.infer(TaskGraph,payload,'asus',{},'diagnostic',WorkerRole(name='planner',purpose='Plan safely.'))
    assert error.value.category=='CONTEXT_BUDGET_EXCEEDED' and error.value.measured_bytes>24576
    p.generate_result.assert_not_awaited()
    assert worker.budget.remaining==32
