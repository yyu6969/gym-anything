"""Shared host-cache layout for the bundled VM-family runners.

Party-side code (not core orchestration): runner classes compose their
`cache_components()` rows from these helpers, and the cache CLI only ever
iterates runner classes. Components are keyed by name and deduplicated by
the CLI, so families that share a store (qemu / qemu_native / avf) can all
declare it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from ...runtime_paths import reusable_cache_paths

_QEMU_WORK_NAMES = {"work", "last_provision.log"}


def _is_qemu_work(path: Path) -> bool:
    return path.name in _QEMU_WORK_NAMES or path.suffix == ".lock"


def _qemu_work_paths() -> List[Path]:
    qemu = reusable_cache_paths().qemu
    if not qemu.exists():
        return []
    paths: List[Path] = []
    for entry in qemu.iterdir():
        if _is_qemu_work(entry):
            paths.append(entry)
            continue
        if entry.name == "avf":
            for sub in entry.iterdir():
                if _is_qemu_work(sub):
                    paths.append(sub)
    return paths


def _qemu_base_paths() -> List[Path]:
    qemu = reusable_cache_paths().qemu
    if not qemu.exists():
        return []
    paths: List[Path] = []
    for entry in qemu.iterdir():
        if _is_qemu_work(entry):
            continue
        if entry.name == "avf":
            paths.extend(sub for sub in entry.iterdir() if not _is_qemu_work(sub))
            continue
        paths.append(entry)
    return paths


def qemu_components() -> List[Dict]:
    return [
        {"name": "qemu-work", "category": "work", "paths": _qemu_work_paths(),
         "desc": "QEMU/AVF work directories and COW overlays (per-run state)"},
        {"name": "qemu-base", "category": "base", "paths": _qemu_base_paths(),
         "desc": "QEMU/AVF base images, checkpoints, and reusable downloads"},
    ]


def apptainer_components() -> List[Dict]:
    caches = reusable_cache_paths()
    return [
        {"name": "apptainer", "category": "base", "paths": [caches.apptainer],
         "desc": "Reusable Apptainer SIF images"},
    ]


def avd_components() -> List[Dict]:
    caches = reusable_cache_paths()
    return [
        {"name": "avd-checkpoints", "category": "base", "paths": [caches.avd_checkpoints],
         "desc": "AVD checkpoint snapshots"},
        {"name": "android-sdk", "category": "base", "paths": [caches.root / "android-sdk"],
         "desc": "Android SDK (requires network to re-download)"},
        {"name": "apks", "category": "base", "paths": [caches.root / "apks"],
         "desc": "Downloaded Android APKs"},
        {"name": "avd", "category": "base", "paths": [caches.root / "avd"],
         "desc": "Android Virtual Device definitions"},
    ]


def container_components() -> List[Dict]:
    caches = reusable_cache_paths()
    return [
        {"name": "containers", "category": "base", "paths": [caches.containers],
         "desc": "Reusable container runtime images"},
    ]
