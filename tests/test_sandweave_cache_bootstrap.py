"""Exercise SDK cache bootstrap offline; no installation or worker is started."""
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sandweave = pytest.importorskip("sandweave")
from sandweave.sandbox import preparation, targets, workspace
from sandweave.sandbox.errors import CacheMiss, ResourceUnavailable

from gym_anything.env import GymAnythingEnv
from gym_anything.integrations.harbor.container import boot_env
from gym_anything.runtime.runners.sandweave import SandweaveRunner
from gym_anything.specs import EnvSpec, TaskSpec


@pytest.fixture
def cache_runtime(tmp_path, monkeypatch):
    home = tmp_path / "machine-cache" / "sandweave"
    monkeypatch.setenv("GYM_ANYTHING_CACHE_DIR", str(home.parent))
    monkeypatch.setenv("SANDWEAVE_HOME", str(home))
    for name in ("SANDWEAVE_ASSETS", "GYM_ANYTHING_SANDWEAVE_TARGET"):
        monkeypatch.delenv(name, raising=False)
    assert workspace.home() == home
    with pytest.raises(ResourceUnavailable, match="runtime files are not configured"):
        workspace.assets()

    spec = EnvSpec.from_dict({
        "id": "gitlab_task_414_env@0.1", "runner": "sandweave",
        "resources": {"cpu": 2, "mem_gb": 4, "gpu": 0, "net": True},
        "recording": {"enable": False, "output_dir": str(tmp_path / "episodes")},
        "hooks": {"pre_start": "prepare-gitlab", "post_start": "start-gitlab"},
    })
    runner = SandweaveRunner(spec)
    runner.set_checkpoint_key("post_start", "add_mit_license@1")
    prepare = Mock(return_value=preparation.Installation(home, tmp_path / "assets"))
    monkeypatch.setattr(preparation, "ensure", prepare)

    # Keep the actual SDK connect/local_connection and workspace path selection.
    # Only first-use provisioning and the worker transport are replaced.
    worker = home / "connections" / "offline-worker"
    worker.mkdir(parents=True)
    (worker / "worker.json").write_text(json.dumps({"port": 1, "token": "offline"}))
    monkeypatch.setattr(targets, "worker_key", lambda assets: "offline-worker")
    snapshot = Mock(return_value={"verification": {"status": "passed"}})
    connections = []

    def connect_transport(*args, **kwargs):
        connection = Mock()
        connection.call.side_effect = lambda operation, **kw: (
            snapshot(**kw) if operation == "snapshot_info" else {}
        )
        connections.append(connection)
        return connection

    monkeypatch.setattr(targets, "Connection", connect_transport)
    launch = Mock(side_effect=AssertionError("must not launch a worker"))
    monkeypatch.setattr(targets.subprocess, "Popen", launch)
    yield SimpleNamespace(runner=runner, prepare=prepare, snapshot=snapshot,
                          connections=connections, home=home, launch=launch)
    launch.assert_not_called()
    for connection in connections:
        connection.close.assert_called_once()


@pytest.mark.parametrize("outcome,expected", [("passed", True), ("failed", False), ("missing", False)])
def test_checkpoint_probe_prepares_unconfigured_workspace(cache_runtime, outcome, expected):
    runtime = cache_runtime
    if outcome == "missing":
        runtime.snapshot.side_effect = CacheMiss("no checkpoint")
    else:
        runtime.snapshot.return_value = {"verification": {"status": outcome}}
    assert runtime.runner.checkpoint_exists() is expected
    recipe = runtime.prepare.call_args.args[0]
    assert isinstance(recipe, dict)
    assert recipe == runtime.runner._template.resolve()
    runtime.snapshot.assert_called_once_with(reference=runtime.runner._checkpoint_key())
    assert workspace.home() == runtime.home
    assert runtime.runner._sandbox is None


@pytest.mark.parametrize("stage", ["preparation", "lookup"])
def test_runtime_errors_are_not_hidden_as_cache_misses(cache_runtime, stage):
    runtime = cache_runtime
    failure = ResourceUnavailable("namespace permission denied")
    (runtime.prepare if stage == "preparation" else runtime.snapshot).side_effect = failure
    with pytest.raises(ResourceUnavailable) as caught:
        runtime.runner.checkpoint_exists()
    assert caught.value is failure
    assert runtime.runner._sandbox is None


def test_remote_checkpoint_probe_keeps_target_and_resolved_recipe(cache_runtime, monkeypatch):
    runtime = cache_runtime
    runtime.runner.target = "configured-remote-worker"
    connection = Mock()
    connection.call.return_value = {"verification": {"status": "passed"}}
    connect = Mock(return_value=connection)
    monkeypatch.setattr(targets, "connect", connect)
    assert runtime.runner.checkpoint_exists()
    connect.assert_called_once_with(runtime.runner.target, template=runtime.runner._template.resolve())
    connection.close.assert_called_once()


@pytest.mark.parametrize("mode", ["cache_miss", "cache_hit", "disabled", "force_build"])
@pytest.mark.parametrize("explicit_home", [False, True])
def test_harbor_boot_reset_preserves_cache_and_hook_semantics(cache_runtime, monkeypatch, mode, explicit_home):
    runtime = cache_runtime
    if explicit_home:
        # Explicit SDK homes win over a conflicting generic Gym cache root.
        monkeypatch.setenv("GYM_ANYTHING_CACHE_DIR", str(runtime.home.parent / "other-cache"))
    else:
        monkeypatch.delenv("SANDWEAVE_HOME")
    runner = runtime.runner
    if mode != "cache_hit":
        runtime.snapshot.side_effect = CacheMiss("no checkpoint")
    sandbox = Mock(id="offline-sandbox", info={})
    sandbox.run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
    launch_sandbox = Mock(return_value=sandbox)
    monkeypatch.setattr(sandweave, "Sandbox", launch_sandbox)
    hooks = Mock()
    monkeypatch.setattr(runner, "run_hook", hooks)
    checkpoint = Mock(return_value=True)
    monkeypatch.setattr(runner, "create_checkpoint", checkpoint)
    monkeypatch.setattr(GymAnythingEnv, "_select_runner", lambda self, spec: runner)
    task = TaskSpec.from_dict({"id": "add_mit_license@1", "hooks": {"pre_task": "prepare-task"}})
    env = GymAnythingEnv(runner.spec, task)
    monkeypatch.setattr(env, "_dockerhub_login_in_guest", Mock())
    monkeypatch.setattr(env, "_capture_observation", Mock(return_value={}))
    monkeypatch.setattr("gym_anything.config.loading.from_config", Mock(return_value=env))
    config = {"env_dir": str(runtime.home.parent), "task_id": "add_mit_license"}
    if mode == "disabled":
        config["use_cache"] = False
    try:
        assert boot_env(config, force_build=mode == "force_build") is env
        with (env.episode_dir / "traj.jsonl").open() as stream:
            reset = json.loads(stream.readline())
        assert reset["use_cache"] is (mode in ("cache_miss", "cache_hit"))
        assert reset["checkpoint_loaded"] is (mode == "cache_hit")
        executed_stages = [call.kwargs["stage"] for call in hooks.call_args_list]
        assert executed_stages == (["pre_task"] if mode == "cache_hit" else
                                   ["pre_start", "post_start", "pre_task"])
        if mode == "cache_hit":
            assert launch_sandbox.call_args.kwargs["cache"] == runner._checkpoint_key()
            checkpoint.assert_not_called()
        else:
            assert launch_sandbox.call_args.kwargs["template"] is runner._template
            assert "cache" not in launch_sandbox.call_args.kwargs
        assert runtime.snapshot.call_count == {"cache_miss": 2, "cache_hit": 1}.get(mode, 0)
        assert workspace.home() == runtime.home
    finally:
        env.close()


@pytest.mark.parametrize("explicit_home", [None, "/explicit/sdk-home"])
def test_qemu_boot_does_not_change_sandweave_home(tmp_path, monkeypatch, explicit_home):
    if explicit_home is None:
        monkeypatch.delenv("SANDWEAVE_HOME", raising=False)
    else:
        monkeypatch.setenv("SANDWEAVE_HOME", explicit_home)
    monkeypatch.setenv("GYM_ANYTHING_CACHE_DIR", str(tmp_path / "machine-cache"))
    env = Mock(runner=object())
    monkeypatch.setattr("gym_anything.config.loading.from_config", Mock(return_value=env))
    assert boot_env({"env_dir": str(tmp_path), "task_id": "add_license", "runner": "qemu"}) is env
    assert os.environ.get("SANDWEAVE_HOME") == explicit_home
    env.reset.assert_called_once_with(seed=0, use_cache=True, cache_level="post_start", use_savevm=False)
