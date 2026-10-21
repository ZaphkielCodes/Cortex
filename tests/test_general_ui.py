"""Generic UI behavior with simulated OS/model boundaries; no physical input."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest

from agent.ui_models import UIElement, UIObservation
from agent.ui_observer import UIObserver
from agent.ui_action_executor import UIActionExecutor
from agent.ui_text import resolve_text_target, verify_text_entry
from agent.ui_verifier import UIActionVerifier
from hub.agent_turn import AgentTurn
from hub.computer_use_controller import ComputerUseTaskController
from hub.pipeline.pipeline import RequestPipeline
from test_agent_turn_runtime import ScriptedModel, call, control, ui_condition, cap_condition


def typed_call(capability, **arguments):
    # Explicit fixture goal: exactly this draft in the known Composer field.
    assert capability == "ui.type"
    return dict(call(capability, **arguments), success_condition=ui_condition("Composer",arguments["text"]))


def scene(app='UnknownApp', role='input', text='', obs_id='before', **metadata):
    return UIObservation(observation_id=obs_id, device_id='asus', timestamp=datetime.now(timezone.utc).isoformat(),
        foreground_app=app, window_title=app, window_id=42, screen_size={'width': 1000, 'height': 800},
        summary='Observed interface', elements=[UIElement(element_id='el_1', role=role,
            label='Composer', text=text, bounds=[50, 50, 400, 100],
            metadata=dict({'runtime_id': [42, 9], 'editable': True, 'has_keyboard_focus': True}, **metadata))])


@pytest.fixture
def input_boundary(monkeypatch):
    observer = UIObserver.__new__(UIObserver)
    observer._observations_by_id = {}
    observer._latest_by_device = {}
    observer._history = []
    observer._uia_client = None
    observer._uia_module = None
    monkeypatch.setattr(UIObserver, '_instance', observer)
    monkeypatch.setattr(observer, 'is_observation_fresh', Mock(return_value=(True, 'Fresh')))
    monkeypatch.setattr(observer, 'focus_text_target', Mock(return_value=True))
    monkeypatch.setattr(observer, 'text_target_has_focus', Mock(return_value=True))
    executor = UIActionExecutor()
    monkeypatch.setattr(executor, '_ensure_desktop', Mock())
    monkeypatch.setattr(executor, '_send_unicode_text', Mock())
    monkeypatch.setattr(executor, '_send_vk_key', Mock())
    return observer, executor


@pytest.mark.parametrize('app,role', [('Discord', 'input'), ('Browser search', 'searchbox'),
    ('Unseen nebula editor', 'document'), ('Invented control panel', 'custom'), ('Notes', 'textbox')])
def test_identical_text_capability_across_apps(input_boundary, app, role):
    observer, executor = input_boundary
    before = scene(app, role)
    observer._store_observation(before)
    text = 'Draft: hello, 世界 👋. Keep punctuation!'
    args = {'observation_id': 'before', 'target': 'el_1', 'text': text}
    result = executor.type_text(args)
    assert result['status'] == 'success' and result['verified'] is False
    assert executor._send_unicode_text.call_args.args == (text,)
    executor._send_vk_key.assert_not_called()  # never submits automatically
    assert text not in str(result)
    after = scene(app, role, text=text, obs_id='after')
    after.elements[0].element_id = 'el_99'  # tree order can change
    assert verify_text_entry(before, after, args)


@pytest.mark.parametrize('failure', ['missing', 'ambiguous', 'readonly', 'disabled', 'not_editable',
    'stale', 'no_observation', 'wrong_device', 'unfocused', 'focus_changed', 'submit', 'secret_unavailable'])
def test_no_input_when_target_or_focus_is_unproven(input_boundary, failure):
    observer, executor = input_boundary
    obs = scene()
    args = {'observation_id': 'before', 'target': 'Composer', 'text': 'hello'}
    if failure == 'missing': args['target'] = 'does not exist'
    if failure == 'ambiguous': obs.elements.append(UIElement('el_2', 'input', 'Composer'))
    if failure == 'readonly': obs.elements[0].metadata['is_read_only'] = True
    if failure == 'disabled': obs.elements[0].enabled = False
    if failure == 'not_editable': obs.elements[0].metadata['editable'] = False
    if failure == 'stale': observer.is_observation_fresh.return_value = (False, 'changed')
    if failure == 'no_observation': args.pop('observation_id')
    if failure == 'wrong_device': args['target_device'] = 'other'
    if failure == 'unfocused': observer.focus_text_target.return_value = False
    if failure == 'focus_changed': observer.text_target_has_focus.return_value = False
    if failure == 'submit': args['press_enter'] = True
    if failure == 'secret_unavailable': args['secret_id'] = 'missing'
    observer._store_observation(obs)
    result = executor.type_text(args)
    assert result['status'] == 'failure'
    executor._send_unicode_text.assert_not_called()
    executor._send_vk_key.assert_not_called()


def test_input_failure_is_not_reported_as_success(input_boundary):
    observer, executor = input_boundary
    observer._store_observation(scene())
    executor._send_unicode_text.side_effect = RuntimeError('input_rejected')
    result = executor.type_text({'observation_id': 'before', 'target': 'el_1', 'text': 'Hello'})
    assert result['status'] == 'failure'
    assert result['data']['input_attempted'] is True


@pytest.mark.parametrize('case', ['wrong_field', 'wrong_window', 'wrong_device', 'label_only', 'unchanged', 'credential', 'missing'])
def test_verification_requires_value_in_same_field(case):
    before = scene()
    after = scene(text='hello', obs_id='after')
    args = {'target': 'el_1', 'text': 'hello'}
    if case == 'wrong_field': after.elements[0].metadata['runtime_id'] = [42, 10]
    if case == 'wrong_window': after.window_id = 99
    if case == 'wrong_device': after.device_id = 'other'
    if case == 'label_only': after.elements[0].label, after.elements[0].text = 'hello', ''
    if case == 'unchanged': after.elements[0].text = ''
    if case == 'credential': args['is_credential'] = True
    if case == 'missing': after = None
    result = UIActionVerifier.verify('ui.type', 'Composer', before, after, args)
    assert result.verified is False


def test_focused_field_and_exact_replace_contract():
    before = scene(text='old')
    assert resolve_text_target(before)[0] is before.elements[0]
    assert verify_text_entry(before, scene(text='old hello'), {'text': 'hello'})
    assert not verify_text_entry(before, scene(text='old hello'), {'text': 'hello', 'clear_existing': True})
    assert verify_text_entry(before, scene(text='hello'), {'text': 'hello', 'clear_existing': True})


def test_unicode_surrogate_pairs_and_sendinput_failure(monkeypatch):
    import ctypes
    import agent.ui_action_executor as module
    calls = []
    def send(count, pointer, size):
        event = ctypes.cast(pointer, ctypes.POINTER(module.INPUT)).contents
        calls.append((event.u.ki.wScan, event.u.ki.dwFlags))
        return 1
    monkeypatch.setattr(module.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(module.ctypes, 'windll', SimpleNamespace(user32=SimpleNamespace(SendInput=send)))
    UIActionExecutor()._send_unicode_text('A👋')
    assert [v for v, flags in calls[::2]] == [65, 0xD83D, 0xDC4B]
    monkeypatch.setattr(module.ctypes.windll.user32, 'SendInput', lambda *args: 0)
    with pytest.raises(RuntimeError, match='input_rejected'):
        UIActionExecutor()._send_unicode_text('hello')


@pytest.mark.asyncio
@pytest.mark.parametrize('app,role', [('Discord', 'input'), ('Browser search', 'searchbox'), ('NeverSeenEditor', 'custom')])
async def test_model_observe_type_observe_respond_without_scenario_plan(monkeypatch, app, role):
    monkeypatch.setenv('CORTEX_LEGACY_COMPATIBILITY', '0')
    before, after = scene(app, role), scene(app, role, text='Draft: hello!', obs_id='after')
    ctrl = control([{'status': 'success', 'data': before.to_dict()},
                    {'status': 'success', 'data': {'input_attempted': True}},
                    {'status': 'success', 'data': after.to_dict()}])
    model = ScriptedModel([call('ui.observe', include_vision=False),
        typed_call('ui.type', target='el_1', text='Draft: hello!', observation_id='before'),
        call('ui.observe', include_vision=False), {'decision': 'RESPOND', 'response_text': 'The draft is in the field.'}])
    result = await RequestPipeline(ctrl, model).execute('asus', 'Put my draft in that field, leaving it unsent.', {}, 'request-123')
    assert result.status == 'success', result
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list] == ['ui.observe', 'ui.type', 'ui.observe']
    sent_state = json.loads(model.calls[-1][1]['system_prompt'].split('\nSTATE:\n', 1)[1])
    assert sent_state['tool_results'][-1]['result']['text_entry_verified'] is True
    from hub.database import get_tasks
    saved = json.loads((await get_tasks())[0]['context'])
    assert saved['request_id'] == 'request-123'


@pytest.mark.asyncio
async def test_missing_target_can_reobserve_without_typing_elsewhere(monkeypatch):
    monkeypatch.setenv('CORTEX_LEGACY_COMPATIBILITY', '0')
    before, after = scene(), scene(text='hello', obs_id='after')
    ctrl = control([{'status': 'success', 'data': before.to_dict()},
                    {'status': 'success', 'data': before.to_dict()},
                    {'status': 'success'}, {'status': 'success', 'data': after.to_dict()}])
    model = ScriptedModel([call('ui.observe'), typed_call('ui.type', target='missing', text='hello', observation_id='before'),
        call('ui.observe'), typed_call('ui.type', target='el_1', text='hello', observation_id='before'),
        call('ui.observe'), {'decision': 'RESPOND', 'response_text': 'Entered the draft.'}])
    result = await RequestPipeline(ctrl, model).execute('asus', 'Draft hello in the composer.', {})
    assert result.status == 'success', result
    assert [c.args[1] for c in ctrl.execute_and_observe.await_args_list].count('ui.type') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('next_decision', [{'decision': 'RESPOND', 'response_text': 'Done'},
    dict(call('ui.keypress', key='enter', observation_id='after'),
         success_condition=[{'source':'ui','selector':{'label':'Composer'},'path':['text'],'operator':'equals','value':'hello'}],
         expected_effect=[{'source':'ui','selector':{'label':'Composer'},'operator':'absent'}])])
async def test_unverified_typing_blocks_false_success_and_submission(next_decision):
    ctrl = control([{'status': 'success', 'data': scene().to_dict()}, {'status': 'success'},
                    {'status': 'success', 'data': scene(obs_id='after').to_dict()},
                    *[{'status':'success','data':scene(obs_id='readback_'+str(i)).to_dict()} for i in range(3)]])
    model = ScriptedModel([call('ui.observe'), typed_call('ui.type', target='el_1', text='hello', observation_id='before'),
                           call('ui.observe'), *[next_decision]*4])
    result = await AgentTurn(ctrl, model).run('Draft hello', 'asus', {})
    assert result.status == 'error'
    assert sum(c.args[1]=='ui.type' for c in ctrl.execute_and_observe.await_args_list)==1
    assert all(c.args[1] in {'ui.observe','ui.type'} for c in ctrl.execute_and_observe.await_args_list)


@pytest.mark.asyncio
async def test_no_non_safety_phrase_interception(monkeypatch):
    monkeypatch.setenv('CORTEX_LEGACY_COMPATIBILITY', '0')
    ctrl = control([])
    model = ScriptedModel([{'decision': 'RESPOND', 'response_text': 'Available capabilities depend on the connected device.'}])
    result = await RequestPipeline(ctrl, model).execute('asus', 'What can you do?', {})
    assert result.status == 'success' and len(model.calls) == 1


@pytest.mark.asyncio
async def test_legacy_controller_has_no_scenario_recipe_or_blind_fallback():
    controller = ComputerUseTaskController()
    for goal in ['Type hello in Notepad', 'Change display to 60 Hz', 'Do something in an unknown app']:
        result = await controller.run_agent_loop(controller.create_task(goal))
        assert result['status'] == 'failure' and 'model runtime' in result['message']
    assert not hasattr(controller, 'decide_next_step')
    result = UIActionVerifier.verify_goal_completion('type hello in Notepad', None, scene('Notepad', text='hello'))
    assert result.verified is False


@pytest.mark.asyncio
async def test_accessibility_only_observation_never_silently_calls_vision(monkeypatch):
    observer = UIObserver.__new__(UIObserver)
    observer._observations_by_id, observer._latest_by_device, observer._history = {}, {}, []
    monkeypatch.setattr(observer, 'get_screen_size', lambda: {'width': 1000, 'height': 800})
    monkeypatch.setattr(observer, '_get_active_window_info', lambda *args: {'window_id': 42})
    monkeypatch.setattr(observer, '_extract_accessibility_elements', lambda *args: [])
    vision = AsyncMock(return_value=[])
    monkeypatch.setattr(observer, '_extract_vision_fallback_elements', vision)
    await observer.observe(include_vision=False)
    vision.assert_not_awaited()


def test_observer_reads_patterns_for_rich_fields_and_masks_password_values():
    observer = UIObserver.__new__(UIObserver)
    observer._uia_module = SimpleNamespace(IUIAutomationValuePattern='value', IUIAutomationTextPattern='text')
    value = SimpleNamespace(CurrentValue='private value', CurrentIsReadOnly=False)
    item = SimpleNamespace(CurrentControlType=50025, CurrentBoundingRectangle=SimpleNamespace(left=0, top=0, right=300, bottom=80),
        CurrentIsEnabled=True, CurrentIsPassword=False, CurrentName='Rich composer', CurrentHasKeyboardFocus=True,
        GetRuntimeId=lambda: [1, 2], GetCurrentPattern=lambda pattern: SimpleNamespace(QueryInterface=lambda _: value) if pattern == 10002 else None)
    element = observer._element_snapshot(item, 1)
    assert element.metadata['editable'] and element.text == 'private value'
    item.CurrentIsPassword = True
    protected = observer._element_snapshot(item, 1)
    assert 'private value' not in str(protected.to_dict())


def test_live_focus_uses_control_identity_and_rejects_changed_window(monkeypatch):
    observer = UIObserver.__new__(UIObserver)
    before = scene()
    focus = []
    item = SimpleNamespace(GetRuntimeId=lambda: [42, 9], SetFocus=lambda: focus.append('focused'))
    collection = SimpleNamespace(Length=1, GetElement=lambda _: item)
    observer._uia_client = SimpleNamespace(ElementFromHandle=lambda _: SimpleNamespace(FindAll=lambda *a: collection),
        CreateTrueCondition=lambda: None, GetFocusedElement=lambda: item)
    monkeypatch.setattr(observer, '_element_snapshot', lambda *args: before.elements[0])
    monkeypatch.setattr(observer, '_extract_accessibility_elements', lambda *args: before.elements)
    monkeypatch.setattr(observer, '_get_active_window_info', lambda: {'window_id': 99})
    assert not observer.focus_text_target(before, before.elements[0])
    assert focus == []
    monkeypatch.setattr(observer, '_get_active_window_info', lambda: {'window_id': 42})
    assert observer.focus_text_target(before, before.elements[0])
    assert focus == ['focused']
    item.GetRuntimeId = lambda: [42, 10]
    assert not observer.text_target_has_focus(before, before.elements[0])


def test_uia_client_is_not_reused_across_threads():
    from concurrent.futures import ThreadPoolExecutor
    observer = UIObserver.__new__(UIObserver)
    main_client = object()
    observer._uia_client = main_client
    def worker():
        assert observer._uia_client is None
        observer._uia_client = object()
        return observer._uia_client
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(worker).result() is not main_client
    assert observer._uia_client is main_client


@pytest.mark.asyncio
async def test_real_dispatch_control_and_executor_keep_typing_evidence(input_boundary, monkeypatch):
    from hub.control_layer import CortexControlLayer
    monkeypatch.setenv("CORTEX_ENABLE_HUB_DESKTOP", "1")
    monkeypatch.setenv("CORTEX_HUB_DEVICE_ID", "asus")
    observer, executor = input_boundary
    monkeypatch.setattr(UIActionExecutor, '_instance', executor)
    observations = iter([scene('UnseenComposer'), scene('UnseenComposer', text='hello', obs_id='after')])
    def observe(**kwargs):
        obs = next(observations)
        observer._store_observation(obs)
        return obs
    monkeypatch.setattr('agent.ui_observer.observe_ui_sync', observe)
    ctrl = control([])
    ctrl.execute_and_observe = CortexControlLayer().execute_and_observe
    model = ScriptedModel([call('ui.observe'), typed_call('ui.type', target='el_1', text='hello', observation_id='before'),
        call('ui.observe'), {'decision': 'RESPOND', 'response_text': 'Draft entered.'}])
    result = await AgentTurn(ctrl, model).run('Draft hello', 'asus', {})
    assert result.status == 'success', result
    executor._send_unicode_text.assert_called_once()
    # Typed failure codes survive the local control-layer adapter as well.
    failure = await ctrl.execute_and_observe('asus', 'ui.type', {'text': 'hi', 'observation_id': 'missing'})
    assert failure['error'] == 'observation_required'
    assert failure['data']['input_attempted'] is False


@pytest.mark.asyncio
async def test_remote_result_preserves_recoverable_failure_code():
    from hub.control_layer import CortexControlLayer
    from hub.models import ResultMessage
    from hub.main import PENDING_FUTURES
    layer = CortexControlLayer()
    layer.connected_devices['remote'] = object()
    async def execute(target, action, args, cmd_id=None):
        PENDING_FUTURES[cmd_id].set_result(ResultMessage(command=action, command_id=cmd_id,
            status='failure', error='stale_observation', message='Observe again', data={'input_attempted': False}))
    layer.execute_command = execute
    result = await layer.execute_and_observe('remote', 'ui.type', {'text': 'Hello'})
    assert result['error'] == 'stale_observation'
    assert result['data']['input_attempted'] is False


@pytest.mark.asyncio
async def test_intermediate_native_capability_can_continue_without_recipe():
    ctrl = control([{'status': 'success', 'data': {'opened': True}},
                    {'status': 'success', 'data': scene().to_dict()}])
    initial = dict(call('app.launch', app='Invented Editor'), continue_after_result=True,
                   success_condition=cap_condition("app.launch","opened",True))
    model = ScriptedModel([initial, call('ui.observe'), {'decision': 'ASK_CLARIFICATION',
        'response_text': 'Which field should I use?'}])
    result = await AgentTurn(ctrl, model).run('Open my editor and prepare a draft', 'asus', {})
    assert result.status == 'clarification_needed'
    assert ctrl.execute_and_observe.await_count == 2


@pytest.mark.asyncio
async def test_observation_from_another_device_cannot_authorize_input():
    obs = scene()
    obs.device_id = 'other'
    ctrl = control([{'status': 'success', 'data': obs.to_dict()}]*4)
    result = await AgentTurn(ctrl, ScriptedModel([call('ui.observe')])).run('Type hello', 'asus', {})
    assert result.status == 'error' and result.data['recovery_category']=='TARGET_CONTEXT_MISMATCH'
    assert ctrl.execute_and_observe.await_count == 4
    assert all(c.args[1]=='ui.observe' for c in ctrl.execute_and_observe.await_args_list)


@pytest.mark.parametrize('method,args', [('keypress', {'key': 'enter'}), ('hotkey', {'keys': ['ctrl', 'a']})])
def test_keys_cannot_go_to_a_different_foreground_window(input_boundary, monkeypatch, method, args):
    observer, executor = input_boundary
    observer._store_observation(scene())
    monkeypatch.setattr(observer, '_get_active_window_info', lambda: {'window_id': 99})
    result = getattr(executor, method)(dict(args, observation_id='before'))
    assert result['status'] == 'failure' and result['error'] == 'focus_changed'
    executor._send_vk_key.assert_not_called()
