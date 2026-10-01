from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from agents.shared.llm_clients import call_gemini_with_retry


class SemanticTrajectoryError(ValueError):
    """Raised when a semantic trajectory cannot be loaded or validated."""


@dataclass(frozen=True)
class SemanticStep:
    step_id: int | str
    instruction: str
    expected_state: str | None = None

    @property
    def completion_criterion(self) -> str:
        return self.expected_state or self.instruction


@dataclass(frozen=True)
class SemanticStepCompletion:
    completed: bool
    reason: str


class SemanticTrajectoryController:
    def __init__(self, steps: Sequence[SemanticStep], *, task: str | None = None) -> None:
        if not steps:
            raise SemanticTrajectoryError("Invalid semantic trajectory: steps must be non-empty")
        self.steps = tuple(steps)
        self.task = task
        self.current_step_index = 0
        self.completed_steps: list[SemanticStep] = []

    @classmethod
    def load(cls, path: str | Path) -> "SemanticTrajectoryController":
        trajectory_path = Path(path)
        if not trajectory_path.is_file():
            raise SemanticTrajectoryError(
                f"Invalid semantic trajectory: path does not exist or is not a file: {trajectory_path}"
            )

        try:
            payload = json.loads(trajectory_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SemanticTrajectoryError(
                "Invalid semantic trajectory: invalid JSON "
                f"at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        except OSError as exc:
            raise SemanticTrajectoryError(
                f"Invalid semantic trajectory: could not read {trajectory_path}: {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise SemanticTrajectoryError("Invalid semantic trajectory: root must be a JSON object")
        if "steps" not in payload:
            raise SemanticTrajectoryError('Invalid semantic trajectory: missing required field "steps"')
        raw_steps = payload["steps"]
        if not isinstance(raw_steps, list):
            raise SemanticTrajectoryError('Invalid semantic trajectory: field "steps" must be a list')
        if not raw_steps:
            raise SemanticTrajectoryError("Invalid semantic trajectory: steps must be non-empty")

        task = payload.get("task")
        if task is not None and (not isinstance(task, str) or not task.strip()):
            raise SemanticTrajectoryError('Invalid semantic trajectory: field "task" must be a non-empty string')

        steps: list[SemanticStep] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            if isinstance(raw_step, str):
                instruction = raw_step.strip()
                if not instruction:
                    raise SemanticTrajectoryError(
                        f"Invalid semantic trajectory: step {index} instruction must be non-empty"
                    )
                steps.append(SemanticStep(step_id=index, instruction=instruction))
                continue

            if not isinstance(raw_step, dict):
                raise SemanticTrajectoryError(
                    f"Invalid semantic trajectory: step {index} must be a string or object"
                )
            if "instruction" not in raw_step:
                raise SemanticTrajectoryError(
                    f'Invalid semantic trajectory: step {index} is missing required field "instruction"'
                )
            instruction = raw_step["instruction"]
            if not isinstance(instruction, str) or not instruction.strip():
                raise SemanticTrajectoryError(
                    f"Invalid semantic trajectory: step {index} instruction must be a non-empty string"
                )

            expected_state = raw_step.get("expected_state")
            if expected_state is not None:
                if not isinstance(expected_state, str) or not expected_state.strip():
                    raise SemanticTrajectoryError(
                        f"Invalid semantic trajectory: step {index} expected_state must be a non-empty string"
                    )
                expected_state = expected_state.strip()

            step_id = raw_step.get("step_id", index)
            if not isinstance(step_id, (int, str)) or isinstance(step_id, bool):
                raise SemanticTrajectoryError(
                    f"Invalid semantic trajectory: step {index} step_id must be a string or integer"
                )
            if isinstance(step_id, str) and not step_id.strip():
                raise SemanticTrajectoryError(
                    f"Invalid semantic trajectory: step {index} step_id must be non-empty"
                )

            steps.append(
                SemanticStep(
                    step_id=step_id,
                    instruction=instruction.strip(),
                    expected_state=expected_state,
                )
            )

        return cls(steps, task=task.strip() if task else None)

    @property
    def current_step(self) -> SemanticStep | None:
        if self.is_finished():
            return None
        return self.steps[self.current_step_index]

    @property
    def completed_count(self) -> int:
        return len(self.completed_steps)

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    def advance(self) -> SemanticStep:
        step = self.current_step
        if step is None:
            raise RuntimeError("Semantic trajectory is already finished")
        self.completed_steps.append(step)
        self.current_step_index += 1
        return step

    def is_finished(self) -> bool:
        return self.current_step_index >= len(self.steps)

    def current_context(self, overall_task: str) -> str:
        step = self.current_step
        if step is None:
            raise RuntimeError("Semantic trajectory is already finished")
        return f"""Overall task:
{overall_task}

You are executing a mandatory reference trajectory.

Current semantic step: {self.current_step_index + 1} / {self.total_steps}

Instruction:
{step.instruction}

Expected state:
{step.completion_criterion}

Progress:
All previous semantic steps are complete.

Rules:
- Work only on the current semantic step.
- Do not independently re-plan the overall task.
- Do not skip or reorder semantic steps.
- Do not perform actions that belong to future semantic steps.
- You may use any required mouse, keyboard, scrolling, or waiting actions.
- Continue working on this semantic step until its expected state is reached.
"""


class SemanticStepCompletionChecker:
    def __init__(
        self,
        model: str,
        *,
        llm_call: Callable[..., Any] = call_gemini_with_retry,
    ) -> None:
        self.model = model
        self.llm_call = llm_call

    def check(self, step: SemanticStep, observation: dict[str, Any]) -> SemanticStepCompletion:
        image_url = _observation_image_url(observation)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict visual completion checker for one semantic step in a "
                    "computer-use trajectory. Judge only whether the current step's completion "
                    "criterion is visibly satisfied in the screenshot. Do not judge the overall "
                    "task or any future step. Return exactly one JSON object with boolean field "
                    '"completed" and string field "reason". Do not use markdown.'
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Current semantic instruction:\n{step.instruction}\n\n"
                            f"Completion criterion:\n{step.completion_criterion}\n\n"
                            "Has this semantic step been completed in the screenshot?"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ]
        response = self.llm_call(
            messages,
            self.model,
            0.0,
            1.0,
            -1,
            max_tokens=512,
            reasoning_effort="low",
        )
        return _parse_completion_response(response)


def _observation_image_url(observation: dict[str, Any]) -> str:
    screen = observation.get("screen") if isinstance(observation, dict) else None
    if not isinstance(screen, dict):
        raise ValueError("Semantic completion checker requires an observation with a screen")

    image = screen.get("image")
    if isinstance(image, str) and image.startswith("data:image/"):
        return image
    if isinstance(image, (bytes, bytearray)):
        encoded = base64.b64encode(bytes(image)).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    image_path = screen.get("path")
    if not image_path:
        raise ValueError("Semantic completion checker requires screen.path or screen.image")
    path = Path(image_path)
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise ValueError(f"Could not read semantic completion screenshot {path}: {exc}") from exc
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{media_type};base64,{encoded}"


def _parse_completion_response(response: Any) -> SemanticStepCompletion:
    if not isinstance(response, str):
        raise ValueError("Semantic completion checker returned a non-text response")

    text = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL | re.IGNORECASE).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise ValueError("Semantic completion checker did not return a JSON object")
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise ValueError("Semantic completion checker returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise ValueError("Semantic completion checker response must be a JSON object")
    completed = payload.get("completed")
    reason = payload.get("reason")
    if not isinstance(completed, bool):
        raise ValueError('Semantic completion checker response requires boolean field "completed"')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Semantic completion checker response requires non-empty string field "reason"')
    return SemanticStepCompletion(completed=completed, reason=reason.strip())
