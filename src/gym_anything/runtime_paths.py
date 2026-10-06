"""Canonical runtime-output and reusable-cache paths for Gym-Anything.

Benchmark definitions are repository resources and are deliberately not part
of either layout. Runtime output follows ``--output-dir``; heavyweight caches
remain machine-level and are shared across runs.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


OUTPUT_DIR_ENV = "GYM_ANYTHING_OUTPUT_DIR"
CACHE_DIR_ENV = "GYM_ANYTHING_CACHE_DIR"


def _default_output_root() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return (base / "gym-anything").resolve()


def resolve_output_root(output_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve CLI value, then environment value, then the stable user fallback."""
    configured = output_dir if output_dir is not None else os.environ.get(OUTPUT_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return _default_output_root()


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    artifacts: Path
    all_runs: Path
    model_usage_dumps: Path
    model_response_dumps: Path
    work: Path
    state: Path


def runtime_paths(output_dir: str | os.PathLike[str] | None = None) -> RuntimePaths:
    root = resolve_output_root(output_dir)
    return RuntimePaths(
        root=root,
        artifacts=root / "artifacts",
        all_runs=root / "all_runs",
        model_usage_dumps=root / "model_usage_dumps",
        model_response_dumps=root / "log_dumps_claude",
        work=root / "work",
        state=root / "state",
    )


@dataclass(frozen=True)
class ReusableCachePaths:
    """Machine-level caches that must not vary with the runtime output root."""

    root: Path
    qemu: Path
    apptainer: Path
    avd_checkpoints: Path
    containers: Path
    agent_sandbox: Path
    remote: Path
    sandweave: Path


def _resolved_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def resolve_cache_root(
    cache_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve explicit cache root, then its env var, then the historic default."""

    configured = cache_dir if cache_dir is not None else os.environ.get(CACHE_DIR_ENV)
    if configured:
        return _resolved_path(configured)
    return (Path.home() / ".cache" / "gym-anything").resolve()


def _runner_cache_path(variable: str, fallback: Path) -> Path:
    configured = os.environ.get(variable)
    return _resolved_path(configured) if configured else fallback.resolve()


def reusable_cache_paths(
    cache_dir: str | os.PathLike[str] | None = None,
) -> ReusableCachePaths:
    """Resolve reusable caches independently from ``--output-dir``.

    Runner-specific variables have highest precedence, followed by
    ``GYM_ANYTHING_CACHE_DIR`` and stable machine-cache defaults. Existing Gym
    caches retain their historical locations; remote downloads retain their
    historical ``~/.gym_anything_cache`` fallback when no generic cache root
    has been configured.
    """

    root_was_configured = cache_dir is not None or bool(os.environ.get(CACHE_DIR_ENV))
    root = resolve_cache_root(cache_dir)
    remote_fallback = (
        root / "remote"
        if root_was_configured
        else Path.home() / ".gym_anything_cache"
    )
    return ReusableCachePaths(
        root=root,
        qemu=_runner_cache_path("GYM_ANYTHING_QEMU_CACHE", root / "qemu"),
        apptainer=_runner_cache_path(
            "GYM_ANYTHING_APPTAINER_CACHE",
            root / "apptainer",
        ),
        avd_checkpoints=_runner_cache_path(
            "GYM_ANYTHING_AVD_CHECKPOINT_CACHE",
            root / "avd-checkpoints",
        ),
        containers=_runner_cache_path(
            "GYM_ANYTHING_AVD_CONTAINER_CACHE",
            root / "containers",
        ),
        agent_sandbox=_runner_cache_path(
            "GYM_ANYTHING_AGENT_SANDBOX_CACHE",
            root / "agent-sandbox",
        ),
        remote=_runner_cache_path("GYM_ANYTHING_REMOTE_CACHE", remote_fallback),
        sandweave=_runner_cache_path("SANDWEAVE_HOME", root / "sandweave"),
    )


def resolve_runtime_file(path: str | os.PathLike[str]) -> Path:
    """Resolve a user-named runtime file without allowing it outside the root."""
    root = resolve_output_root()
    requested = Path(path).expanduser()
    resolved = (requested if requested.is_absolute() else root / requested).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"runtime output path must stay under --output-dir ({root}): {resolved}"
        ) from exc
    return resolved


def configure_runtime_paths(
    output_dir: str | os.PathLike[str] | None = None,
) -> RuntimePaths:
    """Configure runtime output without relocating reusable machine caches.

    This function does not create directories.  Individual components retain
    ownership of directory creation when and only when they are used.
    """
    paths = runtime_paths(output_dir)
    caches = reusable_cache_paths()
    runtime_values = {
        OUTPUT_DIR_ENV: paths.root,
        "GYM_ANYTHING_STATE_DIR": paths.state,
        "SANDWEAVE_LOCAL_DIR": paths.work / "sandweave-local",
    }
    for name, value in runtime_values.items():
        os.environ[name] = str(value)

    # These defaults deliberately use setdefault: an existing cache variable
    # always wins, while the derived defaults remain independent of output_dir.
    cache_values = {
        CACHE_DIR_ENV: caches.root,
        "GYM_ANYTHING_QEMU_CACHE": caches.qemu,
        "GYM_ANYTHING_APPTAINER_CACHE": caches.apptainer,
        "GYM_ANYTHING_AVD_CHECKPOINT_CACHE": caches.avd_checkpoints,
        "GYM_ANYTHING_AVD_CONTAINER_CACHE": caches.containers,
        "GYM_ANYTHING_AGENT_SANDBOX_CACHE": caches.agent_sandbox,
        "GYM_ANYTHING_REMOTE_CACHE": caches.remote,
        "SANDWEAVE_HOME": caches.sandweave,
    }
    for name, value in cache_values.items():
        os.environ.setdefault(name, str(value))

    # Task verifiers are imported dynamically.  Avoid writing __pycache__
    # into benchmark-definition directories in source checkouts.
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    return paths
