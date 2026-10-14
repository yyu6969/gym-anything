"""Compile gym-anything benchmark tasks into Harbor task directories.

A compiled task has the standard Harbor shape::

    <out>/<env_name>__<task_id>/
    ├── instruction.md                    # task.json description
    ├── task.toml                         # Harbor TaskConfig (schema 1.3)
    ├── environment/
    │   ├── Dockerfile                    # generic runtime (QEMU-in-container)
    │   ├── docker-compose.yaml           # KVM passthrough + image cache volume
    │   └── gym-anything.json             # task identity / boot config
    └── tests/test.sh                     # grades via the in-container runtime

The tasks run two ways from the same directory:

* **Standard Harbor path** (any docker-capable backend with KVM): the
  Dockerfile boots the task's guest via QEMU inside the container (the
  ModalRunner sandbox shape, see ``container``), and ``tests/test.sh``
  runs the task's real grading pipeline in the container. Requires the shared
  image-cache volume once per host: ``docker volume create
  harbor-gym-anything-cache`` (the OSWorld adapter uses the same pattern).
* **gym-anything backend fast path** (``--env
  gym_anything.integrations.harbor:GymAnythingEnvironment``): the backend
  boots the guest directly through the local runner stack, ignores the
  Dockerfile, and intercepts the verifier invocation to grade host-side.

This module deliberately imports neither ``harbor`` nor anything heavier than
the gym-anything registry, so compilation (and its tests) run everywhere.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...registry import get_tasks_for_environment, resolve_environment_dir
from .packaging import copy_assets, directory_digest, json_bytes, snapshot_environment, snapshot_runtime

_UNSET: Dict[str, Any] = {"__unset__": True}

_TEST_SH = """#!/bin/bash
# Grades the episode via the in-container gym-anything runtime: runs the
# task's real pipeline (post_task export hook + verifier.py) against the
# guest VM and writes /logs/verifier/reward.json for Harbor's Verifier.
# On the gym-anything custom backend this script is not executed: the
# backend intercepts the invocation and runs the same pipeline host-side.
exec python -m gym_anything.integrations.harbor.container finalize \\
  --reward-path /logs/verifier/reward.json \\
  --verifier-path /logs/verifier/verifier.json
"""

_SOLVE_SH = """#!/usr/bin/env bash
set -euo pipefail

# cua-world tasks are interactive GUI tasks graded by per-task verifiers
# against live application state; no scripted oracle solution is shipped.
# This follows the precedent of the OSWorld and TheAgentCompany adapters.
echo "cua-world does not ship oracle solve scripts." >&2
exit 1
"""

_DOCKERFILE = """# Generic runtime for gym-anything Harbor tasks: QEMU inside the container
# boots the task's guest VM (the same shape gym-anything's ModalRunner
# sandbox uses). This file is identical across tasks; the task identity
# lives in gym-anything.json.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install --no-install-recommends -y \\
        git \\
        qemu-system-x86 \\
        qemu-utils \\
        wget \\
        curl \\
        ca-certificates \\
        genisoimage \\
        openssh-client \\
        procps \\
        unzip \\
        zstd \\
        tini \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ARG GYM_ANYTHING_REF={ref}
RUN pip install --no-cache-dir \\
    "gym-anything{extras} @ git+https://github.com/cmu-l3/gym-anything@${{GYM_ANYTHING_REF}}" \\
 && pip install --no-cache-dir --no-deps \\
    "cua-world @ git+https://github.com/cmu-l3/gym-anything@${{GYM_ANYTHING_REF}}#subdirectory=packaging/cua-world"

COPY gym-anything.json /harbor-task/gym-anything.json

ENV GYM_ANYTHING_QEMU_CACHE=/gym-anything-cache/qemu \\
    GA_HARBOR_PORT=7317 \\
    GA_HARBOR_RUNNER=qemu

ENTRYPOINT ["tini", "-s", "--", "python", "-m", "gym_anything.integrations.harbor.container", "serve"]
"""

_DOCKER_COMPOSE = """# Mirrors the OSWorld adapter's runtime shape: KVM passthrough plus a shared
# cache volume so the guest image is provisioned once per host and reused
# across trials. Create the volume once:
#   docker volume create harbor-gym-anything-cache
services:
  main:
    command: []
    devices:
      - /dev/kvm
    environment:
      GA_HARBOR_PORT: "7317"
      GA_HARBOR_RUNNER: qemu
      GYM_ANYTHING_QEMU_CACHE: /gym-anything-cache/qemu
    volumes:
      - gym-anything-cache:/gym-anything-cache
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:7317/health >/dev/null"]
      interval: 10s
      timeout: 5s
      retries: 720
      start_period: 2400s
    stop_grace_period: 2m

volumes:
  gym-anything-cache:
    external: true
    name: harbor-gym-anything-cache
"""

_SANDWEAVE_VERSION = "0.2.41"
_SANDWEAVE_DOCKERFILE = """# Sandweave worker inside the existing Gym Harbor controller; no KVM.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install --no-install-recommends -y \\
        git curl ca-certificates procps tini unzip zstd squashfs-tools rpm2cpio cpio \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ARG GYM_ANYTHING_REF={ref}
RUN pip install --no-cache-dir \\
    "gym-anything{extras} @ git+https://github.com/cmu-l3/gym-anything@${{GYM_ANYTHING_REF}}" \\
    "sandweave[desktop]==0.2.41" "litellm==1.103.0" \\
    "dissect.extfs==3.15" "dissect.hypervisor==3.21" "dissect.volume==3.18"

COPY gym-anything.json /harbor-task/gym-anything.json

# Sandweave 0.2.41 verifies its pinned Apptainer 1.5.3 installer checksum.
# Tools live in the image; runtime assets and checkpoints live in the cache.
RUN SANDWEAVE_HOME=/opt/sandweave-tools python -c \\
    "from sandweave.onboarding import install_apptainer; install_apptainer()"
ENV PATH=/opt/sandweave-tools/bin:$PATH \\
    SANDWEAVE_HOME=/sandweave-cache \\
    SANDWEAVE_LOCAL_DIR=/sandweave-scratch \\
    APPTAINER_TMPDIR=/sandweave-scratch/apptainer \\
    APPTAINER_CACHEDIR=/sandweave-cache/apptainer \\
    APPTAINER_UNSQUASH=1 \\
    GYM_ANYTHING_OUTPUT_DIR=/logs/verifier/gym/runtime \\
    LITELLM_LOCAL_MODEL_COST_MAP=True \\
    GA_HARBOR_PORT=7317 \\
    GA_HARBOR_RUNNER=sandweave \\
    GYM_ANYTHING_RUNNER=sandweave
RUN mkdir -p /sandweave-cache /sandweave-scratch/apptainer /logs/verifier/gym

ENTRYPOINT ["tini", "-s", "--", "python", "-m", "gym_anything.integrations.harbor.container", "serve"]
"""

_SANDWEAVE_COMPOSE = """services:
  main:
    platform: linux/amd64
    command: []
    # Apptainer needs mount/namespace authority INSIDE this container.
    # Keep the engine's default seccomp policy; no privileged mode, host
    # namespaces, KVM, FUSE or loop devices. Startup probes this permission path.
    cap_add: [SYS_ADMIN]
    environment:
      GA_HARBOR_PORT: "7317"
      GA_HARBOR_RUNNER: sandweave
      GYM_ANYTHING_RUNNER: sandweave
      SANDWEAVE_HOME: /sandweave-cache
      SANDWEAVE_LOCAL_DIR: /sandweave-scratch
      APPTAINER_UNSQUASH: "1"
    volumes:
      - sandweave-cache:/sandweave-cache
      - sandweave-scratch:/sandweave-scratch
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:7317/health >/dev/null"]
      interval: 10s
      timeout: 5s
      retries: 720
      start_period: 2400s
    stop_grace_period: 2m
volumes:
  sandweave-cache:
    external: true
    name: harbor-gym-anything-sandweave-cache
  sandweave-scratch: {}
"""

_SANDWEAVE_README = """# Gym Harbor task with Sandweave

This package contains the selected environment, task, authoritative verifier
bundle, Gym runtime and reference-agent sources. It needs no AAB registry or
host-installed Gym. The host agent is the standalone module in `agent/`;
Gemini3Agent runs inside the existing Gym controller. Its native single-call
parser, conversation history and action feedback are retained.

## Prerequisites

- Harbor 0.24.0 and a working Docker/Compose or Podman/Compose installation.
- A Linux x86-64 worker kernel >=5.4. No `/dev/kvm` is required.
- The resources declared in `task.toml`, plus disk for image builds and caches.
- Network access for pinned Ubuntu/runtime assets, application images and APIs.
- `GEMINI_API_KEY` for both agent and external VLM grading, supplied at run time.

The image installs Sandweave 0.2.41 and its pinned Apptainer tools. The Compose
profile adds `SYS_ADMIN` for container-local Apptainer mount/namespace operations.
It keeps default seccomp and does not request privileged mode, host namespaces,
KVM, FUSE or loop devices. This is a deployment configuration to validate on
your engine, not a guarantee that every rootless or managed host permits it.
Startup probes actual namespace/seccomp support before guest preparation.
Do not work around failed probes by changing host security policy.

Cache and scratch use separate volumes. Rootless engines must support the
UID/GID mappings needed to build Debian images and run the worker. Native host
Sandweave success does not establish container compatibility.

## Run

From this task directory, after checking engine prerequisites:

```bash
docker volume create harbor-gym-anything-sandweave-cache
./run-harbor.sh --env docker -m gemini-3.8-flash \\
  --agent-kwarg max_steps=50 --env-file /path/to/benchmark.env \\
  --jobs-dir /path/to/results --job-name sandweave-smoke --yes
```

For Podman, create the same named volume with `podman volume create` and pass
`--env podman`. The selected Harbor provider runs the outer controller; the
Gym runner inside it remains Sandweave. Sandweave's experimental Harbor provider
is a different outer backend and is not this deployment path. Do not use
`computer-1`, which provisions a separate browser/desktop.

## Evidence and grading

Harbor trial `agent/` retains screenshots, ATIF, `messages.pkl`,
`raw_responses.pkl`, `timing.jsonl` and `usage.jsonl`. The verifier directory
retains `gym/episode_*/` with trajectories, hook logs, final screenshots and
summary, plus bootstrap/worker diagnostics and `reward.json`/`verifier.json`.
Gym evidence is under verifier logs because finalization happens after early
agent-artifact collection. Interrupted runs retain completed model responses;
unreturned in-flight API responses cannot be recovered. Failures remain errors.

The external AAB checklist, when bundled, is authoritative. Its required binary
outcomes must all PASS for reward 1. Native compatibility stubs and model
success termination do not award credit. Guest Docker/browser configuration,
task hooks and seed state come from the packaged recipe without reconstruction.

Cold trials provision the recipe; native host checkpoints are not included.
The current runner's checkpoint key includes resolved mount paths, so random
trial directories can prevent reuse of an application checkpoint. The base
image/runtime cache still persists. Cache optimization is separate work.
"""

_DEFAULT_KEYWORDS = ["computer-use", "gui", "gym-anything"]

# Default grading: the VLM checklist (a VLM judges the trajectory against
# each task's vlm_checklist.json). The programmatic verifiers read live,
# agent-mutable application state for some tasks, so the prime hub shell
# made the same choice for the same reason. Set "mode" to None to restore
# each task's declared (usually programmatic) mode.
_DEFAULT_VERIFIER = {
    "mode": "vlm_checklist",
    "vlm_backend": "local",
    "vlm_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "vlm_model": "gemini-3.5-flash",
    "vlm_api_key_var": "GEMINI_API_KEY",
}


def _toml_string(value: str) -> str:
    # JSON string escaping is a valid TOML basic string for the fields we emit.
    return json.dumps(value)


def _render_task_toml(
    *,
    name: str,
    description: str,
    keywords: List[str],
    build_timeout_sec: int,
    agent_timeout_sec: int,
    verifier_timeout_sec: int,
    metadata: Dict[str, Any],
    resources: Optional[Dict[str, int]] = None,
) -> str:
    keyword_list = ", ".join(_toml_string(k) for k in keywords)
    metadata_lines = "\n".join(
        f"{key} = {_toml_string(str(value))}" for key, value in metadata.items()
    )
    resource_lines = "\n" + "\n".join(f"{key} = {value}" for key, value in resources.items()) if resources else ""
    return f"""schema_version = "1.3"

[task]
name = {_toml_string(name)}
description = {_toml_string(description)}
keywords = [{keyword_list}]

[environment]
build_timeout_sec = {build_timeout_sec}{resource_lines}

[agent]
timeout_sec = {agent_timeout_sec}

[verifier]
timeout_sec = {verifier_timeout_sec}

[verifier.env]
GEMINI_API_KEY = "${{GEMINI_API_KEY:-}}"

[metadata.gym_anything]
{metadata_lines}
"""


def compile_task(
    env_name: str,
    task_id: str,
    out_root: Path | str,
    *,
    benchmark: str = "cua_world",
    org: str = "cua-world",
    env_dir: Optional[Path | str] = None,
    cache_level: str = "post_start",
    use_cache: bool = True,
    runner: Optional[str] = None,
    seed: int = 0,
    build_timeout_sec: int = 3600,
    agent_timeout_sec: int = 1800,
    verifier_timeout_sec: int = 1800,
    gym_anything_ref: str = "main",
    pip_extras: str = "",
    verifier: Optional[Dict[str, Any]] = _UNSET,  # type: ignore[assignment]
    instruction: Optional[str] = None,
    verifier_bundle: Optional[Path | str] = None,
    packaging_mode: Optional[str] = None,
) -> Path:
    """Compile one benchmark task into a Harbor task directory.

    ``build_timeout_sec`` defaults high because the first boot of an
    environment provisions it; later boots load from the cache volume.
    ``gym_anything_ref`` pins the gym-anything git ref installed into the
    task image, with the CUA-World corpus (the cua-world package) from the
    same ref; ``pip_extras`` adds extras (e.g. ``"benchmark"`` for the full
    verifier dependency corpus — core deps already cover PIL/numpy).

    ``instruction`` overrides both Harbor instruction fields and the runtime
    task description. ``verifier_bundle`` is a directory containing an
    authoritative ``checklist.json`` plus arbitrary provenance assets.
    Explicit ``registry_ref`` requires a full immutable commit SHA; omitting
    ``packaging_mode`` preserves the legacy thin artifact and ref defaults.
    ``vendored_snapshot`` packages the selected environment/task and this
    Gym runtime, so freshly created local tasks need no source registry.
    """
    mode = packaging_mode or "registry_ref"
    if mode not in {"registry_ref", "vendored_snapshot"}:
        raise ValueError(f"Unknown packaging mode: {mode}")
    if packaging_mode == "registry_ref" and not re.fullmatch(r"[0-9a-fA-F]{40}", gym_anything_ref):
        raise ValueError("Explicit registry_ref requires an immutable 40-character Gym/CUA-World revision")
    resolved_env_dir = (
        Path(env_dir) if env_dir else resolve_environment_dir(env_name, benchmark)
    ).resolve()
    env_path = resolved_env_dir / "env.json"
    env_data = json.loads(env_path.read_text()) if env_path.is_file() else {}
    effective_runner = (runner or env_data.get("runner") or "qemu").lower()
    if effective_runner not in {"qemu", "qemu_native", "sandweave"}:
        raise ValueError(f"Unsupported Harbor deployment runner: {effective_runner}")
    sandweave = effective_runner == "sandweave"
    resources = None
    if sandweave:
        from ...config.presets import load_preset_env_dict
        from ...utils.merge import deep_merge_env_dict
        merged = deep_merge_env_dict(load_preset_env_dict(env_data["base"]), env_data) if env_data.get("base") else env_data
        requested = merged.get("resources", {})
        if requested.get("gpu", 0):
            raise ValueError("The Sandweave Harbor deployment currently supports CPU desktops only")
        resources = {"cpus": math.ceil(requested.get("cpu") or 1) + 1,
                     "memory_mb": math.ceil((requested.get("mem_gb") or 1) + 4) * 1024,
                     "storage_mb": 32768}
    task_dir = resolved_env_dir / "tasks" / task_id
    task_json_path = task_dir / "task.json"
    task_json = json.loads(task_json_path.read_text())
    description = instruction if instruction is not None else str(task_json.get("description") or "").strip()
    if not description.strip():
        raise ValueError(f"Task {env_name}/{task_id} has no description in {task_json_path}")

    slug = f"{env_name}__{task_id}"
    out_dir = Path(out_root) / slug
    if out_dir.resolve().is_relative_to(resolved_env_dir):
        raise ValueError("Harbor output must be outside the canonical environment")
    bundle = Path(verifier_bundle).resolve() if verifier_bundle is not None else None
    if bundle is not None:
        checklist = json.loads((bundle / "checklist.json").read_text())
        if checklist.get("schema_version") == "binary_outcome_checklist":
            from ...verification.vlm_checklist import validate_binary_checklist
            validate_binary_checklist(checklist)
    (out_dir / "environment").mkdir(parents=True, exist_ok=True)
    (out_dir / "tests").mkdir(exist_ok=True)

    (out_dir / "instruction.md").write_text(description + "\n")

    ga_config: Dict[str, Any] = {
        "benchmark": benchmark,
        "env_name": env_name,
        "task_id": task_id,
        "cache_level": cache_level,
        "use_cache": use_cache,
        "seed": seed,
    }
    if runner:
        ga_config["runner"] = runner
    if sandweave:
        ga_config.update(runner=effective_runner, deployment_profile="sandweave",
                         sandweave_version=_SANDWEAVE_VERSION, portable_agent=True)
    if instruction is not None:
        ga_config["instruction"] = instruction
    if verifier is not _UNSET:
        if verifier:
            ga_config["verifier"] = dict(verifier)
    else:
        ga_config["verifier"] = dict(_DEFAULT_VERIFIER)
    deployment_extras = ",".join(filter(None, [pip_extras, "sandweave,agents" if sandweave else ""]))
    template = _SANDWEAVE_DOCKERFILE if sandweave else _DOCKERFILE
    dockerfile = template.format(ref=gym_anything_ref, extras=f"[{deployment_extras}]" if deployment_extras else "")
    for name in ("verifier", "gym-environment", "gym-runtime"):
        target = out_dir / "environment" / name
        if target.exists():
            shutil.rmtree(target)
    if bundle is not None:
        target = out_dir / "environment" / "verifier"
        copy_assets(bundle, target)
        ga_config["verifier_bundle"] = "verifier"
        ga_config["verifier_bundle_sha256"] = directory_digest(target)
        ga_config.setdefault("verifier", dict(_DEFAULT_VERIFIER))["mode"] = "vlm_checklist"
        dockerfile = dockerfile.replace("COPY gym-anything.json /harbor-task/gym-anything.json",
                                        "COPY gym-anything.json /harbor-task/gym-anything.json\nCOPY verifier /harbor-task/verifier")
    if mode == "vendored_snapshot":
        ga_config["packaging_mode"] = mode
        ga_config["environment_snapshot"] = "gym-environment"
        ga_config["environment_snapshot_sha256"] = snapshot_environment(
            resolved_env_dir, task_id, out_dir / "environment" / "gym-environment")
        ga_config["runtime_snapshot_sha256"] = snapshot_runtime(out_dir / "environment" / "gym-runtime")
        install_start = dockerfile.index("ARG GYM_ANYTHING_REF=")
        install_end = dockerfile.index("COPY gym-anything.json", install_start)
        needs_vlm = (ga_config.get("verifier") or {}).get("mode") == "vlm_checklist"
        extras = ",".join(filter(None, [deployment_extras, "vlm" if needs_vlm else ""]))
        local_spec = "/harbor-task/gym-runtime" + (f"[{extras}]" if extras else "")
        dockerfile = (dockerfile[:install_start]
                      + 'COPY gym-runtime /harbor-task/gym-runtime\n'
                      + f'RUN pip install --no-cache-dir "{local_spec}"'
                      + (' "sandweave[desktop]==0.2.41" "litellm==1.103.0" "dissect.extfs==3.15" "dissect.hypervisor==3.21" "dissect.volume==3.18"' if sandweave else '')
                      + '\n\n'
                      + dockerfile[install_end:])
        dockerfile = dockerfile.replace("COPY gym-anything.json /harbor-task/gym-anything.json",
                                        "COPY gym-anything.json /harbor-task/gym-anything.json\nCOPY gym-environment /harbor-task/gym-environment")
    elif packaging_mode is not None:
        ga_config.update(packaging_mode=mode, gym_anything_ref=gym_anything_ref)
    (out_dir / "environment" / "gym-anything.json").write_text(
        json.dumps(ga_config, indent=2) + "\n"
    )

    (out_dir / "environment" / "Dockerfile").write_text(
        dockerfile
    )
    (out_dir / "environment" / "docker-compose.yaml").write_text(_SANDWEAVE_COMPOSE if sandweave else _DOCKER_COMPOSE)
    if sandweave:
        agent_dir = out_dir / "agent"
        agent_dir.mkdir(exist_ok=True)
        shutil.copy2(Path(__file__).with_name("portable_agent.py"), agent_dir / "gym_controller_agent.py")
        launcher = out_dir / "run-harbor.sh"
        launcher.write_text('''#!/usr/bin/env bash
set -euo pipefail
task_root=$(cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$task_root/agent${PYTHONPATH:+:$PYTHONPATH}"
exec "${HARBOR_BIN:-harbor}" run -p "$task_root" \\
  -a gym_controller_agent:GymControllerAgent "$@"
''')
        launcher.chmod(0o755)
        (out_dir / "README.md").write_text(_SANDWEAVE_README)

    solution_dir = out_dir / "solution"
    solution_dir.mkdir(exist_ok=True)
    solve_path = solution_dir / "solve.sh"
    solve_path.write_text(_SOLVE_SH)
    solve_path.chmod(0o755)

    test_path = out_dir / "tests" / "test.sh"
    test_path.write_text(_TEST_SH)
    test_path.chmod(0o755)

    (out_dir / "task.toml").write_text(
        _render_task_toml(
            name=f"{org}/{slug}",
            description=description,
            keywords=_DEFAULT_KEYWORDS,
            build_timeout_sec=build_timeout_sec,
            agent_timeout_sec=agent_timeout_sec,
            verifier_timeout_sec=verifier_timeout_sec,
            metadata={
                "benchmark": benchmark,
                "env_name": env_name,
                "task_id": task_id,
            },
            resources=resources,
        )
    )
    return out_dir


def compile_environment(
    env_name: str,
    out_root: Path | str,
    *,
    benchmark: str = "cua_world",
    task_ids: Optional[List[str]] = None,
    split: str = "all",
    **task_kwargs: Any,
) -> List[Path]:
    """Compile every task of a benchmark environment (or a named split)."""
    ids = task_ids or get_tasks_for_environment(env_name, benchmark, split=split)
    return [
        compile_task(env_name, task_id, out_root, benchmark=benchmark, **task_kwargs)
        for task_id in ids
    ]


__all__ = ["compile_task", "compile_environment"]
