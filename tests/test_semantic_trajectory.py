from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agents.agents.claude_gemini import Gemini3Agent
from agents.evaluation.semantic_trajectory import (
    SemanticStep,
    SemanticStepCompletionChecker,
    SemanticTrajectoryController,
    SemanticTrajectoryError,
    _parse_completion_response,
)


class SemanticTrajectoryTests(unittest.TestCase):
    def test_valid_object_step_loads_at_first_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "semantic.json"
            path.write_text(
                json.dumps(
                    {
                        "steps": [
                            {
                                "step_id": 7,
                                "instruction": "Open the project.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            controller = SemanticTrajectoryController.load(path)

        self.assertEqual(controller.current_step.step_id, 7)
        self.assertEqual(controller.current_step.instruction, "Open the project.")
        self.assertFalse(controller.is_finished())

    def test_simple_string_steps_are_normalized_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "semantic.json"
            path.write_text(
                json.dumps({"steps": ["First.", "Second.", "Third."]}),
                encoding="utf-8",
            )

            controller = SemanticTrajectoryController.load(path)

        self.assertEqual(
            [(step.step_id, step.instruction) for step in controller.steps],
            [(1, "First."), (2, "Second."), (3, "Third.")],
        )
        self.assertEqual(controller.steps[0].completion_criterion, "First.")

    def test_invalid_path_has_clear_error(self) -> None:
        with self.assertRaisesRegex(SemanticTrajectoryError, "path does not exist"):
            SemanticTrajectoryController.load("/definitely/missing/semantic.json")

    def test_invalid_json_and_schema_have_clear_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            invalid_json = Path(tmp) / "invalid.json"
            invalid_json.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(SemanticTrajectoryError, "invalid JSON"):
                SemanticTrajectoryController.load(invalid_json)

            missing_steps = Path(tmp) / "missing.json"
            missing_steps.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(SemanticTrajectoryError, 'missing required field "steps"'):
                SemanticTrajectoryController.load(missing_steps)

            missing_instruction = Path(tmp) / "missing-instruction.json"
            missing_instruction.write_text(json.dumps({"steps": [{}]}), encoding="utf-8")
            with self.assertRaisesRegex(SemanticTrajectoryError, 'missing required field "instruction"'):
                SemanticTrajectoryController.load(missing_instruction)

    def test_controller_progression_stays_on_incomplete_step(self) -> None:
        controller = SemanticTrajectoryController(
            [
                SemanticStep(1, "First."),
                SemanticStep(2, "Second."),
                SemanticStep(3, "Third."),
            ]
        )
        observed = [controller.current_step.step_id]

        controller.advance()
        observed.append(controller.current_step.step_id)
        observed.append(controller.current_step.step_id)
        controller.advance()
        observed.append(controller.current_step.step_id)
        controller.advance()

        self.assertEqual(observed, [1, 2, 2, 3])
        self.assertTrue(controller.is_finished())
        self.assertEqual(controller.completed_count, 3)

    def test_current_context_does_not_expose_future_instructions(self) -> None:
        controller = SemanticTrajectoryController(
            [
                SemanticStep(1, "Open the project."),
                SemanticStep(2, "Open the editor.", "The editor is visible."),
                SemanticStep(3, "SECRET FUTURE: commit the file."),
            ]
        )
        controller.advance()

        context = controller.current_context("Add a license.")

        self.assertIn("Current semantic step: 2 / 3", context)
        self.assertIn("Open the editor.", context)
        self.assertIn("The editor is visible.", context)
        self.assertNotIn("Open the project.", context)
        self.assertNotIn("SECRET FUTURE", context)

        agent = Gemini3Agent.__new__(Gemini3Agent)
        agent.execution_context = None
        self.assertEqual(
            agent._observation_content("image-data"),
            [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,image-data"},
                }
            ],
        )
        agent.set_execution_context(context)
        observation_content = agent._observation_content("image-data")
        exposed_text = "\n".join(
            item["text"] for item in observation_content if item["type"] == "text"
        )
        self.assertIn("Open the editor.", exposed_text)
        self.assertNotIn("SECRET FUTURE", exposed_text)

    def test_completion_response_requires_structured_boolean_json(self) -> None:
        completion = _parse_completion_response(
            '<think>inspect</think>\n```json\n{"completed": true, "reason": "Visible."}\n```'
        )
        self.assertTrue(completion.completed)
        self.assertEqual(completion.reason, "Visible.")

        with self.assertRaisesRegex(ValueError, "boolean"):
            _parse_completion_response('{"completed": "yes", "reason": "Visible."}')

    def test_completion_checker_uses_current_step_and_deterministic_sampling(self) -> None:
        calls = []

        def fake_llm_call(*args, **kwargs):
            calls.append((args, kwargs))
            return '{"completed": false, "reason": "Not visible yet."}'

        with tempfile.TemporaryDirectory() as tmp:
            screenshot = Path(tmp) / "screen.png"
            screenshot.write_bytes(b"fake-png")
            checker = SemanticStepCompletionChecker("gemini-test", llm_call=fake_llm_call)
            result = checker.check(
                SemanticStep(2, "Open the editor."),
                {"screen": {"path": str(screenshot)}},
            )

        self.assertFalse(result.completed)
        args, kwargs = calls[0]
        self.assertEqual(args[1:5], ("gemini-test", 0.0, 1.0, -1))
        self.assertEqual(kwargs["reasoning_effort"], "low")
        prompt_text = args[0][1]["content"][0]["text"]
        self.assertIn("Open the editor.", prompt_text)
        self.assertNotIn("future", prompt_text.lower())


if __name__ == "__main__":
    unittest.main()
