"""Compile and grade deployment assets without starting a runner."""

import json
import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gym_anything.integrations.harbor.compile import compile_task
from gym_anything.integrations.harbor.container import (
    _Runtime, apply_verifier_config, boot_env, load_task_config, rewards_from_verdict,
)
from gym_anything.integrations.harbor.packaging import directory_digest
from gym_anything.verification.vlm_checklist import (
    build_verification_prompt, compute_scores, evaluate_vlm_checklist,
)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "demo_env"
    for name in ("scripts", "config", "assets", "tasks/first", "tasks/second"):
        (root / name).mkdir(parents=True)
        (root / name / "fixture.txt").write_text(name)
    (root / "env.json").write_text(json.dumps({"id": "demo_env@1", "mounts": [
        {"source": "./scripts", "target": "/workspace/scripts", "mode": "ro"}]}))
    for task in ("first", "second"):
        (root / "tasks" / task / "task.json").write_text(json.dumps({"id": task + "@1", "description": "Original instruction"}))
        (root / "tasks" / task / "verifier.py").write_text("raise AssertionError('stub must not run')")
        (root / "tasks" / task / "vlm_checklist.json").write_text("{}")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "old.png").write_bytes(b"image")
    (root / ".env").write_text("SECRET=private")
    return root


@pytest.fixture
def checklist():
    return {"schema_version": "binary_outcome_checklist", "scoring_policy": "external_policy",
            "items": [{"id": str(i), "criterion": "A complete outcome", "description": "Full or partial credit in original prose", "scoring": "binary"} for i in range(4)]}


@pytest.fixture
def bundle(tmp_path, checklist):
    root = tmp_path / "bundle"
    root.mkdir()
    for name, value in (("checklist.json", checklist), ("manifest.json", {}), ("source.json", {})):
        (root / name).write_text(json.dumps(value))
    return root


def test_backward_compatibility_and_override(source, tmp_path):
    old = compile_task("demo_env", "first", tmp_path / "old", env_dir=source)
    config = json.loads((old / "environment/gym-anything.json").read_text())
    assert "environment_snapshot" not in config
    assert "packaging_mode" not in config
    assert "ARG GYM_ANYTHING_REF=main" in (old / "environment/Dockerfile").read_text()
    assert (old / "instruction.md").read_text() == "Original instruction\n"
    new = compile_task("demo_env", "first", tmp_path / "new", env_dir=source, instruction="Selected\nAAB instruction")
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    assert tomllib.loads((new / "task.toml").read_text())["task"]["description"] == "Selected\nAAB instruction"
    assert (new / "instruction.md").read_text() == "Selected\nAAB instruction\n"


def test_registry_ref_requires_immutable_pin(source, tmp_path):
    for ref in ("main", "v1.0", "a" * 7):
        with pytest.raises(ValueError, match="immutable"):
            compile_task("demo_env", "first", tmp_path, env_dir=source, packaging_mode="registry_ref", gym_anything_ref=ref)
    task = compile_task("demo_env", "first", tmp_path, env_dir=source, packaging_mode="registry_ref", gym_anything_ref="a" * 40)
    assert not (task / "environment/gym-environment").exists()
    assert "ARG GYM_ANYTHING_REF=" + "a" * 40 in (task / "environment/Dockerfile").read_text()


def test_snapshot_and_external_bundle_are_portable(source, bundle, tmp_path, monkeypatch):
    before = directory_digest(source)
    out = compile_task("demo_env", "first", tmp_path / "out", env_dir=source,
                       instruction="Chosen", verifier=None, verifier_bundle=bundle, packaging_mode="vendored_snapshot")
    assert directory_digest(source) == before
    snapshot = out / "environment/gym-environment"
    assert (snapshot / "tasks/first/task.json").is_file()
    assert not (snapshot / "tasks/second").exists()
    assert not (snapshot / "artifacts").exists()
    assert not (snapshot / ".env").exists()
    for name in ("scripts", "assets", "config"):
        assert (snapshot / name / "fixture.txt").is_file()
    assert (out / "environment/verifier/checklist.json").read_bytes() == (bundle / "checklist.json").read_bytes()
    dockerfile = (out / "environment/Dockerfile").read_text()
    assert "COPY verifier /harbor-task/verifier" in dockerfile
    assert "COPY gym-environment /harbor-task/gym-environment" in dockerfile
    assert "COPY gym-runtime" in dockerfile
    assert "git+https" not in dockerfile
    assert (out / "environment/gym-runtime/src/gym_anything/verification/vlm_checklist.py").is_file()
    config = load_task_config(out / "environment/gym-anything.json")
    relocated = tmp_path / "relocated"
    shutil.copytree(out, relocated)
    config = load_task_config(relocated / "environment/gym-anything.json")
    env = Mock()
    env.task_spec = SimpleNamespace(description="original", init=SimpleNamespace(reward_type="continuous"))
    env.env_spec.dockerfile = None
    loader = Mock(return_value=env)
    monkeypatch.setattr("gym_anything.config.loading.from_config", loader)
    monkeypatch.setattr("gym_anything.registry.resolve_environment_dir", Mock(side_effect=AssertionError("registry unavailable")))
    # Conflicting cwd sources must not capture packaged mount paths.
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)
    boot_env(config)
    expected_root = loader.call_args.args[0]
    assert expected_root != relocated / "environment/gym-environment"
    assert directory_digest(expected_root) == config["environment_snapshot_sha256"]
    assert loader.call_args.kwargs["overrides"]["mounts"][0]["source"] == str(expected_root / "scripts")
    assert env.task_spec.description == "Chosen"
    assert env.task_spec.init.reward_type == "sparse"
    apply_verifier_config(env, config, {"GEMINI_API_KEY": "key"})
    overrides = env.set_verifier_overrides.call_args.args[0]
    assert overrides["GYM_ANYTHING_VERIFIER_MODE"] == "vlm_checklist"
    assert overrides["GYM_ANYTHING_VLM_CHECKLIST_CHECKLIST"] == str(relocated / "environment/verifier/checklist.json")
    assert overrides["VLM_API_KEY"] == "key"
    assert directory_digest(source) == before


@pytest.mark.parametrize("values,expected", [([1, 1, 0, 1], {"reward": 0., "passed": 0., "score": 75.}), ([1] * 4, {"reward": 1., "passed": 1., "score": 100.})])
def test_binary_scores_and_reward(values, expected, checklist):
    verdicts = {"items": [{"id": str(i), "verdict": "PASS" if value else "FAIL"} for i, value in enumerate(values)]}
    scores = compute_scores(verdicts, checklist, completion_threshold=0, integrity_threshold=0)
    assert scores["item_scores"] == values
    assert rewards_from_verdict(scores["reward"], {"passed": scores["passed"], "score": scores["final_score"]}) == expected
    from gym_anything.env import GymAnythingEnv
    env = GymAnythingEnv.__new__(GymAnythingEnv)
    env.task_spec = SimpleNamespace(init=SimpleNamespace(reward_type="continuous"))
    assert env._final_reward({"verifier": {"passed": scores["passed"], "score": scores["final_score"], "scores": scores}}, current_reward=.7) == expected["reward"]


@pytest.mark.parametrize("invalid", ["partial", "pass", "yes", .5, 1, None])
def test_no_partial_credit_or_synonyms(invalid, checklist):
    scores = compute_scores({"items": [{"id": "0", "verdict": invalid}]}, checklist, completion_threshold=0, integrity_threshold=0)
    assert scores["item_scores"] == [0] * 4
    assert scores["reward"] == 0


def test_binary_prompt_and_schema_validation(checklist):
    prompt = build_verification_prompt(checklist=checklist, task_description="Do it", image_count=3)
    assert "PASS or FAIL" in prompt
    assert "PASS requires clear evidence" in prompt
    checklist["items"][0]["points"] = 1
    with pytest.raises(ValueError, match="points or weights"):
        compute_scores({}, checklist, completion_threshold=100, integrity_threshold=1)


def test_authoritative_verifier_runs_through_normal_gym_runner(source, bundle, tmp_path, monkeypatch):
    from gym_anything.config.loading import from_config
    from gym_anything.verification.runner import VerifierRunner
    # Use a real validated nearby environment spec while retaining the stub task.
    reference = Path(__file__).resolve().parents[1] / "benchmarks/cua_world/environments/gitlab_env/env.json"
    (source / "env.json").write_bytes(reference.read_bytes())
    out = compile_task("demo_env", "first", tmp_path / "out", env_dir=source,
                       verifier_bundle=bundle, packaging_mode="vendored_snapshot")
    config = load_task_config(out / "environment/gym-anything.json")
    env = from_config(out / "environment/gym-environment", task_id="first")
    apply_verifier_config(env, config)
    image = tmp_path / "screen.png"
    image.write_bytes(b"image")
    monkeypatch.setattr(VerifierRunner, "_load_traj", lambda *a: {"frames": [str(image)]})
    monkeypatch.setattr(VerifierRunner, "_run_program_verifier", Mock(side_effect=AssertionError("stub invoked")))
    monkeypatch.setattr("gym_anything.verification.vlm_checklist.query_vlm", lambda **kwargs: {"success": True, "parsed": {"items": [{"id": str(i), "verdict": "PASS" if i != 2 else "FAIL"} for i in range(4)]}})
    result = VerifierRunner().evaluate(Mock(), env.env_spec, env.task_spec, tmp_path, source, source / "tasks/first", env._verifier_overrides)
    assert result["score"] == 75
    assert result["passed"] is False
    assert result["checklist_path"] == str(out / "environment/verifier/checklist.json")
    (out / "environment/verifier/checklist.json").write_text("{}")
    with pytest.raises(ValueError, match="digest mismatch"):
        apply_verifier_config(env, config)


def test_snapshot_rejects_external_mount(source, tmp_path):
    (source / "env.json").write_text(json.dumps({"mounts": [{"source": str(tmp_path), "target": "/workspace"}]}))
    with pytest.raises(ValueError, match="outside environment"):
        compile_task("demo_env", "first", tmp_path / "out", env_dir=source, packaging_mode="vendored_snapshot")


def test_docker_runtime_and_custom_backend_share_external_verifier(source, bundle, tmp_path, monkeypatch):
    # Exercise the real backend with only Harbor's interface types replaced.
    # Harbor is optional, and neither path needs a VM for contract testing.
    base = types.ModuleType("harbor.environments.base")
    base.BaseEnvironment = object
    base.ExecResult = SimpleNamespace
    capabilities = types.ModuleType("harbor.environments.capabilities")
    capabilities.EnvironmentCapabilities = object
    monkeypatch.setitem(sys.modules, "harbor.environments.base", base)
    monkeypatch.setitem(sys.modules, "harbor.environments.capabilities", capabilities)
    module_path = Path(__file__).resolve().parents[1] / "src/gym_anything/integrations/harbor/environment.py"
    spec = importlib.util.spec_from_file_location("gym_anything.integrations.harbor._backend_contract_test", module_path)
    backend_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend_module)
    out = compile_task("demo_env", "first", tmp_path / "out", env_dir=source,
                       verifier_bundle=bundle, packaging_mode="vendored_snapshot")
    config = load_task_config(out / "environment/gym-anything.json")
    backend = backend_module.GymAnythingEnvironment()
    backend.environment_dir = out / "environment"
    assert backend._ga_config == config
    env = Mock()
    env.step.return_value = ({}, 0., True, {"verifier": {"passed": False, "score": 75}})
    backend._env = env
    monkeypatch.setattr(backend, "_write_guest_file", Mock())
    backend._verify_sync()
    custom_overrides = dict(env.set_verifier_overrides.call_args.args[0])
    guest_reward = backend._write_guest_file.call_args_list[0].args
    assert json.loads(guest_reward[1]) == {"reward": 0., "passed": 0., "score": 75.}
    env.step.assert_called_once_with([], mark_done=True)
    env.reset_mock()
    _Runtime(env, config).finalize()
    docker_overrides = env.set_verifier_overrides.call_args.args[0]
    assert docker_overrides == custom_overrides
    assert docker_overrides["GYM_ANYTHING_VLM_CHECKLIST_CHECKLIST"] == str(out / "environment/verifier/checklist.json")
    # Grading invoked Gym directly, never a verifier program in the guest.
    env.runner.exec.assert_not_called()


def test_duplicate_and_missing_binary_verdicts_fail_closed(checklist):
    records = [{"id": "0", "verdict": "PASS"}, {"id": "0", "verdict": "PASS"}, {"id": "1", "verdict": "PASS"}]
    scores = compute_scores({"items": records}, checklist, completion_threshold=0, integrity_threshold=0)
    assert scores["item_scores"] == [0, 1, 0, 0]
    assert scores["final_score"] == 25 and scores["reward"] == 0


def test_recompile_removes_prior_packaged_assets(source, bundle, tmp_path):
    out = compile_task("demo_env", "first", tmp_path / "out", env_dir=source,
                       verifier_bundle=bundle, packaging_mode="vendored_snapshot")
    compile_task("demo_env", "first", tmp_path / "out", env_dir=source)
    assert not (out / "environment/verifier").exists()
    assert not (out / "environment/gym-environment").exists()
    assert not (out / "environment/gym-runtime").exists()


def test_snapshot_rejects_external_symlink(source, tmp_path):
    secret = tmp_path / "private"
    secret.write_text("private")
    (source / "assets/escape").symlink_to(secret)
    before = directory_digest(source)
    with pytest.raises(ValueError, match="symlink escapes"):
        compile_task("demo_env", "first", tmp_path / "out", env_dir=source, packaging_mode="vendored_snapshot")
    assert directory_digest(source) == before


def test_vendored_runtime_loads_snapshot_without_source_checkout(tmp_path):
    canonical = Path(__file__).resolve().parents[1] / "benchmarks/cua_world/environments/gitlab_env"
    out = compile_task("gitlab_env", "add_mit_license", tmp_path / "out", env_dir=canonical,
                       packaging_mode="vendored_snapshot")
    # Import the packaged Gym source in a fresh process, with no source
    # registry in cwd or PYTHONPATH. Suppress reset so no runner can boot.
    script = """
import sys
from pathlib import Path
from unittest.mock import Mock
sys.dont_write_bytecode = True
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'gym-runtime/src'))
import gym_anything
assert Path(gym_anything.__file__).is_relative_to(root / 'gym-runtime')
from gym_anything.env import GymAnythingEnv
from gym_anything.integrations.harbor.container import boot_env, load_task_config
import gym_anything.registry
gym_anything.registry.resolve_environment_dir = Mock(side_effect=AssertionError('source registry used'))
GymAnythingEnv.reset = Mock()
env = boot_env(load_task_config(root / 'gym-anything.json'))
assert env.task_spec.id == 'add_mit_license@1'
assert all(Path(m.source).is_relative_to(env._env_root) for m in env.env_spec.mounts)
assert env._env_root != root / 'gym-environment'
assert Path(env.env_spec.dockerfile).is_file()
assert Path(env.env_spec.dockerfile).is_relative_to(root / 'gym-runtime')
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(out / "environment")],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_runtime_writes_do_not_mutate_packaged_snapshot(source, tmp_path, monkeypatch):
    out = compile_task("demo_env", "first", tmp_path / "out", env_dir=source,
                       packaging_mode="vendored_snapshot")
    config = load_task_config(out / "environment/gym-anything.json")
    snapshot = out / "environment/gym-environment"
    before = directory_digest(snapshot)
    env = Mock()
    env.env_spec.dockerfile = None
    loader = Mock(return_value=env)
    monkeypatch.setattr("gym_anything.config.loading.from_config", loader)
    boot_env(config)
    runtime_root = loader.call_args.args[0]
    (runtime_root / "scripts/fixture.txt").write_text("runtime changed")
    assert directory_digest(runtime_root) != before
    assert directory_digest(snapshot) == before
