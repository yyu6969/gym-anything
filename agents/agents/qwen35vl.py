import base64
import json
import math
import re
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from agents.agents.qwen3vl import Qwen3VLAgent
from agents.shared.llm_clients import capture_provider_metadata

GRID_MAX = 999.0
SCROLL_STEP_LIMIT = 10
COLLAPSED_SCREENSHOT_TEXT = "This screenshot has been collapsed."

# Mouse-family actions where a `keys` parameter means "hold these modifiers
# while performing the action" rather than a chord of its own.
_MOUSE_ACTIONS = {
    "left_click", "click", "right_click", "middle_click", "double_click",
    "triple_click", "left_click_drag", "drag", "scroll", "mouse_move",
}


class Qwen35ParseError(ValueError):
    """A rejected model turn: diagnostics exist, but no actions may execute."""

    def __init__(self, response, parsed_response):
        self.response_text = response if isinstance(response, str) else ""
        self.parsed_response = parsed_response
        self.parsing_diagnostics = parsed_response["metadata"]["parsing"]
        super().__init__("Qwen3.5 computer-use parsing failed: " + "; ".join(
            error["message"] for error in self.parsing_diagnostics["errors"]
        ))


class Qwen35VLAgent(Qwen3VLAgent):
    """
    Qwen3.5-VL agent, kept functionally consistent with the reference
    implementation in cua-speed-run (templates/qwen35vl/agent.py), which is
    itself the OSWorld-V2 qwen35vl_agent. Consistency covers everything the
    model sees and how its output is executed:

      - 1000x1000 relative coordinate contract: the prompt declares the screen
        as 1000x1000 and every coordinate is scaled by the screenshot's true
        original size / 999 at parse time (not a hardcoded 1920x1080 ratio).
      - smart_resize with max_pixels=16*16*4*12800, so 1920x1080 screenshots
        reach the model at full resolution.
      - Long-history message building with screenshot folding: up to
        `history_n` full turns, at most `image_max` live screenshots, older
        ones collapsed in-place to text in chunks of `fold_size`; non-first
        screenshots are wrapped in <tool_response> markers.
      - Context-length backoff: on model-call failure the messages are rebuilt
        with progressively smaller (history_n, image_max, fold_size) variants.
      - XML <function=...><parameter=...> tool calls with a JSON fallback;
        all blocks must validate before any execute. A missing opening
        function declaration is recoverable only for the sole compatible tool.
      - `keys` on mouse actions become a real hold (keys_down / action /
        keys_up); scroll steps are clamped to +/-10 and sign-inverted
        (Qwen negative=down -> runner positive=down).

    Deliberate deviations, forced by the gym-anything runner action API:
      - Single-coordinate drags ("drag from current cursor") are executed as a
        degenerate two-point drag at that coordinate; the runner has no
        "current cursor" drag form.
      - terminate with status=failure is reported via metadata["status"]
        (the harness convention) instead of a FAIL marker action.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Reference-agent context defaults (agent_args still override).
        self.history_n = self.agent_args.get("history_n", 100)
        self.image_max = self.agent_args.get("image_max", 20)
        self.fold_size = self.agent_args.get("fold_size", 10)
        self.max_tokens = self.agent_args.get("max_tokens", 2048)
        # Original (pre-resize) screenshot sizes, index-aligned with
        # self.screenshots; coordinates scale by the current one.
        self.original_sizes: List[Tuple[int, int]] = []
        self.processed_size: Tuple[int, int] = (0, 0)

    # ---- Image processing ------------------------------------------------------

    @staticmethod
    def _round_by_factor(number: float, factor: int) -> int:
        return round(number / factor) * factor

    @staticmethod
    def _floor_by_factor(number: float, factor: int) -> int:
        return math.floor(number / factor) * factor

    @staticmethod
    def _ceil_by_factor(number: float, factor: int) -> int:
        return math.ceil(number / factor) * factor

    @classmethod
    def _smart_resize(
        cls,
        *,
        height: int,
        width: int,
        factor: int = 32,
        min_pixels: int = 4 * 32 * 32,
        max_pixels: int = 16 * 16 * 4 * 12800,
    ) -> Tuple[int, int]:
        if height < factor or width < factor:
            raise ValueError(f"height:{height} or width:{width} must be >= factor:{factor}")
        if max(height, width) / min(height, width) > 200:
            raise ValueError(f"absolute aspect ratio must be smaller than 200, got {height}/{width}")
        h_bar = max(factor, cls._round_by_factor(height, factor))
        w_bar = max(factor, cls._round_by_factor(width, factor))
        if h_bar * w_bar > max_pixels:
            beta = math.sqrt((height * width) / max_pixels)
            h_bar = max(factor, cls._floor_by_factor(height / beta, factor))
            w_bar = max(factor, cls._floor_by_factor(width / beta, factor))
        elif h_bar * w_bar < min_pixels:
            beta = math.sqrt(min_pixels / (height * width))
            h_bar = cls._ceil_by_factor(height * beta, factor)
            w_bar = cls._ceil_by_factor(width * beta, factor)
        return int(h_bar), int(w_bar)

    def process_image(self, image_path):
        image = Image.open(image_path)
        original_width, original_height = image.size
        resized_height, resized_width = self._smart_resize(
            height=original_height,
            width=original_width,
            factor=32,
            max_pixels=16 * 16 * 4 * 12800,
        )
        image = image.resize((resized_width, resized_height))

        processed_path = f"{self.save_folder_custom}/observation_{self.step_idx}.png"
        image.save(processed_path, format="PNG")
        with open(processed_path, "rb") as f:
            processed_bytes = f.read()

        self.original_sizes.append((original_width, original_height))
        self.processed_size = (resized_width, resized_height)
        return base64.b64encode(processed_bytes).decode("utf-8"), processed_path

    # ---- Prompt ----------------------------------------------------------------

    @staticmethod
    def _tools_def() -> Dict[str, Any]:
        description_prompt = "\n".join(
            [
                "Use a mouse and keyboard to interact with a computer, and take screenshots.",
                "* This is an interface to a desktop GUI. You do not have access to a terminal or applications menu. You must click on desktop icons to start applications.",
                "* Some applications may take time to start or process actions, so you may need to wait and take successive screenshots to see the results of your actions.",
                "* The screen's resolution is 1000x1000.",
                "* Whenever you intend to move the cursor to click on an element like an icon, you should consult a screenshot to determine the coordinates of the element before moving the cursor.",
                "* If you tried clicking on a program or link but it failed to load, even after waiting, try adjusting your cursor position so that the tip of the cursor visually falls on the element that you want to click.",
                "* Make sure to click any buttons, links, icons, etc with the cursor tip in the center of the element. Don't click boxes on their edges unless asked.",
            ]
        )
        action_description_prompt = """
* `key`: Performs key down presses on the arguments passed in order, then performs key releases in reverse order.
* `type`: Type a string of text on the keyboard.
* `mouse_move`: Move the cursor to a specified (x, y) pixel coordinate on the screen.
* `left_click`: Click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `left_click_drag`: Drag from `coordinate` to `coordinate2`.
* `right_click`: Click the right mouse button at a specified (x, y) pixel coordinate on the screen.
* `middle_click`: Click the middle mouse button at a specified (x, y) pixel coordinate on the screen.
* `double_click`: Double-click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `triple_click`: Triple-click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `scroll`: Performs a vertical mouse-wheel scroll. Pass `pixels` as signed wheel steps: negative scrolls down, positive scrolls up, and the magnitude must be between 1 and 10.
* `wait`: Wait specified seconds for the change to happen.
* `terminate`: Terminate the current task and report its completion status.
* `answer`: Answer a question."""

        return {
            "type": "function",
            "function": {
                "name": "computer_use",
                "description": description_prompt,
                "parameters": {
                    "type": "object",
                    "required": ["action"],
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": action_description_prompt,
                            "enum": [
                                "key",
                                "type",
                                "mouse_move",
                                "left_click",
                                "left_click_drag",
                                "right_click",
                                "middle_click",
                                "double_click",
                                "triple_click",
                                "scroll",
                                "wait",
                                "terminate",
                                "answer",
                            ],
                        },
                        "keys": {"type": "array", "description": "Required only by `action=key`."},
                        "text": {
                            "type": "string",
                            "description": "Required by `action=type` and `action=answer`.",
                        },
                        "coordinate": {"type": "array", "description": "(x, y) coordinates."},
                        "coordinate2": {
                            "type": "array",
                            "description": "Drag-end (x, y) coordinates; required by `action=left_click_drag`.",
                        },
                        "pixels": {
                            "type": "number",
                            "description": "Signed wheel steps: negative scrolls down, positive scrolls up; use a magnitude from 1 to 10.",
                        },
                        "time": {"type": "number", "description": "Seconds to wait."},
                        "status": {
                            "type": "string",
                            "description": "Task status for terminate.",
                            "enum": ["success", "failure"],
                        },
                    },
                },
            },
        }

    def get_system_prompt(self) -> str:
        return (
            "You are a multi-purpose intelligent assistant. Based on my requests, you can use tools to help me complete various tasks.\n\n"
            "# Tools\n\n"
            "You have access to the following functions:\n\n"
            "<tools>\n"
            + json.dumps(self._tools_def(), ensure_ascii=False)
            + "\n</tools>\n\n"
            "If you choose to call a function ONLY reply in the following format with NO suffix:\n\n"
            "<tool_call>\n"
            "<function=example_function_name>\n"
            "<parameter=example_parameter_1>\n"
            "value_1\n"
            "</parameter>\n"
            "<parameter=example_parameter_2>\n"
            "This is the value for the second parameter\n"
            "that can span\n"
            "multiple lines\n"
            "</parameter>\n"
            "</function>\n"
            "</tool_call>\n\n"
            "<IMPORTANT>\n"
            "Reminder:\n"
            "- Function calls MUST follow the specified format: an inner <function=...></function> block must be nested within <tool_call></tool_call> XML tags\n"
            "- Required parameters MUST be specified\n"
            "- You may provide optional reasoning for your function call in natural language BEFORE the function call, but NOT after\n"
            "- If there is no function call available, answer the question like normal with your current knowledge and do not tell the user about function calls\n"
            f"- The current date is {datetime.today().strftime('%A, %B %d, %Y')}.\n"
            f"- Collapsed screenshots appear as text: {COLLAPSED_SCREENSHOT_TEXT}\n"
            "</IMPORTANT>\n\n"
            "# Response format\n\n"
            "Response format for every step:\n"
            "1) Action: a short imperative describing what to do in the UI.\n"
            "2) A single <tool_call>...</tool_call> block.\n\n"
            "Rules:\n"
            "- Output exactly in the order: Action, <tool_call>.\n"
            "- Be brief: one sentence for Action.\n"
            "- Do not output anything else outside those parts.\n"
            "- If finishing, use action=terminate in the tool call."
        )

    # ---- Message building with screenshot folding ------------------------------

    @staticmethod
    def _folded_prefix_for(total_screenshots: int, image_max: int, fold_size: int) -> int:
        folded_prefix_k = 0
        while (total_screenshots - folded_prefix_k) > image_max:
            folded_prefix_k += fold_size
        if folded_prefix_k > total_screenshots:
            folded_prefix_k = total_screenshots
        return folded_prefix_k

    @staticmethod
    def _wrap_tool_response(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return (
            [{"type": "text", "text": "<tool_response>\n"}]
            + parts
            + [{"type": "text", "text": "\n</tool_response>"}]
        )

    def build_messages(
        self,
        current_screenshot_b64,
        history_n: Optional[int] = None,
        image_max: Optional[int] = None,
        fold_size: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        # self.screenshots already includes the current screenshot (appended in
        # step() before this is called), matching the reference agent's layout.
        history_n = max(1, int(self.history_n if history_n is None else history_n))
        image_max = max(1, int(self.image_max if image_max is None else image_max))
        fold_size = max(1, int(self.fold_size if fold_size is None else fold_size))

        total_steps = len(self.screenshots)
        folded_prefix_k = self._folded_prefix_for(total_steps, image_max, fold_size)
        start_step = max(1, total_steps - history_n)
        previous_actions = [
            f"Step {i + 1}: {self.history[i]}"
            for i in range(0, min(start_step - 1, len(self.history)))
        ]
        previous_actions_str = "\n".join(previous_actions) if previous_actions else "None"
        instruction_prompt = (
            "\nPlease generate the next move according to the UI screenshot, instruction and previous actions.\n\n"
            f"Instruction: {self.task_description}\n\n"
            "Previous actions:\n"
            f"{previous_actions_str}"
        )

        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": [{"type": "text", "text": self.get_system_prompt()}],
            }
        ]

        for step_num in range(start_step, total_steps + 1):
            is_first_turn = step_num == start_step
            is_collapsed = step_num <= folded_prefix_k

            if is_collapsed:
                parts = [{"type": "text", "text": COLLAPSED_SCREENSHOT_TEXT}]
                if is_first_turn:
                    user_content = [{"type": "text", "text": instruction_prompt}]
                else:
                    user_content = self._wrap_tool_response(parts)
            else:
                img_url = f"data:image/png;base64,{self.screenshots[step_num - 1]}"
                if is_first_turn:
                    user_content = [
                        {"type": "image_url", "image_url": {"url": img_url}},
                        {"type": "text", "text": instruction_prompt},
                    ]
                else:
                    user_content = self._wrap_tool_response(
                        [{"type": "image_url", "image_url": {"url": img_url}}]
                    )
            messages.append({"role": "user", "content": user_content})

            if step_num <= total_steps - 1 and (step_num - 1) < len(self.responses):
                messages.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": self.responses[step_num - 1]}],
                    }
                )

        return messages

    def _context_variants(self) -> List[Tuple[int, int, int]]:
        candidates = [
            (self.history_n, self.image_max, self.fold_size),
            (min(self.history_n, 60), min(self.image_max, 12), min(self.fold_size, 6)),
            (min(self.history_n, 24), min(self.image_max, 8), min(self.fold_size, 4)),
            (min(self.history_n, 8), min(self.image_max, 4), min(self.fold_size, 2)),
        ]
        variants: List[Tuple[int, int, int]] = []
        seen = set()
        for history_n, image_max, fold_size in candidates:
            variant = (max(1, history_n), max(1, image_max), max(1, fold_size))
            if variant not in seen:
                variants.append(variant)
                seen.add(variant)
        return variants

    # ---- Step ------------------------------------------------------------------

    def step(self, obs, action_outputs):
        self.step_idx += 1

        processed_image_b64, processed_path = self.process_image(obs["screen"]["path"])
        self.screenshots.append(processed_image_b64)
        self.b64_to_path[processed_image_b64] = processed_path

        response = None
        received_response = False
        last_exc: Optional[Exception] = None
        for history_n, image_max, fold_size in self._context_variants():
            messages = self.build_messages(
                processed_image_b64,
                history_n=history_n,
                image_max=image_max,
                fold_size=fold_size,
            )
            if (history_n, image_max, fold_size) == self._context_variants()[0]:
                self.save_messages(messages)
            try:
                print(f"Calling LLM with temperature: {self.temperature}")
                with capture_provider_metadata() as provider_metadata:
                    response = self.llm_call(
                        messages,
                        self.model,
                        self.temperature,
                        self.top_p,
                        self.top_k,
                        self.max_tokens,
                    )
                received_response = True
                if (history_n, image_max, fold_size) != self._context_variants()[0]:
                    print(
                        f"context retry succeeded with history_n={history_n}, "
                        f"image_max={image_max}, fold_size={fold_size}"
                    )
                break
            except Exception as exc:
                # The client retries transient errors internally; what reaches
                # here is persistent — most importantly context-length
                # rejections, which a smaller message build can fix.
                last_exc = exc
                print(
                    f"model call failed with history_n={history_n}, "
                    f"image_max={image_max}, fold_size={fold_size}: {exc!r}"
                )
        if not received_response:
            raise RuntimeError("Qwen3.5 model call failed for all context variants") from last_exc

        self.responses.append(response)
        self.all_model_responses.append(response)

        original_width, original_height = self.original_sizes[-1]
        try:
            parsed_response = self._parse_response(response, original_width, original_height)
        except Qwen35ParseError as exc:
            if provider_metadata:
                exc.parsing_diagnostics["provider_metadata"] = provider_metadata
            self.all_parsed_responses.append(exc.parsed_response)
            raise

        if provider_metadata:
            parsed_response["metadata"]["parsing"]["provider_metadata"] = provider_metadata
        self.all_parsed_responses.append(parsed_response)

        actions = parsed_response["actions"]
        metadata = parsed_response["metadata"]
        self.history.append(metadata["conclusion"])

        if self.verbose:
            print(f"Step {self.step_idx + 1}:")
            print(f"  Conclusion: {metadata['conclusion']}")
            print(f"  Actions: {actions}")

        tool_id = f"qwen35vl_step_{self.step_idx}"

        if metadata["is_terminal"]:
            self.done = True
            return [{"tool_id": tool_id, "actions": actions, "metadata": metadata}]

        return [{"tool_id": tool_id, "actions": actions, "metadata": metadata}]

    # ---- Response parsing ------------------------------------------------------

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate tool argument or field")
            result[key] = value
        return result

    def _sole_computer_use_tool(self):
        tools = self._tools_def()
        tools = [tools] if isinstance(tools, dict) else tools
        if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict):
            return None
        function = tools[0].get("function")
        if tools[0].get("type") != "function" or not isinstance(function, dict) or function.get("name") != "computer_use":
            return None
        schema = function.get("parameters", {})
        if not isinstance(schema, dict) or schema.get("type") != "object" or "action" not in schema.get("required", []):
            return None
        action = schema.get("properties", {}).get("action", {})
        return function if action.get("type") == "string" and isinstance(action.get("enum"), list) else None

    def _tool_payloads(self, response):
        """Read complete blocks; literal tags inside JSON or XML text are data."""
        decoder = json.JSONDecoder(object_pairs_hook=self._unique_object)
        starts = list(re.finditer(r'\{\s*"name"\s*:', response))
        first_tag = response.find("<tool_call")
        if (first_tag < 0 and "</tool_call" not in response) or (starts and (first_tag < 0 or starts[0].start() < first_tag)):
            if not starts:
                raise ValueError("No computer-use tool call found")
            payloads, end = [], 0
            for start in starts:
                if start.start() < end:
                    continue
                if "<tool_call" in response[end:start.start()] or "</tool_call" in response[end:start.start()]:
                    raise ValueError("Mixed or unmatched tool_call tags")
                _, length = decoder.raw_decode(response[start.start():])
                end = start.start() + length
                payloads.append(response[start.start():end])
            if "<tool_call" in response[end:] or "</tool_call" in response[end:]:
                raise ValueError("Mixed or unmatched tool_call tags")
            return payloads

        payloads, cursor = [], 0
        boundary = re.compile(r"<parameter=[^>]+>.*?</parameter>|</?tool_call[^<>]*>", re.DOTALL)
        while cursor < len(response):
            opening = re.search(r"<tool_call[^<>]*>", response[cursor:])
            if opening is None:
                if "<tool_call" in response[cursor:] or "</tool_call" in response[cursor:]:
                    raise ValueError("Unmatched tool_call tags")
                if re.search(r'\{\s*"name"\s*:', response[cursor:]):
                    raise ValueError("Mixed tagged and untagged tool calls")
                break
            start = cursor + opening.start()
            if opening.group() != "<tool_call>" or any(tag in response[cursor:start] for tag in ("<tool_call", "</tool_call")):
                raise ValueError("Malformed tool_call tags")
            if re.search(r'\{\s*"name"\s*:', response[cursor:start]):
                raise ValueError("Mixed tagged and untagged tool calls")
            body = cursor + opening.end()
            first = body + len(response[body:]) - len(response[body:].lstrip())
            if response[first:first + 1] == "{":
                _, length = decoder.raw_decode(response[first:])
                end = first + length
                closing = re.match(r"\s*</tool_call>", response[end:])
                if closing is None:
                    raise ValueError("Incomplete JSON tool_call block")
                payloads.append(response[first:end])
                cursor = end + closing.end()
                continue
            for token in boundary.finditer(response, body):
                if token.group().startswith("<parameter="):
                    continue
                if token.group() != "</tool_call>":
                    raise ValueError("Nested or malformed tool_call block")
                payloads.append(response[body:token.start()])
                cursor = token.end()
                break
            else:
                raise ValueError("Incomplete XML tool_call block")
        if not payloads:
            raise ValueError("No complete tool_call block found")
        return payloads

    def _parse_tool_call(self, payload, record):
        content = payload.strip()
        if content.startswith("{"):
            call = json.loads(content, object_pairs_hook=self._unique_object)
            if not isinstance(call, dict):
                raise ValueError("Tool call must be an object")
            if "name" in call:
                record["function_name"] = call["name"]
                if call["name"] != "computer_use" or set(call) - {"name", "arguments"}:
                    raise ValueError("Expected an unambiguous computer_use call")
                params = call.get("arguments")
            else:
                if self._sole_computer_use_tool() is None:
                    raise ValueError("Unnamed JSON call with ambiguous configured tools")
                record["function_name"] = "computer_use"
                params = call
            record["format"] = "json"
            record["original_arguments"] = params
            return params

        record["format"] = "xml"
        opening = re.match(r"<function=([^>]+)>", content)
        if opening:
            record["function_name"] = opening.group(1)
            if opening.group(1) != "computer_use":
                raise ValueError("Explicit function name is not computer_use")
            content = content[opening.end():]
        elif "<function" in content:
            raise ValueError("Malformed function declaration")
        else:
            if self._sole_computer_use_tool() is None:
                raise ValueError("Missing function declaration with ambiguous configured tools")
            record["recovery_reason"] = "missing_opening_function_declaration"
        if not content.endswith("</function>"):
            raise ValueError("Missing closing function declaration")
        content = content[:-len("</function>")]
        params: Dict[str, Any] = {}
        cursor = 0
        for match in re.finditer(
            r"<parameter=([^>]+)>(.*?)</parameter>", content, re.DOTALL
        ):
            if content[cursor:match.start()].strip():
                raise ValueError("Unexpected content between tool parameters")
            cursor = match.end()
            name = match.group(1)
            if name in params:
                raise ValueError("Duplicate tool parameter")
            value = match.group(2)
            if name == "text":
                # Trim exactly one wrapping newline; inner whitespace is content.
                if value.startswith("\r\n"):
                    value = value[2:]
                elif value.startswith("\n"):
                    value = value[1:]
                if value.endswith("\r\n"):
                    value = value[:-2]
                elif value.endswith("\n"):
                    value = value[:-1]
            else:
                value = value.strip()
            if name in {"coordinate", "coordinate2", "keys"} and value.startswith(("[", "{")):
                try:
                    params[name] = json.loads(value, object_pairs_hook=self._unique_object)
                    continue
                except json.JSONDecodeError:
                    pass
            params[name] = value
        if content[cursor:].strip() or not params:
            raise ValueError("Malformed or empty tool parameters")
        record["original_arguments"] = params
        return params

    def _validate_params(self, params, *, recovering=False):
        if not isinstance(params, dict):
            raise ValueError("Tool arguments must be an object")
        params = dict(params)
        known = {"action", "coordinate", "coordinate2", "keys", "text", "pixels", "time", "status"}
        if set(params) - known:
            raise ValueError("Unsupported computer-use parameter")
        action = params.get("action")
        supported = _MOUSE_ACTIONS | {"key", "type", "wait", "terminate", "answer", "screenshot", "noop"}
        if not isinstance(action, str) or action.strip() not in supported:
            raise ValueError("Missing or unsupported computer-use action")
        params["action"] = action = action.strip()

        def number(value):
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError("Action values must be finite numbers")
            try:
                result = float(value)
            except (ValueError, OverflowError):
                raise ValueError("Action values must be finite numbers") from None
            if not math.isfinite(result):
                raise ValueError("Action values must be finite numbers")
            return result

        for key in ("coordinate", "coordinate2"):
            if key not in params:
                continue
            point = params[key]
            if isinstance(point, str):
                try:
                    point = json.loads(point)
                except ValueError:
                    raise ValueError("Coordinates must contain exactly two finite grid numbers") from None
            if (not isinstance(point, list) or len(point) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in point)
                    or any(not math.isfinite(v) or not 0 <= v <= GRID_MAX for v in point)):
                raise ValueError("Coordinates must contain exactly two finite grid numbers in [0,999]")
            params[key] = point
        if action in {"mouse_move", "middle_click", "double_click", "triple_click", "left_click_drag", "drag"} and "coordinate" not in params:
            raise ValueError("Action requires coordinate")
        if "keys" in params:
            keys = params["keys"]
            if isinstance(keys, str):
                if len(keys.strip()) > 1 and keys.lstrip().startswith(("[", "{", '"')):
                    try:
                        keys = json.loads(keys)
                    except ValueError:
                        raise ValueError("Malformed structured keys argument") from None
                else:
                    keys = [keys]
                if isinstance(keys, str):
                    keys = [keys]
            if not isinstance(keys, list) or any(not isinstance(k, str) or not k.strip() for k in keys):
                raise ValueError("keys must be a list of nonempty strings or a key string")
            params["keys"] = [key.strip() for key in keys]
        if action == "key" and not params.get("keys"):
            raise ValueError("key requires nonempty keys")
        if "text" in params and not isinstance(params["text"], str):
            raise ValueError("text must be a string")
        if action in {"type", "answer"} and "text" not in params:
            raise ValueError("Action requires text")
        if "pixels" in params:
            pixels = number(params["pixels"])
            if not pixels.is_integer():
                raise ValueError("scroll pixels must be integer wheel steps")
            params["pixels"] = int(pixels)
        if action == "scroll" and "pixels" not in params:
            raise ValueError("scroll requires pixels")
        if "time" in params or action == "wait":
            duration = number(params.get("time", 1.0))
            if duration < 0:
                raise ValueError("wait duration must be nonnegative")
            params["time"] = duration
        if "status" in params:
            if not isinstance(params["status"], str) or params["status"].strip().lower() not in {"success", "failure"}:
                raise ValueError("terminate status must be success or failure")
            params["status"] = params["status"].strip().lower()
        if recovering:
            schema = self._sole_computer_use_tool()["parameters"]
            properties = schema["properties"]
            if any(key not in params for key in schema.get("required", [])) or set(params) - properties.keys():
                raise ValueError("Recovered parameters do not match the configured tool schema")
            for key, value in params.items():
                spec = properties[key]
                valid_type = {"string": isinstance(value, str), "array": isinstance(value, list),
                              "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
                if not valid_type.get(spec.get("type"), False) or ("enum" in spec and value not in spec["enum"]):
                    raise ValueError("Recovered parameters do not match the configured tool schema")
        return params

    @staticmethod
    def _parse_keys(raw_keys: Any) -> List[str]:
        if isinstance(raw_keys, str):
            try:
                raw_keys = json.loads(raw_keys)
            except Exception:
                raw_keys = [raw_keys]
        if isinstance(raw_keys, list):
            return [str(key).strip() for key in raw_keys if str(key).strip()]
        if raw_keys is None:
            return []
        return [str(raw_keys).strip()]

    @staticmethod
    def _parse_coordinate(raw_coord: Any) -> Optional[Tuple[float, float]]:
        if isinstance(raw_coord, str):
            try:
                raw_coord = json.loads(raw_coord)
            except Exception:
                return None
        if isinstance(raw_coord, list) and len(raw_coord) >= 2:
            try:
                return float(raw_coord[0]), float(raw_coord[1])
            except Exception:
                return None
        return None

    @staticmethod
    def _scale_coordinate(
        coord: Tuple[float, float], original_width: int, original_height: int
    ) -> List[int]:
        x, y = coord
        return [int(x * original_width / GRID_MAX), int(y * original_height / GRID_MAX)]

    def _parse_response(
        self, response: str, original_width: int, original_height: int
    ) -> Dict[str, Any]:
        original_response = response
        thought = ""
        diagnostics = {"policy": "validate_all_calls_before_execution", "tool_calls": [],
                       "recovered": False, "errors": [], "execution_status": "planned", "planned_actions": []}

        def reject(message, call_index=None):
            diagnostics["errors"].append({"call_index": call_index, "message": message})

        def raise_rejected():
            diagnostics["execution_status"] = "rejected"
            raise Qwen35ParseError(original_response, {"actions": [], "metadata": {
                "thought": thought, "conclusion": "Computer-use response rejected",
                "action_type": "parse_error", "is_terminal": False, "wait_time": None,
                "parse_error": True, "parsing": diagnostics,
            }})

        if not isinstance(response, str) or not response.strip():
            reject("Empty or invalid model response")
            raise_rejected()
        if "</think>" in response:
            thought = response.split("</think>", 1)[0]
            response = response.split("</think>", 1)[1]

        low_level_instruction = ""
        for line in response.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("action:"):
                low_level_instruction = stripped.split(":", 1)[-1].strip()
                break

        validated_calls = []
        try:
            payloads = self._tool_payloads(response)
        except ValueError as exc:
            reject(str(exc))
            raise_rejected()
        for index, payload in enumerate(payloads):
            record = {"call_index": index, "raw": payload, "function_name": None,
                      "original_arguments": None, "recovered_call": None, "error": None}
            diagnostics["tool_calls"].append(record)
            try:
                params = self._parse_tool_call(payload, record)
                params = self._validate_params(params, recovering="recovery_reason" in record)
                call = {"function_name": "computer_use", "arguments": params}
                record["validated_call"] = call
                if "recovery_reason" in record:
                    record["recovered_call"] = call
                    diagnostics["recovered"] = True
                validated_calls.append(params)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                record["error"] = str(exc)
                reject(str(exc), index)
        if (any(call["action"] in {"terminate", "answer"} for call in validated_calls[:-1])):
            reject("Termination or answer must be the final tool call")
        if (isinstance(original_width, bool) or isinstance(original_height, bool)
                or not isinstance(original_width, int) or not isinstance(original_height, int)
                or original_width <= 0 or original_height <= 0):
            reject("Screenshot dimensions must be positive integers")
        if diagnostics["errors"]:
            raise_rejected()

        actions: List[Dict[str, Any]] = []
        is_terminal = False
        wait_time: Optional[float] = None
        action_types: List[str] = []
        terminate_status: Optional[str] = None
        answer_text: Optional[str] = None

        def scaled(params: Dict[str, Any], key: str = "coordinate") -> Optional[List[int]]:
            coord = self._parse_coordinate(params.get(key))
            if coord is None:
                return None
            return self._scale_coordinate(coord, original_width, original_height)

        def process_params(params: Dict[str, Any]) -> None:
            nonlocal is_terminal, wait_time, terminate_status, answer_text
            action = str(params.get("action", "")).strip()
            if not action:
                return
            action_types.append(action)

            # Qwen emits `keys` on mouse actions to mean "hold these while
            # performing the action" (shift+click to extend a selection,
            # ctrl+scroll to zoom). Emit an explicit hold around the mouse
            # action; the plain `key` action keeps `keys` as the chord itself.
            mouse_hold = (
                self._parse_keys(params.get("keys", []))
                if action in _MOUSE_ACTIONS
                else []
            )
            if mouse_hold:
                actions.append({"keyboard": {"keys_down": mouse_hold}})
            hold_marker = len(actions)

            if action == "key":
                keys = self._parse_keys(params.get("keys", []))
                if keys:
                    actions.append({"keyboard": {"keys": keys}})
            elif action == "type":
                actions.append({"keyboard": {"text": str(params.get("text", ""))}})
            elif action == "mouse_move":
                point = scaled(params)
                if point:
                    actions.append({"mouse": {"move": point}})
            elif action in {"left_click", "click"}:
                point = scaled(params)
                if point:
                    actions.append({"mouse": {"left_click": point}})
                else:
                    actions.append({"mouse": {"buttons": {"left_down": True, "left_up": True}}})
            elif action == "right_click":
                point = scaled(params)
                if point:
                    actions.append({"mouse": {"right_click": point}})
                else:
                    actions.append({"mouse": {"buttons": {"right_down": True, "right_up": True}}})
            elif action == "middle_click":
                point = scaled(params)
                if point:
                    actions.append({"mouse": {"middle_click": point}})
            elif action == "double_click":
                point = scaled(params)
                if point:
                    actions.append({"mouse": {"double_click": point}})
            elif action == "triple_click":
                point = scaled(params)
                if point:
                    actions.append({"mouse": {"triple_click": point}})
            elif action in {"left_click_drag", "drag"}:
                start = scaled(params)
                end = scaled(params, "coordinate2")
                if start and end:
                    actions.append({"mouse": {"left_click_drag": [start, end]}})
                elif start:
                    # Reference behavior is "drag from the current cursor to
                    # this point"; the runner drag form needs two points, so
                    # degrade to a drag at the target rather than dropping it.
                    actions.append({"mouse": {"left_click_drag": [start, start]}})
            elif action == "scroll":
                requested_steps = params["pixels"]
                bounded_steps = max(
                    -SCROLL_STEP_LIMIT, min(SCROLL_STEP_LIMIT, requested_steps)
                )
                point = scaled(params)
                if point:
                    actions.append({"mouse": {"move": point}})
                if bounded_steps:
                    # Qwen uses negative=down; the runner scroll action uses
                    # positive=down.
                    actions.append({"mouse": {"scroll": -bounded_steps}})
                else:
                    # An explicit zero-wheel request is an intentional no-op
                    # with a fresh observation, never a parsing fallback.
                    actions.append({"action": "screenshot"})
            elif action == "wait":
                wait_time = params["time"]
                actions.append({"action": "wait", "time": wait_time})
            elif action in {"screenshot", "noop"}:
                actions.append({"action": "screenshot"})
            elif action in {"terminate", "answer"}:
                if action == "terminate":
                    terminate_status = str(params.get("status", "success")).strip().lower()
                else:
                    answer_text = str(params.get("text", ""))
                is_terminal = True

            if mouse_hold:
                if len(actions) == hold_marker:
                    # The action produced nothing to hold around; drop the
                    # press so a bare modifier tap never reaches the env.
                    actions.pop()
                else:
                    actions.append({"keyboard": {"keys_up": list(reversed(mouse_hold))}})

        for params in validated_calls:
            process_params(params)

        if not low_level_instruction:
            if is_terminal:
                low_level_instruction = "Task completed"
            elif wait_time is not None:
                low_level_instruction = "Waiting"
            elif actions:
                low_level_instruction = "Performing action"
        diagnostics["planned_actions"] = actions
        diagnostics["intent"] = ("terminal" if is_terminal else "wait" if all(p["action"] == "wait" for p in validated_calls)
                                  else "screenshot" if all(p["action"] == "screenshot" for p in validated_calls)
                                  else "noop" if all(p["action"] == "noop" or (p["action"] == "type" and p["text"] == "")
                                                     or (p["action"] == "scroll" and p["pixels"] == 0 and "coordinate" not in p)
                                                     for p in validated_calls) else "actions")

        metadata: Dict[str, Any] = {
            "thought": thought,
            "conclusion": low_level_instruction,
            "action_type": action_types[0] if action_types else "wait",
            "is_terminal": is_terminal,
            "wait_time": wait_time,
            "parsing": diagnostics,
        }
        if terminate_status is not None:
            metadata["status"] = terminate_status
        if answer_text is not None:
            metadata["answer"] = answer_text

        return {"actions": actions, "metadata": metadata}
