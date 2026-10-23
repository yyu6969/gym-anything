"""Offline regressions for Qwen3.5 tool parsing and execution provenance."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from agents.agents.qwen35vl import Qwen35ParseError, Qwen35VLAgent
from agents.shared import llm_clients


SAVED = json.loads((Path(__file__).parent / "fixtures/qwen35_saved_responses.json").read_text())


@pytest.fixture
def parser():
    return Qwen35VLAgent.__new__(Qwen35VLAgent)


def tool(action, **arguments):
    return '<tool_call>' + json.dumps({"name": "computer_use", "arguments": {"action": action, **arguments}}) + '</tool_call>'


def xml(arguments, *, name="computer_use"):
    body = "".join(f'<parameter={key}>\n{value if isinstance(value, str) else json.dumps(value)}\n</parameter>\n'
                   for key, value in arguments.items())
    return '<tool_call>\n' + (f'<function={name}>\n' if name is not None else '') + body + '</function>\n</tool_call>'


@pytest.mark.parametrize("fixture,expected,recovered", [
    (SAVED[0], [{"mouse": {"left_click": [1856, 134]}}], False),
    (SAVED[1], [{"mouse": {"left_click": [1614, 136]}}], True),
    (SAVED[2], [{"mouse": {"move": [840, 582]}}, {"mouse": {"scroll": 3}}], True),
], ids=[item["name"] for item in SAVED])
def test_saved_responses(parser, fixture, expected, recovered):
    response = fixture["response"]
    assert hashlib.sha256(response.encode()).hexdigest() == fixture["sha256"]
    result = parser._parse_response(response, *fixture["resolution"])
    assert result["actions"] == expected
    assert result["metadata"]["is_terminal"] is False
    assert not result["metadata"].get("parse_error")
    diagnostic = result["metadata"]["parsing"]
    assert diagnostic["recovered"] is recovered
    assert diagnostic["errors"] == []
    assert diagnostic["planned_actions"] == expected
    assert diagnostic["tool_calls"][0]["original_arguments"]["action"] in {"left_click", "mouse_move"}
    if recovered:
        call = diagnostic["tool_calls"][0]
        assert call["function_name"] is None
        assert call["recovery_reason"] == "missing_opening_function_declaration"
        assert call["recovered_call"]["function_name"] == "computer_use"
    else:
        assert diagnostic["tool_calls"][0]["recovered_call"] is None
    assert fixture["response"] == response  # No normalization of saved/model text.


@pytest.mark.parametrize("response", [
    tool("left_click", coordinate=[500, 500]),
    '<tool_call>{"action":"left_click","coordinate":[500,500]}</tool_call>',
    'Action: Click.\n{"name":"computer_use","arguments":{"action":"left_click","coordinate":[500,500]}}',
    xml({"action": "left_click", "coordinate": [500, 500]}),
])
def test_valid_formats_keep_same_actions(parser, response):
    result = parser._parse_response(response, 1920, 1080)
    assert result["actions"] == [{"mouse": {"left_click": [960, 540]}}]
    assert result["metadata"]["parsing"]["recovered"] is False


@pytest.mark.parametrize("action,arguments,expected", [
    ("click", {}, [{"mouse": {"buttons": {"left_down": True, "left_up": True}}}]),
    ("right_click", {}, [{"mouse": {"buttons": {"right_down": True, "right_up": True}}}]),
    ("middle_click", {"coordinate": [500, 500]}, [{"mouse": {"middle_click": [960, 540]}}]),
    ("double_click", {"coordinate": [500, 500]}, [{"mouse": {"double_click": [960, 540]}}]),
    ("triple_click", {"coordinate": [500, 500]}, [{"mouse": {"triple_click": [960, 540]}}]),
    ("drag", {"coordinate": [100, 200]}, [{"mouse": {"left_click_drag": [[192, 216], [192, 216]]}}]),
    ("left_click_drag", {"coordinate": [100, 200], "coordinate2": [500, 500]},
     [{"mouse": {"left_click_drag": [[192, 216], [960, 540]]}}]),
    ("key", {"keys": "Return"}, [{"keyboard": {"keys": ["Return"]}}]),
    ("key", {"keys": "0"}, [{"keyboard": {"keys": ["0"]}}]),
    ("key", {"keys": "["}, [{"keyboard": {"keys": ["["]}}]),
    ("key", {"keys": '"'}, [{"keyboard": {"keys": ['"']}}]),
    ("key", {"keys": '["ctrl","a"]'}, [{"keyboard": {"keys": ["ctrl", "a"]}}]),
    ("type", {"text": " café\n漢字  "}, [{"keyboard": {"text": " café\n漢字  "}}]),
    ("scroll", {"pixels": -100}, [{"mouse": {"scroll": 10}}]),
    ("scroll", {"pixels": 3}, [{"mouse": {"scroll": -3}}]),
    ("left_click", {"coordinate": [500, 500], "keys": ["shift"]},
     [{"keyboard": {"keys_down": ["shift"]}}, {"mouse": {"left_click": [960, 540]}}, {"keyboard": {"keys_up": ["shift"]}}]),
])
def test_existing_action_translation(parser, action, arguments, expected):
    assert parser._parse_response(tool(action, **arguments), 1920, 1080)["actions"] == expected


@pytest.mark.parametrize("action,arguments,expected,intent", [
    ("wait", {}, [{"action": "wait", "time": 1.0}], "wait"),
    ("wait", {"time": 0}, [{"action": "wait", "time": 0.0}], "wait"),
    ("wait", {"time": 2.5}, [{"action": "wait", "time": 2.5}], "wait"),
    ("screenshot", {}, [{"action": "screenshot"}], "screenshot"),
    ("noop", {}, [{"action": "screenshot"}], "noop"),
    ("scroll", {"pixels": 0}, [{"action": "screenshot"}], "noop"),
    ("type", {"text": ""}, [{"keyboard": {"text": ""}}], "noop"),
    ("terminate", {"status": "failure"}, [], "terminal"),
    ("answer", {"text": "Done"}, [], "terminal"),
])
def test_intentional_control_actions_are_not_parse_failures(parser, action, arguments, expected, intent):
    result = parser._parse_response(tool(action, **arguments), 1920, 1080)
    assert result["actions"] == expected
    assert result["metadata"]["parsing"]["intent"] == intent
    assert not result["metadata"].get("parse_error")


@pytest.mark.parametrize("arguments", [
    {"action": "launch_missiles"}, {"action": "key", "keys": []}, {"action": "key", "keys": [3]},
    {"action": "key", "keys": [" "]}, {"action": "key", "keys": "[broken"},
    {"action": "type", "text": 42}, {"action": "type"},
    {"action": "left_click", "coordinate": []}, {"action": "left_click", "coordinate": [1]},
    {"action": "left_click", "coordinate": [1, 2, 3]}, {"action": "left_click", "coordinate": [True, 2]},
    {"action": "left_click", "coordinate": ["one", 2]}, {"action": "left_click", "coordinate": [float("nan"), 2]},
    {"action": "left_click", "coordinate": [float("inf"), 2]}, {"action": "left_click", "coordinate": [-1, 2]},
    {"action": "left_click", "coordinate": [1000, 2]}, {"action": "left_click", "coordinate": {}},
    {"action": "mouse_move"}, {"action": "scroll"}, {"action": "scroll", "pixels": "many"},
    {"action": "scroll", "pixels": False}, {"action": "scroll", "pixels": 0.5},
    {"action": "wait", "time": -1}, {"action": "wait", "time": float("inf")},
    {"action": "terminate", "status": "maybe"}, {"action": "answer", "text": None},
    {"action": "left_click", "command": "not a GUI argument"},
])
def test_invalid_arguments_are_rejected_for_json_and_recovery(parser, arguments):
    responses = [tool(arguments["action"], **{k: v for k, v in arguments.items() if k != "action"})]
    # XML parameter bodies are strings: text "42" and "null" are valid text,
    # unlike the corresponding non-string values in a JSON argument object.
    if "text" not in arguments or isinstance(arguments["text"], str):
        responses.append(xml(arguments, name=None))
    for response in responses:
        with pytest.raises(Qwen35ParseError) as caught:
            parser._parse_response(response, 1920, 1080)
        assert caught.value.parsed_response["actions"] == []
        assert caught.value.parsing_diagnostics["execution_status"] == "rejected"
        assert caught.value.parsing_diagnostics["errors"]


@pytest.mark.parametrize("response", [
    None, "", "Action: Click the avatar.", '<tool_call>not a call</tool_call>',
    '<tool_call>{broken}</tool_call>', '<tool_call>{"action":"wait","time":0}',
    '<tool_call><function=computer_use><parameter=action>wait</parameter></tool_call>',
    '<tool_call><parameter=action>wait</parameter></tool_call>',
    '<tool_call><parameter=action>wait</parameter><parameter=action>type</parameter></function></tool_call>',
    '<tool_call>{"action":"wait","action":"terminate"}</tool_call>',
    '<tool_call>{"name":"other","action":"wait","arguments":{"action":"wait"}}</tool_call>',
    xml({"action": "wait"}, name="other"),
])
def test_unrecoverable_calls_raise_with_original_text(parser, response):
    with pytest.raises(Qwen35ParseError) as caught:
        parser._parse_response(response, 1920, 1080)
    assert caught.value.response_text == (response or "")
    assert caught.value.parsed_response["metadata"]["parse_error"] is True


@pytest.mark.parametrize("tools", [[], [{"type": "function", "function": {"name": "other"}}],
                                      [Qwen35VLAgent._tools_def(), Qwen35VLAgent._tools_def()]])
def test_recovery_requires_unambiguous_compatible_tool(parser, monkeypatch, tools):
    monkeypatch.setattr(parser, "_tools_def", lambda: tools)
    with pytest.raises(Qwen35ParseError, match="ambiguous"):
        parser._parse_response(SAVED[1]["response"], 1680, 1050)


def test_recovery_validates_configured_schema(parser, monkeypatch):
    definition = Qwen35VLAgent._tools_def()
    definition["function"]["parameters"]["required"].append("text")
    monkeypatch.setattr(parser, "_tools_def", lambda: definition)
    with pytest.raises(Qwen35ParseError, match="configured tool schema"):
        parser._parse_response(SAVED[1]["response"], 1680, 1050)


@pytest.mark.parametrize("invalid_first", [True, False])
def test_multiple_calls_reject_whole_turn_on_invalid_sibling(parser, invalid_first):
    valid, invalid = tool("left_click", coordinate=[500, 500]), tool("key", keys=[])
    response = invalid + valid if invalid_first else valid + invalid
    with pytest.raises(Qwen35ParseError) as caught:
        parser._parse_response(response, 1920, 1080)
    assert len(caught.value.parsing_diagnostics["tool_calls"]) == 2
    assert caught.value.parsed_response["actions"] == []


@pytest.mark.parametrize("bad_tag", ['<tool_call', '</tool_call>', '<tool_call >', '<tool_call><tool_call>'])
def test_malformed_sibling_tags_cannot_be_ignored(parser, bad_tag):
    for response in (bad_tag + tool("wait", time=0), tool("wait", time=0) + bad_tag):
        with pytest.raises(Qwen35ParseError):
            parser._parse_response(response, 1920, 1080)


def test_multiple_valid_calls_keep_waits_and_order(parser):
    response = tool("left_click", coordinate=[500, 500]) + tool("wait", time=0) + tool("key", keys=["Return"])
    assert parser._parse_response(response, 1920, 1080)["actions"] == [
        {"mouse": {"left_click": [960, 540]}}, {"action": "wait", "time": 0.0}, {"keyboard": {"keys": ["Return"]}},
    ]
    with pytest.raises(Qwen35ParseError, match="final tool call"):
        parser._parse_response(tool("terminate") + tool("wait", time=0), 1920, 1080)


def test_mixed_framed_and_unframed_calls_are_rejected(parser):
    unframed = json.dumps({"name": "other", "arguments": {"action": "wait"}})
    for response in (tool("wait", time=0) + unframed, unframed + tool("wait", time=0)):
        with pytest.raises(Qwen35ParseError):
            parser._parse_response(response, 1920, 1080)


def test_reasoning_calls_are_not_executable(parser):
    response = '<think>' + tool("unknown") + '</think>\n' + tool("wait", time=0)
    result = parser._parse_response(response, 1920, 1080)
    assert result["actions"] == [{"action": "wait", "time": 0.0}]
    assert len(result["metadata"]["parsing"]["tool_calls"]) == 1


def test_literal_tags_and_whitespace_in_typed_text_are_preserved(parser):
    text = '  <tool_call>{"name":"other"}</tool_call>\n[hello]\r\n  '
    for response in (tool("type", text=text), xml({"action": "type", "text": text}),
                     json.dumps({"name": "computer_use", "arguments": {"action": "type", "text": text}})):
        assert parser._parse_response(response, 1920, 1080)["actions"] == [{"keyboard": {"text": text}}]


@pytest.fixture
def offline_harbor(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    from gym_anything.integrations.harbor.agent import CuaWorldAgent
    monkeypatch.setenv("GYM_ANYTHING_OUTPUT_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("VLM_RETAIN_RESPONSE_METADATA", raising=False)
    screen = tmp_path / "screen.png"
    Image.new("RGB", (1680, 1050), "white").save(screen)
    obs = {"screen": {"path": str(screen), "resolution": (1680, 1050)}}
    env = SimpleNamespace(capture_observation=Mock(return_value=obs), step=Mock(return_value=(obs, 0, False, {})))
    host = CuaWorldAgent(logs_dir=tmp_path / "agent", model_name="qwen3.5-plus", reference_agent="Qwen35VLAgent", max_steps=1)
    async def in_process(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", in_process)
    return host, SimpleNamespace(gym_env=env, environment_name="offline-qwen35"), env, tmp_path


@pytest.mark.parametrize("fixture", SAVED, ids=[item["name"] for item in SAVED])
def test_atif_records_original_response_recovery_and_executed_actions(offline_harbor, monkeypatch, fixture):
    from harbor.models.trajectories import Trajectory
    host, environment, env, tmp_path = offline_harbor
    # Use each saved run's original resolution without starting any guest.
    screen = Path(env.capture_observation.return_value["screen"]["path"])
    Image.new("RGB", tuple(fixture["resolution"]), "white").save(screen)
    env.capture_observation.return_value["screen"]["resolution"] = tuple(fixture["resolution"])
    response = Mock(return_value=fixture["response"])
    monkeypatch.setattr(Qwen35VLAgent, "llm_call", staticmethod(response))
    asyncio.run(host.run("Offline fixture", environment, SimpleNamespace()))
    response.assert_called_once()
    raw = json.loads((tmp_path / "agent/trajectory.json").read_text())
    Trajectory.model_validate(raw)
    step = raw["steps"][0]
    assert step["message"] == fixture["response"]
    call = step["tool_calls"][0]
    assert call["arguments"]["actions"] == env.step.call_args.args[0]
    assert call["extra"]["execution"] == {"status": "completed", "executed_actions": call["arguments"]["actions"]}
    assert call["extra"]["parsing"]["recovered"] is (fixture["name"] != "task448_step1")
    saved = json.loads((tmp_path / "agent/responses.json").read_text())
    assert saved["model_responses"] == [fixture["response"]]
    assert saved["parsed_responses"][0]["metadata"]["parsing"] == call["extra"]["parsing"]


@pytest.mark.parametrize("response", [tool("unknown"), SAVED[0]["response"] + tool("key", keys=[]),
                                      SAVED[1]["response"] + tool("key", keys=[])])
def test_unrecoverable_turn_never_reaches_environment_and_is_saved(offline_harbor, monkeypatch, response):
    from harbor.models.trajectories import Trajectory
    host, environment, env, tmp_path = offline_harbor
    model = Mock(return_value=response)
    monkeypatch.setattr(Qwen35VLAgent, "llm_call", staticmethod(model))
    with pytest.raises(Qwen35ParseError):
        asyncio.run(host.run("Offline invalid fixture", environment, SimpleNamespace()))
    model.assert_called_once()
    env.step.assert_not_called()
    raw = json.loads((tmp_path / "agent/trajectory.json").read_text())
    Trajectory.model_validate(raw)
    step = raw["steps"][0]
    assert step["message"] == response and "tool_calls" not in step
    assert step["extra"]["parsing"]["errors"]
    assert step["extra"]["execution"] == {"status": "rejected", "executed_actions": []}
    saved = json.loads((tmp_path / "agent/responses.json").read_text())
    assert saved["model_responses"] == [response]
    assert saved["parsed_responses"][0]["metadata"]["parse_error"] is True


@pytest.mark.parametrize("response,expected,intent", [
    (tool("wait", time=0), [{"action": "wait", "time": 0.0}], "wait"),
    (tool("screenshot"), [{"action": "screenshot"}], "screenshot"),
    (tool("noop"), [{"action": "screenshot"}], "noop"),
    (tool("wait", time=0) + tool("left_click", coordinate=[500, 500]),
     [{"action": "wait", "time": 0.0}, {"mouse": {"left_click": [840, 525]}}], "actions"),
])
def test_runtime_preserves_observation_controls_and_mixed_waits(offline_harbor, monkeypatch, response, expected, intent):
    host, environment, env, tmp_path = offline_harbor
    model = Mock(return_value=response)
    monkeypatch.setattr(Qwen35VLAgent, "llm_call", staticmethod(model))
    asyncio.run(host.run("Offline control fixture", environment, SimpleNamespace()))
    model.assert_called_once()
    env.step.assert_called_once_with(expected)
    call = json.loads((tmp_path / "agent/trajectory.json").read_text())["steps"][0]["tool_calls"][0]
    assert call["extra"]["parsing"]["intent"] == intent
    assert call["extra"]["execution"]["executed_actions"] == expected


@pytest.mark.parametrize("retain", [False, True])
def test_provider_metadata_is_optional_allowlisted_and_never_executes_native_calls(offline_harbor, monkeypatch, caplog, retain):
    host, environment, env, tmp_path = offline_harbor
    if retain:
        monkeypatch.setenv("VLM_RETAIN_RESPONSE_METADATA", "1")
    secret = "PROVIDER_SECRET_DO_NOT_PERSIST"
    message = SimpleNamespace(content=SAVED[1]["response"], tool_calls=[{"function": {"name": "other", "arguments": secret}}],
                              function_call=None, reasoning_content=secret, provider_headers={"Authorization": secret})
    completion = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20), headers={"api-key": secret})
    create = Mock(return_value=completion)
    monkeypatch.setattr(llm_clients.openai, "OpenAI", Mock(return_value=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))))
    with caplog.at_level(logging.DEBUG, logger=llm_clients.__name__):
        asyncio.run(host.run("Offline provider fixture", environment, SimpleNamespace()))
    create.assert_called_once()
    saved_text = (tmp_path / "agent/responses.json").read_text()
    trajectory_text = (tmp_path / "agent/trajectory.json").read_text()
    assert secret not in saved_text + trajectory_text + caplog.text
    parsing = json.loads(saved_text)["parsed_responses"][0]["metadata"]["parsing"]
    if retain:
        metadata = parsing["provider_metadata"]
        assert metadata["native_tool_calls_field_present"]
        assert metadata["native_tool_calls_present"] and metadata["native_tool_call_count"] == 1
        assert metadata["finish_reason"] == "tool_calls" and metadata["reasoning_present"]
        assert metadata["prompt_tokens"] == 10
    else:
        assert "provider_metadata" not in parsing
    assert env.step.call_args.args[0] == [{"mouse": {"left_click": [1614, 136]}}]


@pytest.mark.parametrize("content", [None, ""])
def test_native_only_or_empty_response_fails_once_with_provider_provenance(offline_harbor, monkeypatch, content):
    host, environment, env, tmp_path = offline_harbor
    monkeypatch.setenv("VLM_RETAIN_RESPONSE_METADATA", "1")
    message = SimpleNamespace(content=content, tool_calls=[SimpleNamespace(function=SimpleNamespace(name="computer_use"))])
    create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")]))
    monkeypatch.setattr(llm_clients.openai, "OpenAI", Mock(return_value=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))))
    with pytest.raises(Qwen35ParseError):
        asyncio.run(host.run("Offline native-only fixture", environment, SimpleNamespace()))
    create.assert_called_once()
    env.step.assert_not_called()
    saved = json.loads((tmp_path / "agent/responses.json").read_text())
    assert saved["model_responses"] == [content]
    diagnostic = saved["parsed_responses"][0]["metadata"]["parsing"]
    assert diagnostic["provider_metadata"]["native_tool_calls_present"]
    trajectory = json.loads((tmp_path / "agent/trajectory.json").read_text())
    assert trajectory["steps"][0]["extra"]["execution"]["status"] == "rejected"


@pytest.mark.parametrize("field_value,present,populated", [
    ("absent", False, False), (None, True, False), ([], True, False),
    ([{"id": "offline-call", "type": "function", "function": {"name": "computer_use", "arguments": "sensitive"}}], True, True),
])
def test_sdk_native_field_presence_and_capture_scope(monkeypatch, field_value, present, populated):
    monkeypatch.setenv("VLM_RETAIN_RESPONSE_METADATA", "1")
    message = {"role": "assistant", "content": "tool text"}
    if field_value != "absent":
        message["tool_calls"] = field_value
    completion = llm_clients.openai.types.chat.ChatCompletion.model_validate({
        "id": "offline-completion", "object": "chat.completion", "created": 0, "model": "qwen3.5-plus",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    })
    with llm_clients.capture_provider_metadata() as metadata:
        llm_clients._retain_provider_metadata(completion)
        assert metadata["native_tool_calls_field_present"] is present
        assert metadata["native_tool_calls_present"] is populated
        with llm_clients.capture_provider_metadata() as next_metadata:
            assert next_metadata == {}
            llm_clients._retain_provider_metadata(completion)
            assert next_metadata == metadata and next_metadata is not metadata
    before = dict(metadata)
    llm_clients._retain_provider_metadata(completion)
    assert metadata == before
    monkeypatch.setenv("VLM_RETAIN_RESPONSE_METADATA", "0")
    with llm_clients.capture_provider_metadata() as disabled:
        llm_clients._retain_provider_metadata(completion)
        assert disabled == {}
