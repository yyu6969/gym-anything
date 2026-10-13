"""Offline regressions for the shared text-embedded computer-use protocol."""

import json
import logging

import pytest

from agents.shared.qwen_computer_use import parse_qwen3vl_response


def tool(action, **arguments):
    return '<tool_call>' + json.dumps({
        'name': 'computer_use', 'arguments': {'action': action, **arguments},
    }) + '</tool_call>'


@pytest.mark.parametrize('action,arguments,expected', [
    ('click', {'coordinate': [650, 304]}, [{'mouse': {'left_click': [1248, 328]}}]),
    ('left_click', {'coordinate': [500, 500]}, [{'mouse': {'left_click': [960, 540]}}]),
    ('right_click', {'coordinate': [500, 500]}, [{'mouse': {'right_click': [960, 540]}}]),
    ('double_click', {'coordinate': [500, 500]}, [{'mouse': {'double_click': [960, 540]}}]),
    ('triple_click', {'coordinate': [500, 500]}, [{'mouse': {'triple_click': [960, 540]}}]),
    ('mouse_move', {'coordinate': [500, 500]}, [{'mouse': {'move': [960, 540]}}]),
    ('drag', {'coordinate': [100, 200], 'coordinate2': [500, 500]},
     [{'mouse': {'left_click_drag': [[192, 216], [960, 540]]}}]),
    ('type', {'text': 'dotfiles'}, [{'keyboard': {'text': 'dotfiles'}}]),
    ('type', {'text': 'hello', 'clear': True, 'enter': True},
     [{'keyboard': {'keys': ['ctrl', 'a']}}, {'keyboard': {'text': 'hello'}},
      {'keyboard': {'keys': ['Return']}}]),
    ('key', {'keys': ['ctrl', 'shift', 's']}, [{'keyboard': {'keys': ['ctrl', 'shift', 's']}}]),
    ('key', {'keys': 'Return'}, [{'keyboard': {'keys': 'Return'}}]),
    ('scroll', {'coordinate': [500, 500], 'pixels': -500},
     [{'mouse': {'move': [960, 540]}}, {'mouse': {'scroll': -500}}]),
    ('scroll', {'scroll': 100}, [{'mouse': {'scroll': 100}}]),
])
def test_single_gui_call_preserves_translation(action, arguments, expected):
    result = parse_qwen3vl_response(tool(action, **arguments))
    assert result['actions'] == expected
    assert result['metadata']['action_type'] == action
    assert result['metadata']['is_terminal'] is False
    assert result['metadata']['wait_time'] is None
    assert 'parse_error' not in result['metadata']
    assert 'tool_call_count' not in result['metadata']


@pytest.mark.parametrize('arguments,status', [({}, 'success'), ({'status': 'success'}, 'success'),
                                             ({'status': 'failure'}, 'failure')])
def test_standalone_terminate_preserves_status(arguments, status):
    result = parse_qwen3vl_response(tool('terminate', **arguments))
    assert result['actions'] == []
    assert result['metadata']['is_terminal'] is True
    assert result['metadata']['status'] == status


@pytest.mark.parametrize('arguments,duration', [({}, 1.0), ({'time': 2.5}, 2.5)])
def test_standalone_wait_preserves_caller_contract(arguments, duration):
    result = parse_qwen3vl_response(tool('wait', **arguments))
    assert result['actions'] == []
    assert result['metadata']['wait_time'] == duration
    assert result['metadata']['is_terminal'] is False


@pytest.mark.parametrize('kwargs,expected', [
    ({'scale_dims': False}, [650, 305]),
    ({'scale_dims_ratio': (1, 2)}, [650, 610]),
    ({}, [1248, 329]),
])
def test_coordinate_scaling(kwargs, expected):
    assert parse_qwen3vl_response(tool('click', coordinate=[650, 305]), **kwargs)['actions'] == [
        {'mouse': {'left_click': expected}},
    ]


@pytest.mark.parametrize('trailing_terminate', [False, True])
def test_multiple_calls_execute_only_first_gui_call(trailing_terminate, caplog):
    response = tool('type', text='dotfiles', clear=True, enter=True) + tool('click', coordinate=[500, 500])
    if trailing_terminate:
        response += tool('terminate', status='success')
    with caplog.at_level(logging.WARNING):
        result = parse_qwen3vl_response(response)
    assert result['actions'] == [
        {'keyboard': {'keys': ['ctrl', 'a']}}, {'keyboard': {'text': 'dotfiles'}},
        {'keyboard': {'keys': ['Return']}},
    ]
    assert result['metadata']['is_terminal'] is False
    assert 'status' not in result['metadata']
    count = 3 if trailing_terminate else 2
    assert result['metadata']['tool_call_count'] == count
    assert result['metadata']['ignored_tool_calls'] == count - 1
    assert f'Detected {count} tool calls; ignored {count - 1}' in caplog.text
    assert 'fresh screenshot' in result['metadata']['tool_call_warning']


def test_october_13_task414_sequence_selects_first_click():
    # Public tool-call content from the saved run; screenshots/reasoning and
    # machine-specific pickle files are deliberately not test dependencies.
    calls = [
        tool('click', coordinate=[650, 305]), tool('type', text='dotfiles'),
        tool('key', keys=['Return']), tool('click', coordinate=[355, 368]),
        tool('click', coordinate=[640, 248]), tool('click', coordinate=[450, 400]),
        tool('click', coordinate=[380, 435]),
        tool('scroll', coordinate=[500, 500], pixels=500),
        tool('scroll', coordinate=[500, 500], pixels=500),
        tool('click', coordinate=[310, 680]), tool('wait', time=2),
        tool('terminate', status='success'),
    ]
    result = parse_qwen3vl_response('\n'.join(calls))
    assert result['actions'] == [{'mouse': {'left_click': [1248, 329]}}]
    assert result['metadata']['is_terminal'] is False
    assert result['metadata']['tool_call_count'] == 12
    assert result['metadata']['ignored_tool_calls'] == 11


def test_first_valid_gui_call_skips_malformed_wait_and_terminate():
    response = ('<tool_call>not JSON</tool_call>' + tool('wait', time=2)
                + tool('terminate') + tool('click', coordinate=[500, 500]) + tool('type', text='ignored'))
    result = parse_qwen3vl_response(response)
    assert result['actions'] == [{'mouse': {'left_click': [960, 540]}}]
    assert result['metadata']['is_terminal'] is False
    assert result['metadata']['ignored_tool_calls'] == 4


@pytest.mark.parametrize('response', [
    tool('terminate') + tool('terminate', status='failure'),
    tool('wait', time=2) + tool('terminate'),
    '<tool_call>not JSON</tool_call>' + tool('terminate'),
    '<tool_call>{broken' + tool('terminate'),
    tool('terminate') + '<tool_call>{broken',
    '<tool_call ' + tool('terminate'),
    tool('terminate') + '<tool_call',
    tool('click', coordinate=[1]) + tool('terminate'),
    tool('type', text='') + tool('terminate'),
    '<tool_call >{"action":"click","coordinate":[500,500]}</tool_call>' + tool('terminate'),
])
def test_multiple_calls_without_gui_action_report_problem(response, caplog):
    with caplog.at_level(logging.WARNING):
        result = parse_qwen3vl_response(response)
    assert result['actions'] == [{'action': 'screenshot'}]
    assert result['metadata']['is_terminal'] is False
    assert result['metadata']['parse_error'] is True
    assert result['metadata']['ignored_tool_calls'] == 2
    assert 'No executable GUI action' in result['metadata']['conclusion']
    assert 'ignored 2' in caplog.text


@pytest.mark.parametrize('response', [
    None, '', 'no tool call', '\x00' * 100,
    '<tool_call>not JSON</tool_call>', '<tool_call>[]</tool_call>',
    '<tool_call>{"arguments": null}</tool_call>',
    '<tool_call>{"name": "other", "arguments": {"action": "terminate"}}</tool_call>',
    tool('unknown'), tool('click', coordinate=[1]), tool('click', coordinate=['x', 2]),
    tool('click', coordinate=[float('nan'), 2]), tool('type', text=42), tool('key', keys=[]),
    tool('drag', coordinate=[1, 2]), tool('scroll', pixels='many'), tool('wait', time=-1),
    tool('terminate', status='maybe'), tool('terminate', status={}),
    '<tool_call>{"action": "terminate"}',
    '<tool_call >{"action": "terminate"}</tool_call>',
    tool('terminate') + '</tool_call>',
    '{"name": "computer_use", "arguments": {"action": "terminate"}',
])
def test_invalid_calls_retry_with_screenshot_without_terminating(response):
    result = parse_qwen3vl_response(response)
    assert result['actions'] == [{'action': 'screenshot'}]
    assert result['metadata']['is_terminal'] is False
    assert result['metadata']['parse_error'] is True
    assert 'Parse error:' in result['metadata']['conclusion']


def test_legacy_untagged_json_and_direct_arguments_remain_supported():
    assert parse_qwen3vl_response('{"name":"computer_use","arguments":{"action":"click","coordinate":[500,500]}}')['actions'] == [
        {'mouse': {'left_click': [960, 540]}},
    ]
    assert parse_qwen3vl_response('<tool_call>{"action":"terminate","status":"failure"}</tool_call>')['metadata']['status'] == 'failure'


def test_multiple_untagged_calls_cannot_bypass_termination_guard():
    response = '\n'.join(json.dumps({'name': 'computer_use', 'arguments': args}) for args in [
        {'action': 'click', 'coordinate': [500, 500]}, {'action': 'terminate', 'status': 'success'},
    ])
    result = parse_qwen3vl_response(response)
    assert result['actions'] == [{'mouse': {'left_click': [960, 540]}}]
    assert result['metadata']['is_terminal'] is False


def test_thinking_examples_are_not_counted_as_executable_calls():
    result = parse_qwen3vl_response('<think>' + tool('terminate') + '</think>' + tool('click', coordinate=[500, 500]))
    assert result['actions'] == [{'mouse': {'left_click': [960, 540]}}]
    assert 'tool_call_count' not in result['metadata']


def test_literal_tool_tags_in_typed_text_are_not_extra_calls():
    text = '<tool_call>{"action":"terminate"}</tool_call>'
    result = parse_qwen3vl_response(tool('type', text=text))
    assert result['actions'] == [{'keyboard': {'text': text}}]
    assert result['metadata']['is_terminal'] is False
    assert 'tool_call_count' not in result['metadata']


def test_extra_closing_tag_does_not_hide_multiple_calls(caplog):
    with caplog.at_level(logging.WARNING):
        result = parse_qwen3vl_response(tool('click', coordinate=[500, 500]) + '</tool_call>' + tool('terminate'))
    assert result['actions'] == [{'mouse': {'left_click': [960, 540]}}]
    assert result['metadata']['is_terminal'] is False
    assert result['metadata']['tool_call_count'] == 2
    assert 'ignored 1' in caplog.text


def test_incomplete_opening_tag_does_not_hide_first_valid_gui_call():
    response = '<tool_call ' + tool('click', coordinate=[500, 500]) + tool('terminate')
    result = parse_qwen3vl_response(response)
    assert result['actions'] == [{'mouse': {'left_click': [960, 540]}}]
    assert result['metadata']['tool_call_count'] == 3
    assert result['metadata']['ignored_tool_calls'] == 2
    assert result['metadata']['is_terminal'] is False
