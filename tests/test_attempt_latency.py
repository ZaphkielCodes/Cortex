"""Per-attempt timing through real transports with only mocked network I/O."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from hub.direct_provider import DirectLLMProvider
from hub.llm_provider import ProviderHTTPError
from hub.tracing import InteractionTrace, observe_model_attempt


@pytest.mark.asyncio
async def test_failed_transport_does_not_inherit_previous_completion_or_ttft():
    trace=InteractionTrace();metrics={}
    responses=iter([200,429])
    def reply(request):
        status=next(responses)
        return httpx.Response(status,json={'choices':[{'finish_reason':'stop','message':{'content':'Ready'}}]})
    provider=DirectLLMProvider('groq','openai/gpt-oss-120b',api_key='fixture',transport=httpx.MockTransport(reply))
    context={'force_response_only':True,'trace':trace,'latency_metrics':metrics}
    await provider.generate_result('Synthetic request',context)
    first=trace.to_dict()['stage_durations']['llm_total_latency']
    with pytest.raises(ProviderHTTPError): await provider.generate_result('Synthetic request',context)
    assert 'model_complete' not in trace.timestamps and 'model_complete' not in metrics
    assert trace.model_attempts[-1]['model_complete'] is None
    assert trace.model_attempts[-1]['status']=='failed'
    durations=trace.to_dict()['stage_durations']
    assert durations['llm_total_latency']>=first>=0
    assert durations['llm_ttft'] is None


def test_legacy_trace_reused_start_clears_completion_and_keeps_ttft_unknown():
    trace=InteractionTrace()
    trace.record_stage('model_request_started',1);trace.record_stage('model_complete',2)
    assert trace.to_dict()['stage_durations']['llm_ttft'] is None
    trace.record_stage('model_request_started',10)
    assert trace.to_dict()['stage_durations']['llm_total_latency'] is None
    trace.record_stage('model_complete',9)
    assert trace.to_dict()['stage_durations']['llm_total_latency'] is None


@pytest.mark.asyncio
async def test_multiple_attempts_keep_owned_timestamps_and_monotonic_aggregate():
    class Provider:
        def __init__(self,name): self.name=name;self.model='fixture'
        @observe_model_attempt
        async def generate(self,text,context):
            trace=context['trace'];trace.record_stage('model_request_started')
            await asyncio.sleep(.001)
            if text=='fail': raise TimeoutError()
            trace.record_stage('model_complete');return 'ready'
    trace=InteractionTrace();values=[]
    for name,text in [('one','ready'),('two','fail'),('three','ready')]:
        try:await Provider(name).generate(text,{'trace':trace})
        except TimeoutError:pass
        values.append(trace.to_dict()['stage_durations']['llm_total_latency'])
    assert values==sorted(values) and values[0]>=0
    assert [a['provider'] for a in trace.model_attempts]==['one','two','three']
    assert trace.model_attempts[1]['model_complete'] is None
    assert all(a['model_first_token'] is None for a in trace.model_attempts)
    restored=InteractionTrace.from_dict(trace.to_dict())
    assert restored.to_dict()['stage_durations']==trace.to_dict()['stage_durations']


@pytest.mark.asyncio
async def test_late_attempt_cannot_overwrite_newer_attempt_timestamps():
    gate=asyncio.Event();entered=asyncio.Event()
    class Provider:
        name='fixture';model='fixture'
        @observe_model_attempt
        async def generate(self,text,context):
            context['trace'].record_stage('model_request_started')
            if text=='late':entered.set();await gate.wait()
            context['trace'].record_stage('model_complete')
    trace=InteractionTrace();provider=Provider()
    late=asyncio.create_task(provider.generate('late',{'trace':trace}))
    await entered.wait();await provider.generate('new',{'trace':trace})
    completed=trace.timestamps['model_complete']
    gate.set();await late
    assert trace.timestamps['model_complete']==completed
    assert trace.model_attempts[0]['model_complete']>completed


@pytest.mark.asyncio
async def test_nonstreaming_gemini_does_not_invent_first_token(monkeypatch):
    from hub.llm_provider import GeminiLLMProvider
    async def generate_content(**kwargs):return SimpleNamespace(text='Ready')
    client=SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    provider=GeminiLLMProvider(api_key='fixture')
    monkeypatch.setattr(provider,'_get_client',lambda:client)
    trace=InteractionTrace();metrics={}
    await provider.generate_result('Synthetic request',{'trace':trace,'latency_metrics':metrics,'force_response_only':True})
    assert trace.to_dict()['stage_durations']['llm_ttft'] is None
    assert 'model_first_token' not in metrics
    assert trace.model_attempts[0]['model_complete'] is not None
