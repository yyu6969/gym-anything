"""Exercise native Harbor execution and grading boundaries without a guest or API."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("harbor")

from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.installed.codex import Codex
from harbor.environments.base import ExecResult
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from gym_anything.contracts import RunnerRuntimeInfo
from gym_anything.env import GymAnythingEnv
from gym_anything.integrations.harbor.container import apply_verifier_config
from gym_anything.integrations.harbor.environment import GymAnythingEnvironment
from gym_anything.integrations.harbor.packaging import directory_digest
from gym_anything.runtime.runners.base import BaseRunner
from gym_anything.specs import EnvSpec, TaskSpec


@pytest.fixture(autouse=True)
def vlm_query(monkeypatch):
    query = Mock(side_effect=AssertionError("Unexpected VLM request"))
    monkeypatch.setattr("gym_anything.verification.vlm_checklist.query_vlm", query)
    monkeypatch.setattr("gym_anything.verification.runner.query_vlm", query)
    return query


@pytest.fixture
def backend(tmp_path):
    environment_dir = tmp_path / "environment"
    bundle = environment_dir / "verifier"
    bundle.mkdir(parents=True)
    (bundle / "checklist.json").write_text(json.dumps({
        "schema_version": "binary_outcome_checklist",
        "scoring_policy": "external_policy",
        "items": [{"id": "website", "criterion": "Website updated through the GUI", "scoring": "binary"}],
    }))
    (environment_dir / "gym-anything.json").write_text(json.dumps({
        "verifier": {
            "mode": "vlm_checklist", "vlm_backend": "local",
            "vlm_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "vlm_model": "gemini-3.5-flash",
        },
        "verifier_bundle": "verifier",
        "verifier_bundle_sha256": directory_digest(bundle),
    }))
    backend = GymAnythingEnvironment(
        environment_dir=environment_dir,
        environment_name="native-agent-test",
        session_id="native-agent-test",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(),
    )
    runner = Mock(spec=BaseRunner)
    runner.get_runtime_info.return_value = RunnerRuntimeInfo(platform_family="linux")
    runner.supports_live_recording.return_value = False
    runner.run_hook.return_value = 0
    runner.capture_screenshot.side_effect = lambda path: Path(path).write_bytes(b"screenshot")
    runner.copy_from.side_effect = lambda source, target: Path(target).write_text(source)
    spec = EnvSpec.from_dict({
        "id": "native-agent-test",
        "observation": [{"type": "rgb_screen", "resolution": [64, 64]}],
        "recording": {"output_dir": str(tmp_path / "episodes")},
        "diagnostics": True,
    })
    task = TaskSpec.from_dict({
        "id": "website", "description": "Update the website",
        "hooks": {"post_task": "echo exported"},
        "metadata": {"post_task_settle_sec": 0},
        "success": {"mode": "program", "spec": {"program": "missing.py::verify"}},
    })
    with patch.object(GymAnythingEnv, "_select_runner", return_value=runner):
        env = GymAnythingEnv(spec, task)
    apply_verifier_config(env, backend._ga_config)
    with patch.object(env, "_dockerhub_login_in_guest"):
        env.reset(seed=0)
    backend._env = env
    yield backend
    backend._stop_sync()


@pytest.fixture
def local_shell(backend):
    if shutil.which("bash") is None:
        pytest.skip("Bash is required for native Harbor command tests")

    def execute(command, *, env=None, timeout=600, **kwargs):
        return subprocess.run(
            ["bash", "-c", command], env={**os.environ, **(env or {})},
            capture_output=True, timeout=timeout,
        ).returncode

    def capture(command):
        return subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, check=True,
        ).stdout

    runner = backend.gym_env.runner
    runner.exec.side_effect = execute
    runner.exec_capture.side_effect = capture
    return runner


def test_native_command_uses_bash_and_preserves_cwd_env_and_quoting(backend, local_shell, tmp_path):
    cwd = tmp_path / "working ' directory"
    cwd.mkdir()
    value = "literal ' quote and $dollar"
    result = asyncio.run(backend.exec(
        'set -o pipefail; printf "%s\\n" "$0" "$PWD" "$NATIVE_TEST_VALUE"',
        cwd=str(cwd), env={"NATIVE_TEST_VALUE": value}, timeout_sec=7, user="ga",
    ))
    assert result.return_code == 0
    assert result.stdout == f"bash\n{cwd}\n{value}\n"
    assert result.stderr == ""
    assert local_shell.exec.call_args.kwargs == {
        "env": {"NATIVE_TEST_VALUE": value}, "user": "ga", "use_pty": False, "timeout": 7,
    }


@pytest.mark.parametrize("ending,code", [("exit 23", 23), ("false | true", 1)])
def test_native_command_preserves_failure_and_output(backend, local_shell, ending, code):
    result = asyncio.run(backend.exec(
        'set -o pipefail; printf "native stdout\\n"; printf "native stderr\\n" >&2; ' + ending,
    ))
    assert result.return_code == code
    assert result.stdout == "native stdout\n"
    assert result.stderr == "native stderr\n"


def test_teardown_without_verification_preserves_evidence_without_grading(backend, vlm_query):
    env = backend.gym_env
    runner = env.runner
    episode = env.episode_dir
    with patch.object(env._verifier, "evaluate", wraps=env._verifier.evaluate) as grader:
        asyncio.run(backend.stop(delete=True))
        asyncio.run(backend.stop(delete=True))
        grader.assert_not_called()
    vlm_query.assert_not_called()
    runner.run_hook.assert_not_called()
    runner.stop.assert_called_once()
    assert backend._env is None
    assert (episode / "final.png").read_bytes() == b"screenshot"
    assert (episode / "env_setup_post_start.log").is_file()
    assert "verifier" not in json.loads((episode / "summary.json").read_text())
    events = [json.loads(line)["event"] for line in (episode / "traj.jsonl").read_text().splitlines()]
    assert events.count("finalize") == 1


def test_formal_verification_grades_authoritative_bundle_once(backend, vlm_query):
    env = backend.gym_env
    runner = env.runner
    episode = env.episode_dir
    vlm_query.side_effect = None
    vlm_query.return_value = {
        "success": True, "parsed": {"items": [{"id": "website", "verdict": "PASS"}]},
    }
    backend._write_guest_file = Mock()
    command = "bash /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1"
    with patch.object(env._verifier, "evaluate", wraps=env._verifier.evaluate) as grader:
        result = asyncio.run(backend.exec(command))
        captures = runner.capture_screenshot.call_count
        assert asyncio.run(backend.exec(command)) is result
        asyncio.run(backend.stop(delete=True))
        grader.assert_called_once()
    vlm_query.assert_called_once()
    query = vlm_query.call_args.kwargs
    assert "Website updated through the GUI" in query["prompt"]
    assert query["config"]["model"] == "gemini-3.5-flash"
    assert query["config"]["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert str(episode / "final.png") in query["images"]
    assert result.return_code == 0
    writes = dict(call.args for call in backend._write_guest_file.call_args_list)
    assert json.loads(writes["/logs/verifier/reward.json"]) == {
        "reward": 1.0, "passed": 1.0, "score": 100.0,
    }
    assert json.loads((episode / "summary.json").read_text())["verifier"]["passed"] is True
    runner.run_hook.assert_called_once_with("echo exported", stage="post_task")
    assert (episode / "post_verification.png").is_file()
    assert runner.capture_screenshot.call_count == captures
    runner.stop.assert_called_once()


def test_failed_native_agent_installation_stops_without_vlm_request(backend, vlm_query, tmp_path):
    env = backend.gym_env
    agent = Codex(logs_dir=tmp_path / "agent", model_name="gpt-6.1-sol")

    async def execute(command, **kwargs):
        if "npm install -g @openai/codex" in command:
            return ExecResult(return_code=37, stdout="", stderr="simulated installation failure")
        return ExecResult(return_code=0, stdout="", stderr="")

    with patch.object(agent, "_installed_codex_satisfies_version", new=AsyncMock(return_value=False)), \
         patch.object(backend, "exec", new=AsyncMock(side_effect=execute)), \
         patch.object(env._verifier, "evaluate", wraps=env._verifier.evaluate) as grader:
        try:
            with pytest.raises(NonZeroAgentExitCodeError, match="simulated installation failure"):
                asyncio.run(agent.setup(backend))
        finally:
            asyncio.run(backend.stop(delete=True))
        grader.assert_not_called()
    vlm_query.assert_not_called()
    env.runner.stop.assert_called_once()
    assert backend._env is None
