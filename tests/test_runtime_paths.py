from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents.shared.output_paths import agent_run_base
from gym_anything.runtime_paths import (
    CACHE_DIR_ENV,
    configure_runtime_paths,
    reusable_cache_paths,
    resolve_output_root,
    resolve_runtime_file,
)


_RUNTIME_DIR_NAMES = {
    "all_runs",
    "artifacts",
    "audits",
    "cache",
    "log_dumps_claude",
    "logs",
    "model_usage_dumps",
    "outputs",
    "runs",
    "sandweave",
    "state",
    "work",
    ".sandweave",
}

_CONFIGURED_ENVIRONMENT = {
    "APPTAINER_CACHEDIR",
    "GYM_ANYTHING_AGENT_SANDBOX_CACHE",
    "GYM_ANYTHING_APPTAINER_CACHE",
    "GYM_ANYTHING_AVD_CHECKPOINT_CACHE",
    "GYM_ANYTHING_AVD_CONTAINER_CACHE",
    "GYM_ANYTHING_CACHE_DIR",
    "GYM_ANYTHING_FAST_IO_WORK_DIR",
    "GYM_ANYTHING_OUTPUT_DIR",
    "GYM_ANYTHING_QEMU_CACHE",
    "GYM_ANYTHING_QEMU_WORK_DIR",
    "GYM_ANYTHING_REMOTE_CACHE",
    "GYM_ANYTHING_STATE_DIR",
    "SANDWEAVE_HOME",
    "SANDWEAVE_LOCAL_DIR",
}


@pytest.fixture(autouse=True)
def _restore_path_environment():
    before = {name: os.environ.get(name) for name in _CONFIGURED_ENVIRONMENT}
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _clear_path_environment() -> None:
    for name in _CONFIGURED_ENVIRONMENT:
        os.environ.pop(name, None)


def test_explicit_output_root_is_independent_of_current_working_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_path_environment()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    output = (tmp_path / "canonical-output").resolve()
    cwd_a = tmp_path / "cwd-a"
    cwd_b = tmp_path / "cwd-b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    observed = []
    for cwd in (cwd_a, cwd_b):
        monkeypatch.chdir(cwd)
        paths = configure_runtime_paths(output)
        observed.append(paths)
        assert Path(agent_run_base("experiment", "model", "task")) == (
            output / "all_runs" / "experiment" / "model" / "task"
        )

    assert observed[0] == observed[1]
    assert observed[0].artifacts == output / "artifacts"
    expected_cache = (home / ".cache/gym-anything").resolve()
    assert os.environ["SANDWEAVE_HOME"] == str(expected_cache / "sandweave")
    assert os.environ["SANDWEAVE_LOCAL_DIR"] == str(
        output / "work" / "sandweave-local"
    )
    assert os.environ["GYM_ANYTHING_QEMU_CACHE"] == str(expected_cache / "qemu")
    assert "APPTAINER_CACHEDIR" not in os.environ
    assert "GYM_ANYTHING_QEMU_WORK_DIR" not in os.environ
    assert "GYM_ANYTHING_FAST_IO_WORK_DIR" not in os.environ
    assert not output.exists(), "path resolution must not eagerly create output directories"
    for cwd in (cwd_a, cwd_b):
        assert not any((cwd / name).exists() for name in _RUNTIME_DIR_NAMES)


def test_output_root_precedence(tmp_path: Path, monkeypatch) -> None:
    env_output = tmp_path / "from-env"
    explicit_output = tmp_path / "explicit"
    monkeypatch.setenv("GYM_ANYTHING_OUTPUT_DIR", str(env_output))

    assert resolve_output_root() == env_output.resolve()
    assert resolve_output_root(explicit_output) == explicit_output.resolve()


def test_explicit_qemu_cache_takes_precedence_over_cache_root_and_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_path_environment()
    cache_root = tmp_path / "generic-cache"
    qemu_cache = tmp_path / "configured-qemu-cache"
    qemu_work = tmp_path / "configured-qemu-work"
    fast_io_work = tmp_path / "configured-fast-io-work"
    apptainer_runtime_cache = tmp_path / "apptainer-runtime-cache"
    monkeypatch.setenv(CACHE_DIR_ENV, str(cache_root))
    monkeypatch.setenv("GYM_ANYTHING_QEMU_CACHE", str(qemu_cache))
    monkeypatch.setenv("GYM_ANYTHING_QEMU_WORK_DIR", str(qemu_work))
    monkeypatch.setenv("GYM_ANYTHING_FAST_IO_WORK_DIR", str(fast_io_work))
    monkeypatch.setenv("APPTAINER_CACHEDIR", str(apptainer_runtime_cache))

    configure_runtime_paths(tmp_path / "run-output")
    caches = reusable_cache_paths()

    assert caches.root == cache_root.resolve()
    assert caches.qemu == qemu_cache.resolve()
    assert caches.apptainer == (cache_root / "apptainer").resolve()
    assert os.environ["GYM_ANYTHING_QEMU_CACHE"] == str(qemu_cache)
    assert os.environ["GYM_ANYTHING_QEMU_WORK_DIR"] == str(qemu_work)
    assert os.environ["GYM_ANYTHING_FAST_IO_WORK_DIR"] == str(fast_io_work)
    assert os.environ["APPTAINER_CACHEDIR"] == str(apptainer_runtime_cache)


@pytest.mark.parametrize(
    ("variable", "attribute"),
    [
        ("GYM_ANYTHING_QEMU_CACHE", "qemu"),
        ("GYM_ANYTHING_APPTAINER_CACHE", "apptainer"),
        ("GYM_ANYTHING_AVD_CHECKPOINT_CACHE", "avd_checkpoints"),
        ("GYM_ANYTHING_AVD_CONTAINER_CACHE", "containers"),
        ("GYM_ANYTHING_AGENT_SANDBOX_CACHE", "agent_sandbox"),
        ("GYM_ANYTHING_REMOTE_CACHE", "remote"),
        ("SANDWEAVE_HOME", "sandweave"),
    ],
)
def test_component_specific_cache_variable_has_highest_precedence(
    variable: str,
    attribute: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_path_environment()
    component_cache = tmp_path / f"configured-{attribute}"
    monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path / "generic-cache"))
    monkeypatch.setenv(variable, str(component_cache))

    paths = reusable_cache_paths()

    assert getattr(paths, attribute) == component_cache.resolve()


def test_two_output_roots_reuse_the_same_base_qcow2(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_path_environment()
    cache_root = tmp_path / "shared-machine-cache"
    base_image = cache_root / "qemu/base_ubuntu_gnome.qcow2"
    base_image.parent.mkdir(parents=True)
    base_image.write_bytes(b"existing reusable qcow2")
    monkeypatch.setenv(CACHE_DIR_ENV, str(cache_root))

    observed: list[Path] = []
    for output in (tmp_path / "run-a", tmp_path / "run-b"):
        paths = configure_runtime_paths(output)
        observed.append(Path(os.environ["GYM_ANYTHING_QEMU_CACHE"]) / base_image.name)
        assert paths.artifacts == output.resolve() / "artifacts"
        assert os.environ["GYM_ANYTHING_STATE_DIR"] == str(
            output.resolve() / "state"
        )

    assert observed == [base_image.resolve(), base_image.resolve()]
    assert observed[0].read_bytes() == b"existing reusable qcow2"
    assert not (tmp_path / "run-a/cache").exists()
    assert not (tmp_path / "run-b/cache").exists()


def test_cache_location_is_cwd_independent(tmp_path: Path, monkeypatch) -> None:
    _clear_path_environment()
    cache_root = tmp_path / "shared-cache"
    monkeypatch.setenv(CACHE_DIR_ENV, str(cache_root))
    cwd_a = tmp_path / "cwd-a"
    cwd_b = tmp_path / "cwd-b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    observed = []
    for cwd, output_name in ((cwd_a, "one"), (cwd_b, "two")):
        monkeypatch.chdir(cwd)
        configure_runtime_paths(tmp_path / output_name)
        observed.append(reusable_cache_paths().qemu)

    assert observed == [cache_root.resolve() / "qemu"] * 2
    assert not (cwd_a / "cache").exists()
    assert not (cwd_b / "cache").exists()


def test_cache_cli_only_classifies_ephemeral_qemu_paths_as_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from gym_anything.runtime.runners.host_cache import (
        _qemu_base_paths,
        _qemu_work_paths,
        apptainer_components,
        avd_components,
        container_components,
    )

    _clear_path_environment()
    cache_root = tmp_path / "shared-cache"
    monkeypatch.setenv(CACHE_DIR_ENV, str(cache_root))
    qemu = cache_root / "qemu"
    work = qemu / "work"
    avf_work = qemu / "avf/work"
    work.mkdir(parents=True)
    avf_work.mkdir(parents=True)
    persistent = {
        qemu / "base_ubuntu_gnome.qcow2",
        qemu / "checkpoint_example_post_start.qcow2",
        qemu / "ubuntu-cloud.img",
        qemu / "avf/base_ubuntu_gnome_arm64.raw",
    }
    disposable = {
        work,
        avf_work,
        qemu / "checkpoint_example.lock",
        qemu / "last_provision.log",
    }
    for path in persistent | (disposable - {work, avf_work}):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    assert set(_qemu_base_paths()) == persistent
    assert set(_qemu_work_paths()) == disposable
    assert apptainer_components()[0]["category"] == "base"
    assert avd_components()[0]["category"] == "base"
    assert container_components()[0]["category"] == "base"


def test_qemu_runner_reuses_explicit_base_image_across_processes_and_cwds(
    tmp_path: Path,
) -> None:
    qemu_cache = tmp_path / "shared-qemu"
    base_image = qemu_cache / "base_ubuntu_gnome.qcow2"
    base_image.parent.mkdir(parents=True)
    base_image.write_bytes(b"existing base image marker")
    cwd_a = tmp_path / "cwd-a"
    cwd_b = tmp_path / "cwd-b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    probe = (
        "import json, os; "
        "from gym_anything.runtime_paths import configure_runtime_paths; "
        "configure_runtime_paths(os.environ['PROBE_OUTPUT']); "
        "from gym_anything.runtime.runners.qemu_apptainer import "
        "QEMU_CACHE, QEMU_WORK_DIR; "
        "print(json.dumps({'cache': str(QEMU_CACHE), "
        "'base': str(QEMU_CACHE / 'base_ubuntu_gnome.qcow2'), "
        "'work': str(QEMU_WORK_DIR), "
        "'artifacts': os.environ['GYM_ANYTHING_OUTPUT_DIR'] + '/artifacts'}))"
    )
    observed = []
    for cwd, output in ((cwd_a, tmp_path / "run-a"), (cwd_b, tmp_path / "run-b")):
        environment = {
            **os.environ,
            "GYM_ANYTHING_QEMU_CACHE": str(qemu_cache),
            "PROBE_OUTPUT": str(output),
        }
        environment.pop("GYM_ANYTHING_QEMU_WORK_DIR", None)
        environment.pop("GYM_ANYTHING_FAST_IO_WORK_DIR", None)
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        observed.append(json.loads(completed.stdout))

    assert [entry["cache"] for entry in observed] == [str(qemu_cache.resolve())] * 2
    assert [entry["base"] for entry in observed] == [str(base_image.resolve())] * 2
    assert [entry["work"] for entry in observed] == [
        str(qemu_cache.resolve() / "work")
    ] * 2
    assert base_image.read_bytes() == b"existing base image marker"
    assert observed[0]["artifacts"] == str(tmp_path / "run-a/artifacts")
    assert observed[1]["artifacts"] == str(tmp_path / "run-b/artifacts")
    assert not (cwd_a / "artifacts").exists()
    assert not (cwd_b / "artifacts").exists()


def test_path_configuration_does_not_touch_repository_runtime_directories(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_path_environment()
    gym_root = Path(__file__).resolve().parents[1]
    aab_root = gym_root.parents[1]

    def manifest(root: Path) -> dict[str, tuple[bool, int | None]]:
        return {
            name: (
                (root / name).exists(),
                (root / name).stat().st_mtime_ns if (root / name).exists() else None,
            )
            for name in _RUNTIME_DIR_NAMES
        }

    before = {root: manifest(root) for root in (aab_root, gym_root)}
    for root in (aab_root, gym_root):
        monkeypatch.chdir(root)
        paths = configure_runtime_paths(tmp_path / "runtime-output")
        assert paths.artifacts == tmp_path.resolve() / "runtime-output/artifacts"

    assert {root: manifest(root) for root in (aab_root, gym_root)} == before
    assert not (tmp_path / "runtime-output").exists()


def test_named_runtime_file_cannot_escape_output_root(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "output"
    configure_runtime_paths(output)

    assert resolve_runtime_file("logs/timing.jsonl") == (
        output / "logs" / "timing.jsonl"
    ).resolve()
    try:
        resolve_runtime_file(tmp_path / "outside.jsonl")
    except ValueError as exc:
        assert "must stay under --output-dir" in str(exc)
    else:
        raise AssertionError("an outside timing path was accepted")
