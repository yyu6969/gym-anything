"""Portable, content-addressed assets for the Harbor compiler."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def directory_digest(root: Path) -> str:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            entries.append({"path": path.relative_to(root).as_posix(),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "executable": bool(path.stat().st_mode & 0o111)})
    return hashlib.sha256(json_bytes(entries)).hexdigest()


_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".env", ".env.*",
                                 "artifacts", ".pytest_cache", ".cache")


def copy_assets(source: Path, destination: Path) -> None:
    # Materialize internal symlinks, but never include checkout-external data.
    for path in source.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"Asset symlink escapes its source: {path}")
    shutil.copytree(source, destination, ignore=_IGNORE)


def snapshot_environment(source: Path, task_id: str, destination: Path) -> str:
    destination.mkdir()
    for path in sorted(source.iterdir()):
        if _IGNORE(str(source), [path.name]) or path.name == "tasks":
            continue
        if path.is_symlink() and not path.resolve().is_relative_to(source):
            raise ValueError(f"Environment symlink escapes source: {path}")
        target = destination / path.name
        if path.is_dir():
            copy_assets(path, target)
        else:
            shutil.copy2(path, target)
    copy_assets(source / "tasks" / task_id, destination / "tasks" / task_id)
    # Host mount sources must resolve inside the artifact, never against cwd
    # or the original checkout. Normalize only the deployment copy.
    env_path = destination / "env.json"
    spec = json.loads(env_path.read_text())
    dockerfile = spec.get("dockerfile")
    if dockerfile:
        path = Path(dockerfile)
        if path.is_absolute():
            if not path.resolve().is_relative_to(source):
                raise ValueError(f"Snapshot Dockerfile is outside environment: {path}")
            spec["dockerfile"] = "./" + path.resolve().relative_to(source).as_posix()
    for mount in spec.get("mounts", []):
        mount_source = Path(mount["source"])
        resolved = (source / mount_source).resolve()
        if not resolved.is_relative_to(source):
            raise ValueError(f"Snapshot mount source is outside environment: {mount_source}")
        relative = resolved.relative_to(source)
        if not (destination / relative).exists():
            raise ValueError(f"Snapshot mount source is missing: {relative}")
        mount["source"] = "./" + relative.as_posix()
    recording = spec.get("recording")
    if isinstance(recording, dict) and "output_dir" in recording:
        recording["output_dir"] = "./artifacts"
    env_path.write_bytes(json_bytes(spec))
    return directory_digest(destination)


def snapshot_runtime(destination: Path) -> str:
    # Ship the compiler's runtime as well: local AAB improvements may not yet
    # exist at any public revision. This is the same Gym source, not a fork.
    package = Path(__file__).resolve().parents[2]
    project = package.parents[1]
    destination.mkdir()
    copy_assets(package, destination / "src" / "gym_anything")
    if (project / "agents").is_dir():
        copy_assets(project / "agents", destination / "agents")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(project / name, destination / name)
    return directory_digest(destination)


def resolve_asset(config: dict, key: str, asset_root: Path) -> Path:
    relative = Path(config[key])
    resolved = (asset_root / relative).resolve()
    if relative.is_absolute() or not resolved.is_relative_to(asset_root.resolve()):
        raise ValueError(f"Packaged {key} must be relative to the task environment")
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    return resolved
