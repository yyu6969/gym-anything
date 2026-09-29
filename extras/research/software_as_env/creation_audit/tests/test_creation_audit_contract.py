"""Contract tests for the creation_audit method.

These tests verify the wiring (parser, path resolution, prompt assembly)
without invoking the actual agent CLI.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from extras.research.software_as_env.creation_audit import method as ca

TASK_INSTRUCTION = "Add an MIT license to the 'Byte Blaze / dotfiles' repository."

VALID_ENVIRONMENT_SPEC = {
    "software": {"name": "GitLab", "version": None},
    "entities": [
        {
            "id": "project_dotfiles",
            "type": "project",
            "attributes": {"path_with_namespace": "byteblaze/dotfiles"},
        }
    ],
    "properties": [
        {
            "entity_ref": "project_dotfiles",
            "property": "default_branch",
            "value": "main",
        }
    ],
    "relationships": [
        {
            "subject_ref": "user_byte_blaze",
            "relationship": "can_write",
            "object_ref": "project_dotfiles",
        }
    ],
    "capabilities": [
        {
            "name": "apply_license_template",
            "entity_refs": ["project_dotfiles"],
        }
    ],
    "environment_settings": [],
    "data_generation_constraints": {
        "preserve_entities": True,
        "allow_synthetic_background_data": True,
    },
}

VALID_ENVIRONMENT_INITIAL_STATE = {
    "episode_start": {
        "application": "GitLab",
        "page_type": "projects_dashboard",
        "authenticated": True,
        "authenticated_entity_ref": "user_byte_blaze",
    },
    "task_preconditions": [
        {
            "id": "has_license",
            "entity_ref": "project_dotfiles",
            "attribute": "has_license",
            "value": False,
        }
    ],
    "evidence": [],
}

VALID_TASK_INSTRUCTION_ARTIFACT = {
    "step_id": "step4_candidate_selection",
    "status": "success",
    "output": {
        "selected_candidate_id": "candidate_1",
        "selected_instruction": TASK_INSTRUCTION,
    },
}


def _write_json(directory: str, filename: str, document: object) -> Path:
    path = Path(directory) / filename
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _write_spec(directory: str, document=None) -> Path:
    payload = (
        {"environment_spec": VALID_ENVIRONMENT_SPEC} if document is None else document
    )
    return _write_json(directory, "environment_spec.json", payload)


def _write_initial_state(directory: str, document=None) -> Path:
    payload = (
        {"environment_initial_state": VALID_ENVIRONMENT_INITIAL_STATE}
        if document is None
        else document
    )
    return _write_json(directory, "environment_initial_state.json", payload)


def _write_task_instruction(directory: str, document=None) -> Path:
    payload = VALID_TASK_INSTRUCTION_ARTIFACT if document is None else document
    return _write_json(directory, "step4_candidate_selection.json", payload)


class EnvironmentSpecLoadingTests(unittest.TestCase):
    def test_valid_environment_spec_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = ca.load_environment_spec(_write_spec(tmp))

        self.assertEqual(loaded, VALID_ENVIRONMENT_SPEC)

    def test_missing_file_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ca.load_environment_spec(Path("/no/such/environment_spec.json"))

    def test_malformed_json_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "environment_spec.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "Malformed Environment Specification JSON"
            ):
                ca.load_environment_spec(path)

    def test_missing_top_level_environment_spec_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_spec(tmp, {"software": {"name": "GitLab"}})
            with self.assertRaisesRegex(ValueError, "top-level 'environment_spec'"):
                ca.load_environment_spec(path)

    def test_environment_spec_wrapper_must_be_an_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_spec(tmp, {"environment_spec": []})
            with self.assertRaisesRegex(
                ValueError, "'environment_spec' must be an object"
            ):
                ca.load_environment_spec(path)


class EnvironmentInitialStateLoadingTests(unittest.TestCase):
    def test_valid_environment_initial_state_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = ca.load_environment_initial_state(_write_initial_state(tmp))

        self.assertEqual(loaded, VALID_ENVIRONMENT_INITIAL_STATE)

    def test_missing_file_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ca.load_environment_initial_state(
                Path("/no/such/environment_initial_state.json")
            )

    def test_malformed_json_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "environment_initial_state.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "Malformed Environment Initial State JSON"
            ):
                ca.load_environment_initial_state(path)

    def test_missing_top_level_wrapper_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_initial_state(tmp, {"episode_start": {}})
            with self.assertRaisesRegex(
                ValueError, "top-level 'environment_initial_state'"
            ):
                ca.load_environment_initial_state(path)


class TaskInstructionArtifactLoadingTests(unittest.TestCase):
    def test_canonical_selected_instruction_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = ca.load_task_instruction_artifact(_write_task_instruction(tmp))

        self.assertEqual(loaded, TASK_INSTRUCTION)

    def test_missing_selected_instruction_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_task_instruction(tmp, {"output": {}})
            with self.assertRaisesRegex(ValueError, "output.selected_instruction"):
                ca.load_task_instruction_artifact(path)


class SoftwareResolutionTests(unittest.TestCase):
    def test_software_is_derived_from_environment_spec(self):
        self.assertEqual(ca._resolve_software(None, VALID_ENVIRONMENT_SPEC), "GitLab")

    def test_matching_software_is_accepted_case_insensitively(self):
        self.assertEqual(
            ca._resolve_software("gitlab", VALID_ENVIRONMENT_SPEC), "gitlab"
        )

    def test_software_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            ca._resolve_software("Moodle", VALID_ENVIRONMENT_SPEC)

    def test_software_remains_required_without_environment_spec(self):
        with self.assertRaisesRegex(ValueError, "--software is required"):
            ca._resolve_software(None, None)


class PathResolutionTests(unittest.TestCase):
    def test_packaged_memory_dir_exists_and_has_required_files(self):
        prompts = ca._packaged_memory_dir()
        self.assertTrue(prompts.is_dir(), prompts)
        self.assertTrue((prompts / "audit_prompt.md").is_file())
        self.assertTrue((prompts / "env_creation_notes" / "prompt.md").is_file())
        # The 800-line creation prompt should be substantial
        self.assertGreater(
            (prompts / "env_creation_notes" / "prompt.md").stat().st_size, 5000
        )

    def test_default_workspace_finds_repo_root(self):
        ws = ca._default_workspace()
        self.assertTrue((ws / "src" / "gym_anything").is_dir())


class ParserTests(unittest.TestCase):
    def test_required_args(self):
        parser = ca.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_defaults(self):
        args = ca.build_parser().parse_args(
            ["--software", "Demo", "--env-dir", "demo_env"]
        )
        self.assertEqual(args.software, "Demo")
        self.assertEqual(args.env_dir, "demo_env")
        self.assertEqual(args.backend, "cc")
        self.assertEqual(args.blind_nudges, ca.DEFAULT_BLIND_NUDGES)
        self.assertEqual(args.audit_rounds, ca.DEFAULT_AUDIT_ROUNDS)
        self.assertEqual(args.start_idx, 0)
        self.assertEqual(args.timeout_sec, ca.DEFAULT_TIMEOUT_SEC)
        self.assertIsNone(args.environment_spec)
        self.assertIsNone(args.task_instruction)
        self.assertIsNone(args.task_instruction_file)
        self.assertIsNone(args.environment_initial_state)

    def test_environment_spec_allows_software_to_be_omitted_at_parse_time(self):
        args = ca.build_parser().parse_args(
            [
                "--environment-spec",
                "/tmp/environment_spec.json",
                "--env-dir",
                "demo_env",
            ]
        )
        self.assertIsNone(args.software)
        self.assertEqual(args.environment_spec, Path("/tmp/environment_spec.json"))

    def test_aab_context_arguments_parse(self):
        args = ca.build_parser().parse_args(
            [
                "--software",
                "GitLab",
                "--env-dir",
                "gitlab_env",
                "--task-instruction",
                TASK_INSTRUCTION,
                "--environment-spec",
                "/tmp/environment_spec.json",
                "--environment-initial-state",
                "/tmp/environment_initial_state.json",
            ]
        )
        self.assertEqual(args.task_instruction, TASK_INSTRUCTION)
        self.assertEqual(
            args.environment_spec, Path("/tmp/environment_spec.json")
        )
        self.assertEqual(
            args.environment_initial_state,
            Path("/tmp/environment_initial_state.json"),
        )

    def test_task_instruction_file_parses(self):
        args = ca.build_parser().parse_args(
            [
                "--software",
                "GitLab",
                "--env-dir",
                "gitlab_env",
                "--task-instruction-file",
                "/tmp/step4_candidate_selection.json",
            ]
        )
        self.assertEqual(
            args.task_instruction_file,
            Path("/tmp/step4_candidate_selection.json"),
        )

    def test_task_instruction_forms_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            ca.build_parser().parse_args(
                [
                    "--software",
                    "GitLab",
                    "--env-dir",
                    "gitlab_env",
                    "--task-instruction",
                    TASK_INSTRUCTION,
                    "--task-instruction-file",
                    "/tmp/step4_candidate_selection.json",
                ]
            )

    def test_backend_codex(self):
        args = ca.build_parser().parse_args(
            ["--software", "Demo", "--env-dir", "demo_env", "--backend", "codex"]
        )
        self.assertEqual(args.backend, "codex")


class CliWiringTests(unittest.TestCase):
    def test_run_without_environment_spec_preserves_existing_inputs(self):
        with mock.patch.object(ca, "run_creation_audit", return_value=0) as run_loop:
            result = ca.run(["--software", "Demo", "--env-dir", "demo_env"])

        self.assertEqual(result, 0)
        kwargs = run_loop.call_args.kwargs
        self.assertEqual(kwargs["software"], "Demo")
        self.assertIsNone(kwargs["environment_spec"])
        self.assertIsNone(kwargs["environment_spec_path"])
        self.assertIsNone(kwargs["task_instruction"])
        self.assertIsNone(kwargs["task_instruction_path"])
        self.assertIsNone(kwargs["environment_initial_state"])
        self.assertIsNone(kwargs["environment_initial_state_path"])

    def test_run_derives_software_and_passes_loaded_environment_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = _write_spec(tmp)
            with mock.patch.object(
                ca, "run_creation_audit", return_value=0
            ) as run_loop:
                result = ca.run(
                    [
                        "--environment-spec",
                        str(spec_path),
                        "--env-dir",
                        "gitlab_env",
                    ]
                )

        self.assertEqual(result, 0)
        kwargs = run_loop.call_args.kwargs
        self.assertEqual(kwargs["software"], "GitLab")
        self.assertEqual(kwargs["environment_spec"], VALID_ENVIRONMENT_SPEC)
        self.assertEqual(kwargs["environment_spec_path"], spec_path.resolve())

    def test_run_passes_direct_instruction_and_loaded_environment_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = _write_spec(tmp)
            initial_state_path = _write_initial_state(tmp)
            with mock.patch.object(
                ca, "run_creation_audit", return_value=0
            ) as run_loop:
                result = ca.run(
                    [
                        "--software",
                        "GitLab",
                        "--env-dir",
                        "gitlab_env",
                        "--task-instruction",
                        TASK_INSTRUCTION,
                        "--environment-spec",
                        str(spec_path),
                        "--environment-initial-state",
                        str(initial_state_path),
                    ]
                )

        self.assertEqual(result, 0)
        kwargs = run_loop.call_args.kwargs
        self.assertEqual(kwargs["task_instruction"], TASK_INSTRUCTION)
        self.assertIsNone(kwargs["task_instruction_path"])
        self.assertEqual(kwargs["environment_spec"], VALID_ENVIRONMENT_SPEC)
        self.assertEqual(
            kwargs["environment_initial_state"],
            VALID_ENVIRONMENT_INITIAL_STATE,
        )
        self.assertEqual(
            kwargs["environment_initial_state_path"],
            initial_state_path.resolve(),
        )

    def test_run_extracts_canonical_task_instruction_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            instruction_path = _write_task_instruction(tmp)
            with mock.patch.object(
                ca, "run_creation_audit", return_value=0
            ) as run_loop:
                result = ca.run(
                    [
                        "--software",
                        "GitLab",
                        "--env-dir",
                        "gitlab_env",
                        "--task-instruction-file",
                        str(instruction_path),
                    ]
                )

        self.assertEqual(result, 0)
        kwargs = run_loop.call_args.kwargs
        self.assertEqual(kwargs["task_instruction"], TASK_INSTRUCTION)
        self.assertEqual(
            kwargs["task_instruction_path"], instruction_path.resolve()
        )

    def test_run_rejects_missing_aab_path_before_agent_invocation(self):
        stderr = StringIO()
        with (
            mock.patch.object(ca, "run_creation_audit") as run_loop,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            ca.run(
                [
                    "--software",
                    "GitLab",
                    "--env-dir",
                    "gitlab_env",
                    "--environment-initial-state",
                    "/no/such/environment_initial_state.json",
                ]
            )

        run_loop.assert_not_called()
        self.assertIn("does not exist", stderr.getvalue())

    def test_run_rejects_software_mismatch_before_agent_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = _write_spec(tmp)
            stderr = StringIO()
            with (
                mock.patch.object(ca, "run_creation_audit") as run_loop,
                redirect_stderr(stderr),
                self.assertRaises(SystemExit),
            ):
                ca.run(
                    [
                        "--software",
                        "Moodle",
                        "--environment-spec",
                        str(spec_path),
                        "--env-dir",
                        "demo_env",
                    ]
                )

        run_loop.assert_not_called()
        self.assertIn("does not match", stderr.getvalue())


class ResolveBinTests(unittest.TestCase):
    def test_explicit_path_must_exist(self):
        with self.assertRaises(RuntimeError):
            ca._resolve_bin("/no/such/binary", "FAKE_VAR", "fake")

    def test_env_var_used_when_explicit_missing(self):
        with tempfile.NamedTemporaryFile(prefix="claude-", delete=False) as fh:
            fake = Path(fh.name)
        try:
            with mock.patch.dict(os.environ, {"CLAUDE_BIN": str(fake)}):
                resolved = ca._resolve_bin(None, "CLAUDE_BIN", "claude")
            self.assertEqual(resolved, fake)
        finally:
            fake.unlink(missing_ok=True)

    def test_path_lookup_used_when_neither_set(self):
        with tempfile.NamedTemporaryFile(prefix="claude-", delete=False) as fh:
            fake = Path(fh.name)
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CLAUDE_BIN", None)
                with mock.patch.object(ca.shutil, "which", return_value=str(fake)):
                    resolved = ca._resolve_bin(None, "CLAUDE_BIN", "claude")
            self.assertEqual(resolved, fake)
        finally:
            fake.unlink(missing_ok=True)


class CodexSessionTests(unittest.TestCase):
    def test_new_session_extracts_id_from_codex_transcript(self):
        transcript = (
            "OpenAI Codex\n"
            "workdir: /tmp/workspace\n"
            "session id: 019abcde-1234-7000-8000-0123456789ab\n"
            "codex\nHi!"
        )
        with mock.patch.object(ca, "_run_agent", return_value=transcript):
            session_id = ca._codex_new_session(
                Path("/tmp/codex"), Path("/tmp/workspace")
            )

        self.assertEqual(session_id, "019abcde-1234-7000-8000-0123456789ab")

    def test_new_session_rejects_transcript_without_id(self):
        with (
            mock.patch.object(ca, "_run_agent", return_value="Hi!"),
            self.assertRaisesRegex(RuntimeError, "did not contain a session id"),
        ):
            ca._codex_new_session(Path("/tmp/codex"), Path("/tmp/workspace"))

    def test_pipeline_keeps_creator_and_auditor_in_their_bootstrapped_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(ca, "_resolve_bin", return_value=Path("/tmp/codex")),
                mock.patch.object(
                    ca,
                    "_codex_new_session",
                    side_effect=["creator-session", "auditor-session"],
                ) as new_session,
                mock.patch.object(ca, "_codex_invoke") as invoke,
            ):
                result = ca.run_creation_audit(
                    software="GitLab",
                    env_dir="gitlab_env",
                    backend="codex",
                    platform="linux",
                    blind_nudges=0,
                    audit_rounds=1,
                    start_idx=0,
                    session_id=None,
                    workspace=root,
                    memory_dir=ca._packaged_memory_dir(),
                    audits_dir=root / "audits",
                    logs_dir=root / "logs",
                    claude_bin=None,
                    codex_bin=None,
                    timeout_sec=60,
                )

        self.assertEqual(result, 0)
        self.assertEqual(new_session.call_count, 2)
        self.assertEqual(len(invoke.call_args_list), 3)
        creator_call, audit_explore_call, audit_run_call = invoke.call_args_list
        self.assertEqual(creator_call.kwargs["session_id"], "creator-session")
        self.assertTrue(creator_call.kwargs["resume"])
        self.assertEqual(audit_explore_call.kwargs["session_id"], "auditor-session")
        self.assertTrue(audit_explore_call.kwargs["resume"])
        self.assertEqual(audit_run_call.kwargs["session_id"], "auditor-session")
        self.assertTrue(audit_run_call.kwargs["resume"])

    def test_pipeline_passes_and_logs_aab_context_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_source = root / "environment_spec.json"
            state_source = root / "environment_initial_state.json"
            instruction_source = root / "step4_candidate_selection.json"
            with (
                mock.patch.object(ca, "_resolve_bin", return_value=Path("/tmp/codex")),
                mock.patch.object(ca, "_codex_invoke") as invoke,
            ):
                result = ca.run_creation_audit(
                    software="GitLab",
                    env_dir="gitlab_env",
                    backend="codex",
                    platform="linux",
                    blind_nudges=0,
                    audit_rounds=0,
                    start_idx=0,
                    session_id="creator-session",
                    workspace=root,
                    memory_dir=ca._packaged_memory_dir(),
                    audits_dir=root / "audits",
                    logs_dir=root / "logs",
                    claude_bin=None,
                    codex_bin=None,
                    timeout_sec=60,
                    environment_spec=VALID_ENVIRONMENT_SPEC,
                    environment_spec_path=spec_source,
                    task_instruction=TASK_INSTRUCTION,
                    task_instruction_path=instruction_source,
                    environment_initial_state=VALID_ENVIRONMENT_INITIAL_STATE,
                    environment_initial_state_path=state_source,
                )
                creation_prompt = invoke.call_args.args[1]
                log_text = (root / "logs" / "gitlab_env.txt").read_text(
                    encoding="utf-8"
                )

        self.assertEqual(result, 0)
        self.assertIn(f"Task Instruction: >>>\n{TASK_INSTRUCTION}", creation_prompt)
        self.assertIn("Environment Specification: >>>", creation_prompt)
        self.assertIn("Environment Initial State: >>>", creation_prompt)
        self.assertIn(f"Task Instruction Source: {instruction_source}", log_text)
        self.assertIn(f"EnvironmentSpec Source: {spec_source}", log_text)
        self.assertIn(f"Environment Initial State Source: {state_source}", log_text)


class PromptAssemblyTests(unittest.TestCase):
    """Prompts must reference files at their real on-disk path, not at the
    workspace root."""

    def test_initial_prompt_uses_real_creation_path(self):
        prompts = ca._packaged_memory_dir()
        text = ca._initial_prompt("Demo", "demo_env", prompts)
        expected = (prompts / "env_creation_notes" / "prompt.md").as_posix()
        self.assertIn(f"@{expected}", text)
        self.assertIn("Demo", text)
        self.assertIn("demo_env", text)

    def test_audit_run_prompt_uses_real_audit_path(self):
        prompts = ca._packaged_memory_dir()
        with tempfile.TemporaryDirectory() as tmp:
            audits = Path(tmp)
            text = ca._audit_run_prompt("demo_env", audits, prompts)
        self.assertIn(f"@{(prompts / 'audit_prompt.md').as_posix()}", text)
        self.assertIn("demo_env", text)

    def test_nudge_prompt_uses_real_creation_path(self):
        prompts = ca._packaged_memory_dir()
        text = ca._nudge_prompt(prompts)
        expected = (prompts / "env_creation_notes" / "prompt.md").as_posix()
        self.assertIn(f"@{expected}", text)

    def test_existing_prompts_are_unchanged_without_environment_spec(self):
        prompts = ca._packaged_memory_dir()
        creation_path = (prompts / "env_creation_notes" / "prompt.md").as_posix()
        expected_initial = (
            f"read @{creation_path} and follow the prompt. "
            "target application is Demo and target env directory is demo_env. "
            "Do not enter plan mode (although you are strongly encouraged to plan "
            "before making code edits), or ask me for any input at any time. "
            "All information is already present in the prompt file."
        )
        expected_nudge = (
            f"reread @{creation_path}. "
            "you haven't completed the task yet. (Unrelated Context: remember to "
            "use the visual_grounding MCP tool to interact with the running "
            "environment)"
        )
        expected_feedback = (
            "An independent audit of your progress was performed. Here is the "
            "audit: audit text. Please fix the issues. (Unrelated Context: "
            "remember to use the visual_grounding MCP tool to interact with the "
            "running environment)"
        )
        self.assertEqual(
            ca._initial_prompt("Demo", "demo_env", prompts), expected_initial
        )
        self.assertEqual(ca._nudge_prompt(prompts), expected_nudge)
        self.assertEqual(ca._audit_feedback_prompt("audit text"), expected_feedback)

        with tempfile.TemporaryDirectory() as tmp:
            audits = Path(tmp)
            expected_audit = (
                f"read @{(prompts / 'audit_prompt.md').as_posix()} and follow "
                "the prompt. target env directory is "
                "@benchmarks/cua_world/environments/demo_env. "
                f"Note: save file is {(audits / 'audit_demo_env.md').as_posix()}"
            )
            self.assertEqual(
                ca._audit_run_prompt("demo_env", audits, prompts),
                expected_audit,
            )

    def test_creation_and_audit_prompts_render_separate_aab_inputs(self):
        prompts = ca._packaged_memory_dir()
        rendered_spec = json.dumps(
            {"environment_spec": VALID_ENVIRONMENT_SPEC},
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        rendered_initial_state = json.dumps(
            {"environment_initial_state": VALID_ENVIRONMENT_INITIAL_STATE},
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        creation = ca._initial_prompt(
            "GitLab",
            "gitlab_env",
            prompts,
            environment_spec=VALID_ENVIRONMENT_SPEC,
            task_instruction=TASK_INSTRUCTION,
            environment_initial_state=VALID_ENVIRONMENT_INITIAL_STATE,
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit = ca._audit_run_prompt(
                "gitlab_env",
                Path(tmp),
                prompts,
                environment_spec=VALID_ENVIRONMENT_SPEC,
                task_instruction=TASK_INSTRUCTION,
                environment_initial_state=VALID_ENVIRONMENT_INITIAL_STATE,
            )

        for prompt in (creation, audit):
            self.assertIn("## Input", prompt)
            self.assertIn("Software: >>>\nGitLab\n<<<", prompt)
            self.assertIn(
                f"Task Instruction: >>>\n{TASK_INSTRUCTION}\n<<<",
                prompt,
            )
            self.assertIn(
                f"Environment Specification: >>>\n{rendered_spec}\n<<<",
                prompt,
            )
            self.assertIn(
                "Environment Initial State: >>>\n"
                f"{rendered_initial_state}\n<<<",
                prompt,
            )
            self.assertLess(
                prompt.index("Environment Specification: >>>"),
                prompt.index("Environment Initial State: >>>"),
            )
            self.assertIn("stable, shared world", prompt)
            self.assertIn("per-task episode setup", prompt)
            self.assertIn("Preserve its exact wording and intent", prompt)

        self.assertIn("shared env.json and environment hooks", creation)
        self.assertIn("Audit each supplied input", audit)
        self.assertIn("task_preconditions", creation)
        self.assertIn("episode_start", creation)
        self.assertIn("has_license", creation)
        self.assertIn("apply_license_template", creation)

    def test_iterative_prompts_restate_all_aab_inputs(self):
        prompts = ca._packaged_memory_dir()
        kwargs = {
            "environment_spec": VALID_ENVIRONMENT_SPEC,
            "task_instruction": TASK_INSTRUCTION,
            "environment_initial_state": VALID_ENVIRONMENT_INITIAL_STATE,
            "software": "GitLab",
        }
        nudge = ca._nudge_prompt(prompts, **kwargs)
        feedback = ca._audit_feedback_prompt("audit text", **kwargs)

        for prompt in (nudge, feedback):
            self.assertIn(TASK_INSTRUCTION, prompt)
            self.assertIn("Environment Specification: >>>", prompt)
            self.assertIn("Environment Initial State: >>>", prompt)

    def test_task_instruction_only_is_rendered_without_invented_context(self):
        prompts = ca._packaged_memory_dir()
        prompt = ca._initial_prompt(
            "GitLab",
            "gitlab_env",
            prompts,
            task_instruction=TASK_INSTRUCTION,
        )

        self.assertIn(TASK_INSTRUCTION, prompt)
        self.assertNotIn("Environment Specification: >>>", prompt)
        self.assertNotIn("Environment Initial State: >>>", prompt)


if __name__ == "__main__":
    unittest.main()
