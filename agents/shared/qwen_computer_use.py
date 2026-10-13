"""Qwen3-VL / Kimi computer-use shared logic (system prompt + response parser).

Single source of truth for the prompt, tool protocol, and response parser
shared by the local agent loops (`agents/agents/qwen3vl.py`,
`agents/agents/kimi.py`) and the prime-rl/verifiers adapter. The protocol is
text-embedded tool calling: the tool schema rides in the system prompt inside
<tools></tools> tags and the model answers with one
<tool_call>{"name": ..., "arguments": ...}</tool_call> block per turn, with
coordinates on a 0-1000 grid scaled to the real resolution at parse time.

Rollout note: the local agent loops maintain a sliding window (last
`history_n` turns as messages, older turns as a one-line text summary). The
adapter keeps the full message history instead — assistant text turns are the
action record — and bounds context via screenshot pruning
(`keep_recent_screenshots`, default 2 ≈ the agents' history_n=1 window).
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


def _tool_call_payloads(response):
    payloads = []
    tag_problem = None
    cursor = 0
    decoder = json.JSONDecoder()
    opening = re.compile(r"<tool_call\b[^<>]*(?:>|(?=<)|$)")
    while match := opening.search(response, cursor):
        prefix = response[cursor:match.start()]
        if "</tool_call" in prefix or "<tool_call" in prefix:
            tag_problem = "Unmatched tool_call tags"
        start = match.end()
        while start < len(response) and response[start].isspace():
            start += 1
        try:
            _, length = decoder.raw_decode(response[start:])
        except ValueError:
            payloads.append(None)
            boundary = re.search(r"</tool_call>|(?=<tool_call\b)", response[match.end():])
            cursor = match.end() + boundary.end() if boundary else len(response)
            continue
        end = start + length
        closing = re.match(r"\s*</tool_call>", response[end:])
        payloads.append(response[start:end] if match.group() == "<tool_call>" and closing else None)
        # Decode the whole JSON value before looking for the next block, so
        # literal tool_call tags inside typed text are not treated as calls.
        cursor = end + closing.end() if closing else end
    tail = response[cursor:]
    if not payloads or "</tool_call" in tail or "<tool_call" in tail:
        tag_problem = "Unmatched tool_call tags"
    return payloads, tag_problem


def _parse_error(thought, problem):
    logger.warning("Computer-use parse error: %s", problem)
    return {
        "actions": [{"action": "screenshot"}],
        "metadata": {
            "thought": thought,
            "conclusion": f"Parse error: {problem}. Inspect a fresh screenshot and retry with one tool call.",
            "action_type": "screenshot",
            "is_terminal": False,
            "wait_time": None,
            "parse_error": True,
        },
    }


def _validate_action(action_json):
    if not isinstance(action_json, dict):
        raise ValueError("Tool arguments must be an object")
    kind = action_json.get("action")
    mouse_actions = {"mouse_move", "left_click", "click", "right_click", "double_click",
                     "triple_click", "left_click_drag", "drag"}
    if not isinstance(kind, str) or kind not in mouse_actions | {"key", "type", "scroll", "wait", "terminate"}:
        raise ValueError("Missing or unsupported computer-use action")

    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Action coordinates and durations must be finite numbers")

    for field in ("coordinate", "coordinate2"):
        required = (field == "coordinate" and kind in mouse_actions) or (
            field == "coordinate2" and kind in {"left_click_drag", "drag"}
        )
        if required or (kind == "scroll" and field == "coordinate" and field in action_json):
            point = action_json.get(field)
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"{field} must contain two numbers")
            for value in point:
                number(value)
    if kind == "key":
        keys = action_json.get("keys")
        if not ((isinstance(keys, str) and keys) or (
            isinstance(keys, list) and keys and all(isinstance(key, str) and key for key in keys)
        )):
            raise ValueError("key requires a nonempty keys string or list of strings")
    elif kind == "type" and not isinstance(action_json.get("text"), str):
        raise ValueError("type requires a text string")
    elif kind == "scroll":
        number(action_json.get("pixels", action_json.get("scroll", 0)))
    elif kind == "wait":
        duration = action_json.get("time", 1.0)
        number(duration)
        if duration < 0:
            raise ValueError("wait duration must be nonnegative")
    elif kind == "terminate" and action_json.get("status", "success") not in ("success", "failure"):
        raise ValueError("terminate status must be success or failure")


def convert_point_format_qwen3vl(x, y, scale_dims=True, scale_dims_ratio=(1920 / 1000, 1080 / 1000)):
    if scale_dims:
        x = x * scale_dims_ratio[0]
        y = y * scale_dims_ratio[1]
    return int(x), int(y)


def parse_qwen3vl_response(response, scale_dims=True, scale_dims_ratio=(1920 / 1000, 1080 / 1000)):
    if not response or not isinstance(response, str):
        return _parse_error("Empty or invalid response", "Empty or invalid response")

    thought = response.split("</think>")[0]
    conclusion = None
    if "</think>" in response:
        response = response.split("</think>")[1]

    printable_ratio = sum(1 for c in response if c.isprintable() or c.isspace()) / max(len(response), 1)
    if printable_ratio < 0.5:
        return _parse_error(thought, "Garbled response detected")

    if "<tool_call" in response or "</tool_call" in response:
        # Keep incomplete blocks in the count: a broken sibling must never
        # turn a trailing terminate into a standalone call.
        payloads, tag_problem = _tool_call_payloads(response)
        if len(payloads) <= 1 and tag_problem:
            return _parse_error(thought, tag_problem)
    else:
        tag_problem = None
        # Preserve the legacy untagged JSON protocol, without repairing
        # truncated JSON into an apparently valid termination command.
        starts = [match.start() for match in re.finditer(r'\{\s*"name"\s*:\s*"computer_use"', response)]
        payloads = []
        for start in starts:
            try:
                _, end = json.JSONDecoder().raw_decode(response[start:])
                payloads.append(response[start:start + end])
            except ValueError:
                payloads.append(None)
        if not payloads:
            return _parse_error(thought, "No computer-use tool call found")

    for line in response.split("\n"):
        if "Action:" in line:
            conclusion = line.split("Action:")[-1].strip()
    if conclusion is None:
        conclusion = response.split("<tool_call>")[0].strip()

    parsed_calls = []
    errors = [tag_problem] if tag_problem else []
    for index, payload in enumerate(payloads):
        try:
            if payload is None:
                raise ValueError("Incomplete tool_call block or invalid JSON")
            call = json.loads(payload)
            if not isinstance(call, dict) or call.get("name", "computer_use") != "computer_use":
                raise ValueError("Expected a computer_use tool object")
            action_json = call.get("arguments", call)
            _validate_action(action_json)
            parsed = _translate_action(action_json, thought, conclusion, scale_dims, scale_dims_ratio)
            parsed_calls.append((index, parsed))
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            errors.append(f"call {index + 1}: {exc}")

    if len(payloads) == 1:
        return parsed_calls[0][1] if parsed_calls else _parse_error(thought, "; ".join(errors))

    # One GUI tool call may translate to several wire actions (e.g. typing
    # with clear/enter). Execute that whole call, then let the normal env.step
    # observation capture provide a fresh screenshot; never queue siblings.
    selected = next(((index, parsed) for index, parsed in parsed_calls
                     if parsed["actions"] and parsed["actions"] != [{"keyboard": {"text": ""}}]), None)
    ignored = len(payloads) - (1 if selected else 0)
    warning = f"Detected {len(payloads)} tool calls; ignored {ignored}. "
    if selected:
        index, result = selected
        warning += f"Only GUI call {index + 1} will execute. Inspect the fresh screenshot before choosing one next action."
        logger.warning("%s", warning)
    else:
        warning += "No executable GUI action; termination requires a standalone valid tool call."
        result = _parse_error(thought, warning + (" " + "; ".join(errors) if errors else ""))
    result["metadata"].update({
        "tool_call_count": len(payloads),
        "ignored_tool_calls": ignored,
        "tool_call_warning": warning,
    })
    return result


def _translate_action(action_json, thought, conclusion, scale_dims, scale_dims_ratio):
    metadata = {
        "thought": thought,
        "conclusion": conclusion,
        "action_type": action_json["action"],
        "is_terminal": False,
        "wait_time": None,
    }

    if action_json["action"] == "key":
        actions = [{"keyboard": {"keys": action_json["keys"]}}]
    elif action_json["action"] == "type":
        actions = []
        if action_json.get("clear"):
            actions.append({"keyboard": {"keys": ["ctrl", "a"]}})
        actions.append({"keyboard": {"text": action_json["text"]}})
        if action_json.get("enter"):
            actions.append({"keyboard": {"keys": ["Return"]}})
    elif action_json["action"] == "mouse_move":
        x, y = convert_point_format_qwen3vl(
            action_json["coordinate"][0],
            action_json["coordinate"][1],
            scale_dims,
            scale_dims_ratio,
        )
        actions = [{"mouse": {"move": [x, y]}}]
    elif action_json["action"] in {"left_click", "click"}:
        x, y = convert_point_format_qwen3vl(
            action_json["coordinate"][0],
            action_json["coordinate"][1],
            scale_dims,
            scale_dims_ratio,
        )
        actions = [{"mouse": {"left_click": [x, y]}}]
    elif action_json["action"] == "right_click":
        x, y = convert_point_format_qwen3vl(
            action_json["coordinate"][0],
            action_json["coordinate"][1],
            scale_dims,
            scale_dims_ratio,
        )
        actions = [{"mouse": {"right_click": [x, y]}}]
    elif action_json["action"] == "double_click":
        x, y = convert_point_format_qwen3vl(
            action_json["coordinate"][0],
            action_json["coordinate"][1],
            scale_dims,
            scale_dims_ratio,
        )
        actions = [{"mouse": {"double_click": [x, y]}}]
    elif action_json["action"] == "triple_click":
        x, y = convert_point_format_qwen3vl(
            action_json["coordinate"][0],
            action_json["coordinate"][1],
            scale_dims,
            scale_dims_ratio,
        )
        actions = [{"mouse": {"triple_click": [x, y]}}]
    elif action_json["action"] in {"left_click_drag", "drag"}:
        x1, y1 = convert_point_format_qwen3vl(
            action_json["coordinate"][0],
            action_json["coordinate"][1],
            scale_dims,
            scale_dims_ratio,
        )
        x2, y2 = convert_point_format_qwen3vl(
            action_json["coordinate2"][0],
            action_json["coordinate2"][1],
            scale_dims,
            scale_dims_ratio,
        )
        actions = [{"mouse": {"left_click_drag": [[x1, y1], [x2, y2]]}}]
    elif action_json["action"] == "scroll":
        if "coordinate" in action_json:
            x, y = convert_point_format_qwen3vl(
                action_json["coordinate"][0],
                action_json["coordinate"][1],
                scale_dims,
                scale_dims_ratio,
            )
            actions = [
                {"mouse": {"move": [x, y]}},
                {"mouse": {"scroll": action_json["pixels"] if "pixels" in action_json else action_json.get("scroll", 0)}},
            ]
        else:
            actions = [{"mouse": {"scroll": action_json["pixels"] if "pixels" in action_json else action_json.get("scroll", 0)}}]
    elif action_json["action"] == "wait":
        actions = []
        metadata["wait_time"] = action_json.get("time", 1.0)
    elif action_json["action"] == "terminate":
        actions = []
        metadata["is_terminal"] = True
        metadata["status"] = action_json.get("status", "success")
    else:
        actions = []

    return {"actions": actions, "metadata": metadata}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_COMPUTER_USE_DESCRIPTION = """Use a mouse and keyboard to interact with a computer, and take screenshots.
* This is an interface to a desktop GUI. You do not have access to a terminal or applications menu. You must click on desktop icons to start applications.
* Some applications may take time to start or process actions, so you may need to wait and take successive screenshots to see the results of your actions. E.g. if you click on Firefox and a window doesn't open, try wait and taking another screenshot.
* The screen is {resolution}. IMPORTANT: give every (x, y) coordinate as integers on a 0-1000 normalized grid, where [0,0] is the top-left corner and [1000,1000] is the bottom-right corner. Do NOT use raw pixel values.
* Whenever you intend to move the cursor to click on an element like an icon, you should consult a screenshot to determine the coordinates of the element before moving the cursor.
* If you tried clicking on a program or link but it failed to load even after waiting, try adjusting your cursor position so that the tip of the cursor visually falls on the element that you want to click.
* Make sure to click any buttons, links, icons, etc with the cursor tip in the center of the element. Don't click boxes on their edges unless asked."""


def qwen_tools_def(resolution: Tuple[int, int]) -> Dict[str, Any]:
    width, height = resolution
    return {
        "type": "function",
        "function": {
            "name": "computer_use",
            "description": _COMPUTER_USE_DESCRIPTION.format(resolution=f"{width}x{height}"),
            "parameters": {
                "properties": {
                    "action": {
                        "description": """The action to perform. The available actions are:
* `key`: Performs key down presses on the arguments passed in order, then performs key releases in reverse order.
* `type`: Type a string of text on the keyboard.
* `mouse_move`: Move the cursor to a specified (x, y) pixel coordinate on the screen.
* `click`: Click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `left_click`: Click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `drag`: Click and drag the cursor to a specified (x, y) pixel coordinate on the screen.
* `right_click`: Click the right mouse button at a specified (x, y) pixel coordinate on the screen.
* `middle_click`: Click the middle mouse button at a specified (x, y) pixel coordinate on the screen.
* `double_click`: Double-click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `scroll`: Performs a scroll of the mouse scroll wheel.
* `wait`: Wait specified seconds for the change to happen.
* `terminate`: Terminate the current task and report its completion status.""",
                        "enum": ["key", "type", "mouse_move", "click", "left_click", "drag",
                                 "right_click", "middle_click", "double_click", "scroll", "wait", "terminate"],
                        "type": "string"
                    },
                    "keys": {"description": "Required only by `action=key`.", "type": "array"},
                    "text": {"description": "Required only by `action=type`.", "type": "string"},
                    "coordinate": {"description": "The x,y coordinates for mouse actions.", "type": "array"},
                    "coordinate2": {"description": "The x2,y2 coordinates for drag end position. Required only by `action=drag`.", "type": "array"},
                    "pixels": {"description": "The amount of scrolling.", "type": "number"},
                    "time": {"description": "The seconds to wait.", "type": "number"},
                    "status": {
                        "description": "The status of the task.",
                        "type": "string",
                        "enum": ["success", "failure"]
                    }
                },
                "required": ["action"],
                "type": "object"
            }
        }
    }


def kimi_tools_def() -> Dict[str, Any]:
    """osworld-aligned variant: relative 1000x1000 coordinates, no click/drag aliases."""
    return {
        "type": "function",
        "function": {
            "name_for_human": "computer_use",
            "name": "computer_use",
            "description": _COMPUTER_USE_DESCRIPTION.format(resolution="1000x1000"),
            "parameters": {
                "properties": {
                    "action": {
                        "description": """The action to perform. The available actions are:
* `key`: Performs key down presses on the arguments passed in order, then performs key releases in reverse order.
* `type`: Type a string of text on the keyboard.
* `mouse_move`: Move the cursor to a specified (x, y) pixel coordinate on the screen.
* `left_click`: Click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `left_click_drag`: Click and drag the cursor to a specified (x, y) pixel coordinate on the screen.
* `right_click`: Click the right mouse button at a specified (x, y) pixel coordinate on the screen.
* `middle_click`: Click the middle mouse button at a specified (x, y) pixel coordinate on the screen.
* `double_click`: Double-click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `scroll`: Performs a scroll of the mouse scroll wheel.
* `wait`: Wait specified seconds for the change to happen.
* `terminate`: Terminate the current task and report its completion status.""",
                        "enum": ["key", "type", "mouse_move", "left_click", "left_click_drag",
                                 "right_click", "middle_click", "double_click", "scroll", "wait", "terminate"],
                        "type": "string"
                    },
                    "keys": {"description": "Required only by `action=key`.", "type": "array"},
                    "text": {"description": "Required only by `action=type`.", "type": "string"},
                    "coordinate": {"description": "The x,y coordinates for mouse actions.", "type": "array"},
                    "coordinate2": {"description": "The x2,y2 coordinates for drag end position. Required only by `action=left_click_drag`.", "type": "array"},
                    "pixels": {"description": "The amount of scrolling.", "type": "number"},
                    "time": {"description": "The seconds to wait.", "type": "number"},
                    "status": {
                        "description": "The status of the task.",
                        "type": "string",
                        "enum": ["success", "failure"]
                    }
                },
                "required": ["action"],
                "type": "object"
            }
        }
    }


def wrap_system_prompt(tools_def: Dict[str, Any]) -> str:
    return """# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
""" + json.dumps(tools_def) + """
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>

# Response format

Response format for every step:
1) Action: a short imperative describing what to do in the UI.
2) A single <tool_call>...</tool_call> block containing only the JSON: {"name": <function-name>, "arguments": <args-json-object>}.

Rules:
- Output exactly in the order: Action, <tool_call>.
- Be brief: one sentence for Action.
- Do not output anything else outside those parts.
- If finishing, use action=terminate in the tool call."""


def qwen_system_prompt(resolution: Tuple[int, int]) -> str:
    return wrap_system_prompt(qwen_tools_def(resolution))


def kimi_system_prompt() -> str:
    return wrap_system_prompt(kimi_tools_def())


INSTRUCTION_TEMPLATE = """Please generate the next move according to the UI screenshot, instruction and previous actions.

Instruction: {instruction}

Previous actions:
None"""


__all__ = [
    "INSTRUCTION_TEMPLATE",
    "convert_point_format_qwen3vl",
    "kimi_system_prompt",
    "kimi_tools_def",
    "parse_qwen3vl_response",
    "qwen_system_prompt",
    "qwen_tools_def",
    "wrap_system_prompt",
]
