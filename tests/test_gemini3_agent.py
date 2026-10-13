"""Gemini3Agent regressions with scripted responses and no live runtime/API."""

import copy
import importlib
import json
import os
import base64
import pickle
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image
from litellm.types.utils import ModelResponse
from litellm.utils import validate_and_fix_openai_messages
from litellm.llms.gemini.chat.transformation import GoogleAIStudioGeminiConfig

from agents.agents.claude_gemini import Gemini3Agent
from agents.evaluation import run_single
from agents.shared.qwen_computer_use import parse_qwen3vl_response


CLICK = '<tool_call>{"name":"computer_use","arguments":{"action":"click","coordinate":[650,305]}}</tool_call>'
TERMINATE = '<tool_call>{"name":"computer_use","arguments":{"action":"terminate","status":"success"}}</tool_call>'
MULTI = CLICK + '<tool_call>{"name":"computer_use","arguments":{"action":"type","text":"dotfiles"}}</tool_call>' + TERMINATE
# Public tool calls from the October 13 task 414 run's eleven-call response.
TASK414_MULTI = ''.join('<tool_call>' + json.dumps({'name': 'computer_use', 'arguments': call})
                       + '</tool_call>' for call in [
    {'action': 'click', 'coordinate': [650, 305]},
    {'action': 'type', 'text': 'dotfiles'},
    {'action': 'key', 'keys': ['Return']},
    {'action': 'click', 'coordinate': [356, 368]},
    {'action': 'click', 'coordinate': [454, 381]},
    {'action': 'click', 'coordinate': [410, 382]},
    {'action': 'click', 'coordinate': [410, 420]},
    {'action': 'scroll', 'pixels': 500},
    {'action': 'click', 'coordinate': [308, 622]},
    {'action': 'wait', 'time': 2},
    {'action': 'terminate', 'status': 'success'},
])


def response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, reasoning_content=None,
    ))])


def bare_agent():
    with mock.patch.object(Gemini3Agent, 'setup_custom_logger'):
        agent = Gemini3Agent(agent_args={'model': 'offline-model'}, verbose=False)
    agent.system_prompt = 'One tool call per response.'
    agent.save_observation = mock.Mock()
    agent.process_image = mock.Mock(side_effect=lambda path, **kwargs: path)
    agent.init('Add the MIT license.', (1920, 1080), 'unused')
    return agent


def test_standalone_termination_returns_no_empty_action_group():
    agent = bare_agent()
    with mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', return_value=response(TERMINATE)):
        groups = agent.step({'screen': {'path': 'initial.png'}}, [])
    assert groups == []
    assert agent.done is True


def test_multi_call_feedback_accompanies_next_screenshot():
    agent = bare_agent()
    requests = []

    def model(messages, *args, **kwargs):
        requests.append(copy.deepcopy(messages))
        return response(MULTI if len(requests) == 1 else TERMINATE)

    with mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', side_effect=model):
        groups = agent.step({'screen': {'path': 'initial.png'}}, [])
        assert groups[0]['actions'] == [{'mouse': {'left_click': [1248, 329]}}]
        assert agent.done is False
        assert agent.step({'screen': {'path': 'fresh.png'}}, []) == []
    content = requests[1][-1]['content']
    assert any('ignored 2' in part.get('text', '') for part in content)
    assert content[-1]['image_url']['url'].endswith('fresh.png')
    assert agent._tool_call_feedback is None


def provider_response(content, turn=0):
    result = ModelResponse(
        id=f'offline-response-{turn}', model='gemini/gemini-3.8-flash',
        choices=[{'index': 0, 'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': content,
            'reasoning_content': 'Offline reasoning metadata.',
            'provider_specific_fields': {'audit_tag': f'turn-{turn}'},
        }}],
        usage={'prompt_tokens': 120 + turn, 'completion_tokens': 24, 'total_tokens': 144 + turn},
    )
    result._hidden_params['response_cost'] = 0.001
    return result


@pytest.fixture
def png_b64():
    buffer = BytesIO()
    Image.new('RGB', (64, 64), 'white').save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode()


def provider_contents(messages):
    validated = validate_and_fix_openai_messages(copy.deepcopy(messages))
    return GoogleAIStudioGeminiConfig()._transform_messages(validated, model='gemini-3.8-flash')


def execution_records(messages):
    text = next(part['text'] for part in messages[-1]['content']
                if part.get('text', '').startswith('Previous action execution feedback'))
    return json.loads(text.split('\n', 1)[1])


def test_real_gemini_formatter_drops_nested_response_negative_control(png_b64):
    raw = provider_response(CLICK)
    messages = [
        {'role': 'user', 'content': 'Add the MIT license.'},
        {'role': 'assistant', 'content': raw},
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {
            'url': f'data:image/png;base64,{png_b64}',
        }}]},
    ]
    assert not any(turn['role'] == 'model' for turn in provider_contents(messages))
    messages[1]['content'] = raw.choices[0].message.content
    turns = [turn for turn in provider_contents(messages) if turn['role'] == 'model']
    assert turns == [{'role': 'model', 'parts': [{'text': CLICK}]}]


@pytest.mark.parametrize('multi_call,ignored', [(MULTI, 2), (TASK414_MULTI, 10)])
def test_multiturn_history_and_execution_feedback_reach_real_gemini_formatter(png_b64, multi_call, ignored):
    agent = bare_agent()
    agent.process_image.return_value = png_b64
    agent.process_image.side_effect = None
    type_call = '<tool_call>{"action":"type","text":"dotfiles"}</tool_call>'
    key_call = '<tool_call>{"action":"key","keys":["Return"]}</tool_call>'
    responses = [provider_response(content, i) for i, content in enumerate(
        [multi_call, type_call, key_call, TERMINATE]
    )]
    requests = []
    formatted = []

    def model(messages, *args, **kwargs):
        requests.append(copy.deepcopy(messages))
        formatted.append(provider_contents(messages))
        return responses[len(requests) - 1]

    outputs = []
    with mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', side_effect=model):
        for i in range(len(responses)):
            groups = agent.step({'screen': {'path': f'fresh-{i}.png'}}, outputs)
            if i == 0:
                assert groups[0]['actions'] == [{'mouse': {'left_click': [1248, 329]}}]
                assert not agent.done
            outputs = [{'tool_id': group['tool_id'], 'action': 'other',
                        'output': 'Executed the action', 'obs': {'screen': {'path': 'fresh.png'}}}
                       for group in groups]

    for i, contents in enumerate(formatted):
        model_turns = [turn for turn in contents if turn['role'] == 'model']
        assert [turn['parts'][0]['text'] for turn in model_turns] == [
            raw.choices[0].message.content for raw in responses[:i]
        ]
        json.dumps(requests[i])  # Entire outbound chat history is JSON serializable.
        assert all(isinstance(message['content'], str) for message in requests[i]
                   if message['role'] == 'assistant')

    click_result = execution_records(requests[1])[0]
    assert click_result['status'] == 'executed'
    assert click_result['submitted_actions'] == [{'mouse': {'left_click': [1248, 329]}}]
    assert 'dotfiles' not in json.dumps(click_result)
    assert 'terminate' not in json.dumps(click_result)
    user_text = '\n'.join(part.get('text', '') for part in requests[1][-1]['content'])
    assert f'ignored {ignored}' in user_text
    assert 'skipped tool calls were not executed' in user_text
    assert 'will execute' not in user_text
    provider_user_text = '\n'.join(part.get('text', '') for turn in formatted[1]
                                   if turn['role'] == 'user' for part in turn['parts'])
    assert user_text in provider_user_text
    assert execution_records(requests[2])[0]['submitted_actions'] == [{'keyboard': {'text': 'dotfiles'}}]
    assert execution_records(requests[3])[0]['submitted_actions'] == [{'keyboard': {'keys': ['Return']}}]
    assert groups == [] and agent.done
    assert all(saved is raw for saved, raw in zip(agent.raw_responses, responses))
    assert 'Offline reasoning metadata.' not in json.dumps(agent.messages)


def test_wait_action_and_feedback_keep_single_action_behavior(png_b64):
    agent = bare_agent()
    agent.process_image.side_effect = None
    agent.process_image.return_value = png_b64
    wait_call = '<tool_call>{"action":"wait","time":2}</tool_call>'
    with mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', side_effect=[
        provider_response(wait_call), provider_response(TERMINATE),
    ]):
        group = agent.step({'screen': {'path': 'initial.png'}}, [])[0]
        assert group['actions'] == [{'action': 'wait', 'time': 2}]
        assert not agent.done
        assert agent.step({'screen': {'path': 'after-wait.png'}}, [{
            'tool_id': group['tool_id'], 'action': 'wait', 'output': 'Waited for 2 seconds',
        }]) == []
    messages = agent.messages[:-1]
    result = execution_records(messages)[0]
    assert result['status'] == 'executed'
    assert result['submitted_actions'] == [{'action': 'wait', 'time': 2}]
    contents = provider_contents(messages)
    assert any('Waited for 2 seconds' in part.get('text', '')
               for turn in contents if turn['role'] == 'user' for part in turn['parts'])


@pytest.mark.parametrize('error_result', [
    {'is_error': True, 'output': 'Desktop injection failed'},
    {'error': 'Desktop injection failed'},
    {'exit_code': 1, 'output': 'Desktop injection failed'},
    {'return_code': 1, 'output': 'Desktop injection failed'},
    {'returncode': 1, 'output': 'Desktop injection failed'},
])
def test_execution_errors_reach_next_provider_request(png_b64, error_result):
    agent = bare_agent()
    agent.process_image.side_effect = None
    agent.process_image.return_value = png_b64
    requests = []

    def model(messages, *args, **kwargs):
        requests.append(copy.deepcopy(messages))
        return provider_response(CLICK if len(requests) == 1 else TERMINATE)

    with mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', side_effect=model):
        group = agent.step({'screen': {'path': 'initial.png'}}, [])[0]
        agent.step({'screen': {'path': 'after-error.png'}}, [
            {'tool_id': group['tool_id'], 'action': 'other', **error_result},
        ])
    record = execution_records(requests[1])[0]
    assert record['status'] == 'execution_error'
    assert record['submitted_actions'] == group['actions']
    assert error_result.items() <= record.items()
    contents = provider_contents(requests[1])
    assert any('Desktop injection failed' in part.get('text', '')
               for turn in contents if turn['role'] == 'user' for part in turn['parts'])


@pytest.mark.parametrize('outputs', [[], [{'tool_id': 'different-action', 'output': 'Executed the action'}]])
def test_missing_or_unmatched_results_do_not_claim_execution(outputs):
    agent = bare_agent()
    with mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', side_effect=[response(MULTI), response(TERMINATE)]):
        group = agent.step({'screen': {'path': 'initial.png'}}, [])[0]
        agent.step({'screen': {'path': 'fresh.png'}}, outputs)
    records = execution_records(agent.messages[:-1])
    assert records[-1]['status'] == 'execution_not_confirmed'
    assert records[-1]['submitted_actions'] == group['actions']
    assert all(record['status'] != 'executed' for record in records)


def test_screenshot_feedback_does_not_duplicate_image_payload():
    agent = bare_agent()
    with mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', side_effect=[response('invalid'), response(TERMINATE)]):
        group = agent.step({'screen': {'path': 'initial.png'}}, [])[0]
        agent.step({'screen': {'path': 'fresh.png'}}, [{
            'tool_id': group['tool_id'], 'action': 'screenshot',
            'output': 'large-encoded-image-payload', 'obs': {'screen': {'image': 'large-encoded-image-payload'}},
        }])
    feedback = execution_records(agent.messages[:-1])
    assert 'large-encoded-image-payload' not in json.dumps(feedback)
    assert feedback[0]['submitted_actions'] == [{'action': 'screenshot'}]
    assert any('No computer-use tool call found' in part.get('text', '')
               for part in agent.messages[-2]['content'])


@pytest.mark.parametrize('content,expected', [
    (CLICK, CLICK), (None, ''),
    ([{'type': 'text', 'text': 'Inspecting the filter.'}, {'type': 'text', 'text': CLICK}],
     'Inspecting the filter.\n' + CLICK),
])
def test_assistant_text_normalization(content, expected):
    assert Gemini3Agent._assistant_text(content) == expected


def test_assistant_text_rejects_response_objects():
    with pytest.raises(TypeError, match='assistant content must be text'):
        Gemini3Agent._assistant_text(provider_response(CLICK))


def test_finish_preserves_full_raw_responses_separately_from_chat_history(tmp_path):
    agent = bare_agent()
    agent.save_path = str(tmp_path / 'episode')
    agent.save_folder_custom = str(tmp_path / 'agent')
    os.makedirs(agent.save_path)
    os.makedirs(agent.save_folder_custom)
    raw = provider_response(TERMINATE)
    info = {'verifier': {'passed': False, 'score': 0}}
    with mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', return_value=raw):
        assert agent.step({'screen': {'path': 'initial.png'}}, []) == []
    agent.finish(info=info)
    for folder in (agent.save_path, agent.save_folder_custom):
        with open(os.path.join(folder, 'messages.pkl'), 'rb') as handle:
            messages = pickle.load(handle)
        with open(os.path.join(folder, 'raw_responses.pkl'), 'rb') as handle:
            saved = pickle.load(handle)
        assert messages[-1] == {'role': 'assistant', 'content': TERMINATE}
        assert isinstance(saved[0], ModelResponse)
        assert saved[0].model_dump() == raw.model_dump()
        assert saved[0]._hidden_params == raw._hidden_params
    with open(os.path.join(agent.save_folder_custom, 'info.pkl'), 'rb') as handle:
        assert pickle.load(handle) == info


def test_parse_problem_is_reported_to_model_without_termination():
    agent = bare_agent()
    with mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', return_value=response(TERMINATE + TERMINATE)):
        groups = agent.step({'screen': {'path': 'initial.png'}}, [])
    assert groups[0]['actions'] == [{'action': 'screenshot'}]
    assert agent.done is False
    assert 'No executable GUI action' in agent._tool_call_feedback


def test_benchmark_loop_refreshes_observation_and_finalizes_only_once(tmp_path):
    agent = bare_agent()
    agent.finish = mock.Mock()
    steps = []

    def env_step(actions, mark_done=False, **kwargs):
        steps.append((actions, mark_done))
        info = {'verifier': {'passed': False, 'score': 0}} if mark_done else {'action_result': {'action': 'other', 'output': 'clicked'}}
        return {'screen': {'path': 'after_click.png'}}, 0.0, mark_done, info

    env = SimpleNamespace(
        episode_dir=tmp_path, task_spec=SimpleNamespace(description='Add the MIT license.'),
        task_root=None, env_spec=SimpleNamespace(observation=[SimpleNamespace(resolution=(1920, 1080))]),
        max_steps=5, reset=mock.Mock(), set_episode_limits=mock.Mock(),
        capture_observation=mock.Mock(return_value={'screen': {'path': 'initial.png'}}),
        step=env_step, close=mock.Mock(),
    )
    args = SimpleNamespace(
        env_dir='unused', task='add_mit_license', seed=42, steps=5, agent='Gemini3Agent',
        agent_args=json.dumps({'model': 'offline-model'}), verbose=False, debug=False,
        use_cache=False, cache_level='pre_start', use_savevm=False,
        output_dir=str(tmp_path), vlm_backend='local', vlm_model='offline-model',
        vlm_base_url='http://unused',
    )
    with mock.patch.dict(os.environ), \
         mock.patch.object(run_single, '_make_env', return_value=env), \
         mock.patch.object(run_single, '_resolve_agent_class', return_value=lambda **kwargs: agent), \
         mock.patch('agents.agents.claude_gemini.call_gemini_with_retry', side_effect=[response(MULTI), response(TERMINATE)]):
        assert run_single.run_single(args) == 0
    assert steps == [([{'mouse': {'left_click': [1248, 329]}}], False), ([], True)]
    assert [call.args[0] for call in agent.process_image.call_args_list] == ['initial.png', 'after_click.png']
    env.close.assert_called_once()
    agent.finish.assert_called_once()


@pytest.mark.parametrize('module_name', [
    'claude_gemini', 'claude_gemini_qwen3', 'claude_gemini_qwen3_audit',
    'qwen3vl', 'qwen3vlfixed', 'qwen3vl_audit', 'kimi', 'kimi_distill',
])
def test_existing_agent_imports_use_the_same_parser(module_name):
    assert importlib.import_module('agents.agents.' + module_name).parse_qwen3vl_response is parse_qwen3vl_response


@pytest.mark.parametrize('module_name,class_name', [('qwen3vl', 'Qwen3VLAgent'), ('kimi', 'KimiAzureAgent')])
@pytest.mark.parametrize('content,expected', [
    (CLICK, [{'mouse': {'left_click': [1248, 329]}}]),
    ('<tool_call>{"action":"wait","time":2}</tool_call>', [{'action': 'wait', 'time': 2}]),
    (TERMINATE, []),
    (MULTI, [{'mouse': {'left_click': [1248, 329]}}]),
])
def test_shared_qwen_and_kimi_callers_keep_action_and_metadata_contract(module_name, class_name, content, expected):
    module = importlib.import_module('agents.agents.' + module_name)
    cls = getattr(module, class_name)
    with mock.patch.object(cls, 'setup_custom_logger'):
        agent = cls(agent_args={'model': 'offline-model'}, verbose=False)
    agent.save_observation = mock.Mock()
    agent.process_image = mock.Mock(return_value=('image', 'unused.png'))
    agent.build_messages = mock.Mock(return_value=[])
    agent.save_messages = mock.Mock()
    if module_name == 'qwen3vl':
        sampling = mock.patch.object(agent, 'llm_call', return_value=content)
    else:
        sampling = mock.patch.object(module, 'call_kimi_azure', return_value=content)
    with sampling:
        groups = agent.step({'screen': {'path': 'unused.png'}}, [])
    assert groups[0]['actions'] == expected
    assert agent.done is (content == TERMINATE)
