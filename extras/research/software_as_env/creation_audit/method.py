"""
Creation–Audit loop for converting a target software application into a
gym-anything environment, as described in §3 / Appendix E of the CUA-World
paper (Aggarwal et al., 2026).

The pipeline drives a coding+computer-use agent (Claude Code by default,
or Codex CLI as an alternative) through three phases for a single software:

  1. Initial attempt   — agent reads the creation prompt and authors
                         scripts/{install,setup}.sh, env.json, tasks/,
                         and produces evidence_docs/.
  2. Blind nudge ×N    — re-prompts the agent to recheck the creation
                         prompt; recovers omissions caused by context fatigue.
  3. Audit rounds ×M   — an independent agent reads the evidence against
                         the audit checklist and writes audit_<env>.md;
                         the creation agent then ingests that audit and
                         fixes issues.

This module is part of `gym-anything-extras` (research category) and depends
on the gym-anything library only by reading/writing files that conform to
the env.json / task.json contract. It does not import or modify any
gym_anything runtime code.

Invoked through:

    gym-anything-extras research software_as_env creation_audit \
        --software "Moodle" --env-dir moodle_env

or directly:

    python -m extras.research.software_as_env.creation_audit.method \
        --software "Moodle" --env-dir moodle_env
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SEC = 7200  # 2 hours per agent invocation
DISALLOWED_TOOLS = "AskUserQuestion,EnterPlanMode,ExitPlanMode,Task(Plan)"
DEFAULT_BLIND_NUDGES = 1
DEFAULT_AUDIT_ROUNDS = 2


# ---------------------------------------------------------------------------
# AAB artifact interoperability
# ---------------------------------------------------------------------------


def _load_json_object(path: Path, artifact_name: str) -> dict[str, Any]:
    """Load one JSON object while keeping validation at the artifact boundary."""
    path = path.expanduser()
    if not path.exists():
        raise ValueError(f"{artifact_name} file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"{artifact_name} path is not a file: {path}")

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Malformed {artifact_name} JSON in {path}: "
            f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    except OSError as exc:
        raise ValueError(f"Could not read {artifact_name} file {path}: {exc}") from exc

    if not isinstance(document, dict):
        raise ValueError(  # noqa: TRY004
            f"{artifact_name} JSON must be a top-level object"
        )
    return document


def _load_wrapped_aab_artifact(
    path: Path, *, artifact_name: str, wrapper: str
) -> dict[str, Any]:
    document = _load_json_object(path, artifact_name)
    if wrapper not in document:
        raise ValueError(
            f"{artifact_name} JSON is missing required top-level {wrapper!r}"
        )

    artifact = document[wrapper]
    if not isinstance(artifact, dict):
        raise ValueError(f"{wrapper!r} must be an object")  # noqa: TRY004
    return artifact


def load_environment_spec(path: Path) -> dict[str, Any]:
    """Load an AAB Environment Specification using its stable wrapper."""
    return _load_wrapped_aab_artifact(
        path,
        artifact_name="Environment Specification",
        wrapper="environment_spec",
    )


def load_environment_initial_state(path: Path) -> dict[str, Any]:
    """Load an AAB Environment Initial State using its stable wrapper."""
    return _load_wrapped_aab_artifact(
        path,
        artifact_name="Environment Initial State",
        wrapper="environment_initial_state",
    )


def load_task_instruction_artifact(path: Path) -> str:
    """Extract AAB's canonical selected instruction from the observed artifact."""
    document = _load_json_object(path, "Task Instruction artifact")
    output = document.get("output")
    if not isinstance(output, dict):
        raise ValueError("Task Instruction artifact must contain an 'output' object")
    instruction = output.get("selected_instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError(
            "Task Instruction artifact must contain a non-empty "
            "'output.selected_instruction'"
        )
    return instruction.strip()


def _normalize_task_instruction(instruction: str | None) -> str | None:
    if instruction is None:
        return None
    normalized = instruction.strip()
    if not normalized:
        raise ValueError("--task-instruction must be a non-empty value")
    return normalized


def _resolve_software(
    software: str | None, environment_spec: dict[str, Any] | None
) -> str:
    """Resolve the target software and reject conflicts with the contract."""
    supplied_name = software.strip() if software is not None else None
    if supplied_name == "":
        raise ValueError("--software must be a non-empty value")

    if environment_spec is None:
        if supplied_name is None:
            raise ValueError(
                "--software is required when --environment-spec is not supplied"
            )
        return supplied_name

    spec_software = environment_spec.get("software")
    spec_name = spec_software.get("name") if isinstance(spec_software, dict) else None
    if not isinstance(spec_name, str) or not spec_name.strip():
        if supplied_name is None:
            raise ValueError(
                "--software is required when environment_spec.software.name is absent"
            )
        return supplied_name
    spec_name = spec_name.strip()
    if supplied_name is None:
        return spec_name
    if supplied_name.casefold() != spec_name.casefold():
        raise ValueError(
            f"--software {supplied_name!r} does not match EnvironmentSpec "
            f"software name {spec_name!r}"
        )
    return supplied_name


# ---------------------------------------------------------------------------
# Process control
# ---------------------------------------------------------------------------


def _kill_process_group(pgid: int) -> None:
    """SIGTERM then SIGKILL an entire process group, ignoring already-dead pids."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    time.sleep(2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run_agent(
    binary: Path,
    args: list[str],
    timeout: int,
    cwd: Path,
    capture_stdout: bool = False,
) -> str | None:
    """Run the agent CLI. If it doesn't exit (Bun runtime quirk), kill the
    process group after `timeout` seconds — we assume the agent finished.
    """
    pgid: int | None = None
    stdout_data: str | None = None
    try:
        proc = subprocess.Popen(
            [str(binary)] + args,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.STDOUT if capture_stdout else None,
            start_new_session=True,
            text=True,
        )
        pgid = os.getpgid(proc.pid)
        try:
            if capture_stdout:
                stdout_data, _ = proc.communicate(timeout=timeout)
            else:
                proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(
                f"[creation_audit] agent did not exit after {timeout}s — assuming done, killing."
            )
            if capture_stdout and proc.stdout is not None:
                stdout_data = proc.stdout.read()
    finally:
        if pgid is not None:
            _kill_process_group(pgid)
    return stdout_data


# ---------------------------------------------------------------------------
# Backend wrappers
# ---------------------------------------------------------------------------


def _resolve_bin(explicit: str | None, env_var: str, name: str) -> Path:
    """Pick the agent binary in priority order: --flag > env var > PATH."""
    candidate = explicit or os.environ.get(env_var) or shutil.which(name)
    if not candidate:
        raise RuntimeError(
            f"Could not find {name} CLI. Install it, set {env_var}=<path>, "
            f"or pass --{name}-bin."
        )
    path = Path(candidate)
    if not path.is_file():
        raise RuntimeError(f"{name} binary not found: {path}")
    return path


def _claude_invoke(
    binary: Path,
    prompt: str,
    *,
    session_id: str | None,
    resume: bool,
    workspace: Path,
    timeout: int,
) -> None:
    args = [
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        "--disallowedTools",
        DISALLOWED_TOOLS,
    ]
    if session_id is None:
        raise ValueError("Claude backend requires a session_id")
    args += ["--resume", session_id] if resume else ["--session-id", session_id]
    _run_agent(binary, args, timeout, cwd=workspace)


def _codex_invoke(
    binary: Path,
    prompt: str,
    *,
    session_id: str | None,
    resume: bool,
    workspace: Path,
    timeout: int,
) -> None:
    # Codex CLI argv layout: codex --yolo exec [resume <id>] "<prompt>"
    args = ["--yolo", "exec"]
    if session_id and resume:
        args += ["resume", session_id]
    args += [prompt]
    _run_agent(binary, args, timeout, cwd=workspace)


def _codex_new_session(binary: Path, workspace: Path, timeout: int = 60) -> str:
    """Bootstrap a Codex session and extract its resumable session id."""
    out = _run_agent(
        binary, ["--yolo", "exec", "hi"], timeout, cwd=workspace, capture_stdout=True
    )
    if not out:
        raise RuntimeError("Codex did not return any output for session bootstrap")

    prefix = "session id:"
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.casefold().startswith(prefix):
            session_id = stripped[len(prefix) :].strip()
            if session_id:
                return session_id
    raise RuntimeError("Codex bootstrap output did not contain a session id")


# ---------------------------------------------------------------------------
# Pipeline phases
# ---------------------------------------------------------------------------


SUPPORTED_PLATFORMS = ("linux", "macos")


def _benchmark_root(platform: str) -> str:
    """Top-level benchmark dir for the chosen target platform.

    Linux envs live under benchmarks/cua_world/. macOS envs live under
    benchmarks/cua_world-macos/ (built on UseComputerRunner + the
    use.computer fleet — see env_creation_notes/12_macos_environments.md).
    """
    return (
        "benchmarks/cua_world-macos" if platform == "macos" else "benchmarks/cua_world"
    )


def _creation_prompt_path(memory_dir: Path, platform: str) -> str:
    """Per-platform creation prompt. Linux uses prompt.md; macOS uses
    prompt_macos.md (which references 12_macos_environments.md, base="macos",
    UseComputerRunner, /Users/lume/workspace paths, etc.)."""
    if platform == "macos":
        return (memory_dir / "env_creation_notes" / "prompt_macos.md").as_posix()
    return (memory_dir / "env_creation_notes" / "prompt.md").as_posix()


def _audit_prompt_path(memory_dir: Path, platform: str) -> str:
    """Per-platform audit prompt. macOS variant has the cua_world-macos path."""
    if platform == "macos":
        return (memory_dir / "audit_prompt_macos.md").as_posix()
    return (memory_dir / "audit_prompt.md").as_posix()


def _path_context_prompt(
    *,
    reference_root: Path | None,
    workspace: Path | None,
    environment_dir: Path | None,
) -> str:
    """Render explicit path ownership without changing the legacy defaults."""

    if reference_root is None and workspace is None and environment_dir is None:
        return ""
    if reference_root is None or workspace is None or environment_dir is None:
        raise ValueError(
            "reference_root, workspace, and environment_dir must be supplied together"
        )
    return (
        "\n\n## Repository and Output Paths\n\n"
        f"- Gym reference repository (read-only): @{reference_root.as_posix()}\n"
        f"- Writable agent workspace: @{workspace.as_posix()}\n"
        f"- Target environment directory: @{environment_dir.as_posix()}\n\n"
        "Resolve every repository-relative source or example path in the creation "
        "and audit instructions against the Gym reference repository. The absolute "
        "target environment directory above overrides any benchmark-relative "
        "destination shown in those instructions. Do not modify or create files "
        "inside the Gym reference repository. Write generated environment files "
        "only inside the target environment directory; use the writable workspace "
        "for any other temporary working files."
    )


def _aab_context_prompt(
    *,
    software: str,
    task_instruction: str | None,
    environment_spec: dict[str, Any] | None,
    environment_initial_state: dict[str, Any] | None,
    audit: bool,
) -> str:
    if (
        task_instruction is None
        and environment_spec is None
        and environment_initial_state is None
    ):
        return ""

    sections = [f"## Input\n\nSoftware: >>>\n{software}\n<<<"]
    if task_instruction is not None:
        sections.append(f"Task Instruction: >>>\n{task_instruction}\n<<<")
    if environment_spec is not None:
        rendered_spec = json.dumps(
            {"environment_spec": environment_spec},
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        sections.append(f"Environment Specification: >>>\n{rendered_spec}\n<<<")
    if environment_initial_state is not None:
        rendered_initial_state = json.dumps(
            {"environment_initial_state": environment_initial_state},
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        sections.append(
            "Environment Initial State: >>>\n"
            f"{rendered_initial_state}\n"
            "<<<"
        )

    responsibilities = [
        "Keep these inputs semantically separate; do not flatten them into one "
        "undifferentiated set of environment facts."
    ]
    if environment_spec is not None:
        responsibilities.append(
            "Environment Specification describes the stable, shared world. Use it "
            "for environment construction/configuration, persistent entities and "
            "data, relationships, permissions, and capabilities. Do not move "
            "transient task state into permanent environment setup."
        )
    if environment_initial_state is not None:
        responsibilities.append(
            "Environment Initial State describes per-task episode setup. Implement "
            "task_preconditions in the existing task setup/reset hook, and use "
            "episode_start to prepare the initial application, page, authenticated "
            "session, and visible UI state. These are not permanent world properties."
        )
    if task_instruction is not None:
        responsibilities.append(
            "Task Instruction is the benchmark task presented to the agent. Preserve "
            "its exact wording and intent in the generated task description, support "
            "that task with the environment and pre-task setup, and do not invent an "
            "unrelated replacement task."
        )

    if audit:
        responsibilities.append(
            "Audit each supplied input against its own responsibility using direct "
            "evidence: shared-world compliance for Environment Specification, "
            "pre-task/reset/start-state compliance for Environment Initial State, "
            "and exact task-description and task-support compliance for Task "
            "Instruction. Do not accept the creation agent's claims as proof."
        )
    else:
        responsibilities.append(
            "Use the existing Gym-Anything structure: shared env.json and environment "
            "hooks for the stable world, and the generated task.json plus pre_task "
            "hook for the supplied task and its episode start state."
        )

    rendered_responsibilities = "\n".join(
        f"- {responsibility}" for responsibility in responsibilities
    )
    return (
        "\n\n"
        + "\n\n".join(sections)
        + "\n\n## Semantic Responsibilities\n\n"
        + rendered_responsibilities
    )


def _resolve_context_software(
    software: str | None, environment_spec: dict[str, Any] | None
) -> str:
    if software is not None:
        return software
    if environment_spec is not None:
        spec_software = environment_spec.get("software")
        if isinstance(spec_software, dict):
            name = spec_software.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    raise ValueError("software is required when rendering AAB context")


def _initial_prompt(
    software: str,
    env_dir: str,
    memory_dir: Path,
    platform: str = "linux",
    environment_spec: dict[str, Any] | None = None,
    task_instruction: str | None = None,
    environment_initial_state: dict[str, Any] | None = None,
    reference_root: Path | None = None,
    workspace: Path | None = None,
    environment_dir: Path | None = None,
) -> str:
    target = environment_dir.as_posix() if environment_dir is not None else env_dir
    prompt = (
        f"read @{_creation_prompt_path(memory_dir, platform)} and follow the prompt. "
        f"target application is {software} and target env directory is {target}. "
        f"Do not enter plan mode (although you are strongly encouraged to plan "
        f"before making code edits), or ask me for any input at any time. "
        f"All information is already present in the prompt file."
    )
    return (
        prompt
        + _aab_context_prompt(
            software=software,
            task_instruction=task_instruction,
            environment_spec=environment_spec,
            environment_initial_state=environment_initial_state,
            audit=False,
        )
        + _path_context_prompt(
            reference_root=reference_root,
            workspace=workspace,
            environment_dir=environment_dir,
        )
    )


def _nudge_prompt(
    memory_dir: Path,
    platform: str = "linux",
    environment_spec: dict[str, Any] | None = None,
    task_instruction: str | None = None,
    environment_initial_state: dict[str, Any] | None = None,
    software: str | None = None,
    reference_root: Path | None = None,
    workspace: Path | None = None,
    environment_dir: Path | None = None,
) -> str:
    prompt = (
        f"reread @{_creation_prompt_path(memory_dir, platform)}. "
        f"you haven't completed the task yet. (Unrelated Context: remember to "
        f"use the visual_grounding MCP tool to interact with the running "
        f"environment)"
    )
    path_context = _path_context_prompt(
        reference_root=reference_root,
        workspace=workspace,
        environment_dir=environment_dir,
    )
    if all(
        value is None
        for value in (task_instruction, environment_spec, environment_initial_state)
    ):
        return prompt + path_context
    return (
        prompt
        + _aab_context_prompt(
            software=_resolve_context_software(software, environment_spec),
            task_instruction=task_instruction,
            environment_spec=environment_spec,
            environment_initial_state=environment_initial_state,
            audit=False,
        )
        + path_context
    )


def _audit_explore_prompt(
    *,
    reference_root: Path | None = None,
    workspace: Path | None = None,
    environment_dir: Path | None = None,
) -> str:
    prompt = (
        "deep explore this repository to understand what it is about, "
        "how each individual components work, etc"
    )
    return prompt + _path_context_prompt(
        reference_root=reference_root,
        workspace=workspace,
        environment_dir=environment_dir,
    )


def _audit_run_prompt(
    env_dir: str,
    audits_dir: Path,
    memory_dir: Path,
    platform: str = "linux",
    environment_spec: dict[str, Any] | None = None,
    task_instruction: str | None = None,
    environment_initial_state: dict[str, Any] | None = None,
    software: str | None = None,
    reference_root: Path | None = None,
    workspace: Path | None = None,
    environment_dir: Path | None = None,
) -> str:
    audit_file_rel = (audits_dir / f"audit_{env_dir}.md").as_posix()
    target_dir = (
        environment_dir.as_posix()
        if environment_dir is not None
        else f"{_benchmark_root(platform)}/environments/{env_dir}"
    )
    prompt = (
        f"read @{_audit_prompt_path(memory_dir, platform)} and follow the prompt. "
        f"target env directory is @{target_dir}. "
        f"Note: save file is {audit_file_rel}"
    )
    path_context = _path_context_prompt(
        reference_root=reference_root,
        workspace=workspace,
        environment_dir=environment_dir,
    )
    if all(
        value is None
        for value in (task_instruction, environment_spec, environment_initial_state)
    ):
        return prompt + path_context
    return (
        prompt
        + _aab_context_prompt(
            software=_resolve_context_software(software, environment_spec),
            task_instruction=task_instruction,
            environment_spec=environment_spec,
            environment_initial_state=environment_initial_state,
            audit=True,
        )
        + path_context
    )


def _audit_feedback_prompt(
    audit_text: str,
    environment_spec: dict[str, Any] | None = None,
    task_instruction: str | None = None,
    environment_initial_state: dict[str, Any] | None = None,
    software: str | None = None,
    reference_root: Path | None = None,
    workspace: Path | None = None,
    environment_dir: Path | None = None,
) -> str:
    prompt = (
        f"An independent audit of your progress was performed. Here is the "
        f"audit: {audit_text}. Please fix the issues. (Unrelated Context: "
        f"remember to use the visual_grounding MCP tool to interact with the "
        f"running environment)"
    )
    path_context = _path_context_prompt(
        reference_root=reference_root,
        workspace=workspace,
        environment_dir=environment_dir,
    )
    if all(
        value is None
        for value in (task_instruction, environment_spec, environment_initial_state)
    ):
        return prompt + path_context
    return (
        prompt
        + _aab_context_prompt(
            software=_resolve_context_software(software, environment_spec),
            task_instruction=task_instruction,
            environment_spec=environment_spec,
            environment_initial_state=environment_initial_state,
            audit=False,
        )
        + path_context
    )


def run_creation_audit(
    *,
    software: str,
    env_dir: str,
    backend: str,
    platform: str,
    blind_nudges: int,
    audit_rounds: int,
    start_idx: int,
    session_id: str | None,
    workspace: Path,
    memory_dir: Path,
    audits_dir: Path,
    logs_dir: Path,
    claude_bin: str | None,
    codex_bin: str | None,
    timeout_sec: int,
    environment_spec: dict[str, Any] | None = None,
    environment_spec_path: Path | None = None,
    task_instruction: str | None = None,
    task_instruction_path: Path | None = None,
    environment_initial_state: dict[str, Any] | None = None,
    environment_initial_state_path: Path | None = None,
    reference_root: Path | None = None,
    environment_dir: Path | None = None,
) -> int:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(
            f"--platform must be one of {SUPPORTED_PLATFORMS}, got {platform!r}"
        )
    workspace = workspace.expanduser().resolve()
    explicit_path_layout = reference_root is not None or environment_dir is not None
    resolved_reference_root = (
        reference_root.expanduser().resolve()
        if reference_root is not None
        else workspace
    )
    resolved_environment_dir = (
        environment_dir.expanduser().resolve()
        if environment_dir is not None
        else workspace / _benchmark_root(platform) / "environments" / env_dir
    )
    if explicit_path_layout and not (
        resolved_reference_root / "src" / "gym_anything"
    ).is_dir():
        raise ValueError(
            "reference_root must be a Gym-Anything repository containing "
            f"src/gym_anything: {resolved_reference_root}"
        )
    workspace.mkdir(parents=True, exist_ok=True)
    resolved_environment_dir.parent.mkdir(parents=True, exist_ok=True)
    audits_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    prompt_paths = {
        "reference_root": resolved_reference_root if explicit_path_layout else None,
        "workspace": workspace if explicit_path_layout else None,
        "environment_dir": resolved_environment_dir if explicit_path_layout else None,
    }

    if backend == "cc":
        binary = _resolve_bin(claude_bin, "CLAUDE_BIN", "claude")
        invoke = lambda prompt, *, resume: _claude_invoke(
            binary,
            prompt,
            session_id=session_id,
            resume=resume,
            workspace=workspace,
            timeout=timeout_sec,
        )
        if session_id is None:
            session_id = str(uuid.uuid4())
    elif backend == "codex":
        binary = _resolve_bin(codex_bin, "CODEX_BIN", "codex")
        if session_id is None:
            session_id = _codex_new_session(binary, workspace)
        invoke = lambda prompt, *, resume: _codex_invoke(
            binary,
            prompt,
            session_id=session_id,
            resume=resume,
            workspace=workspace,
            timeout=timeout_sec,
        )
    else:
        raise ValueError(f"Unknown backend: {backend!r}; expected 'cc' or 'codex'")

    log_path = logs_dir / f"{env_dir}.txt"
    if explicit_path_layout:
        target_description = resolved_environment_dir.as_posix()
        path_log = f"Gym Reference Root: {resolved_reference_root}\n"
    else:
        target_description = (
            f"{env_dir} (under {_benchmark_root(platform)}/environments/)"
        )
        path_log = ""
    log_text = (
        f"Session ID: {session_id}\n"
        f"Backend: {backend}\n"
        f"Platform: {platform}\n"
        f"Target Application: {software}\n"
        f"Target Env Directory: {target_description}\n"
        f"Workspace: {workspace}\n"
        f"{path_log}"
        f"Audits Dir: {audits_dir}\n"
        f"Start Index: {start_idx}\n"
        f"Blind Nudges: {blind_nudges}\n"
        f"Audit Rounds: {audit_rounds}\n"
    )
    if task_instruction_path is not None:
        log_text += f"Task Instruction Source: {task_instruction_path}\n"
    if task_instruction is not None:
        log_text += f"Task Instruction:\n{task_instruction}\n"
    if environment_spec_path is not None:
        log_text += f"EnvironmentSpec Source: {environment_spec_path}\n"
    if environment_spec is not None:
        log_text += (
            "EnvironmentSpec JSON:\n"
            + json.dumps(
                {"environment_spec": environment_spec},
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    if environment_initial_state_path is not None:
        log_text += (
            f"Environment Initial State Source: {environment_initial_state_path}\n"
        )
    if environment_initial_state is not None:
        log_text += (
            "Environment Initial State JSON:\n"
            + json.dumps(
                {"environment_initial_state": environment_initial_state},
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    log_path.write_text(log_text, encoding="utf-8")

    def append_log(line: str) -> None:
        with log_path.open("a") as fh:
            fh.write(line + "\n")

    # Phase 1: initial creation pass
    if start_idx <= 0:
        print("\n=== Initial Attempt ===")
        invoke(
            _initial_prompt(
                software,
                env_dir,
                memory_dir,
                platform,
                environment_spec,
                task_instruction,
                environment_initial_state,
                **prompt_paths,
            ),
            # Codex cannot accept a caller-selected session id for a fresh exec.
            # Its session was bootstrapped above, so keep the actual creation
            # prompt in that same resumable session. Claude can start directly
            # with the UUID supplied by this workflow.
            resume=backend == "codex",
        )
        append_log("Initial Attempt Completed")
    else:
        print(f"Resuming from index {start_idx}, skipping initial attempt")

    # Phase 2: blind nudges
    for i in range(blind_nudges):
        phase_idx = i + 1
        if start_idx > phase_idx:
            print(f"Skipping blind nudge {phase_idx}")
            continue
        print(f"\n=== Blind Nudge {phase_idx} ===")
        invoke(
            _nudge_prompt(
                memory_dir,
                platform,
                environment_spec,
                task_instruction,
                environment_initial_state,
                software,
                **prompt_paths,
            ),
            resume=True,
        )
        append_log(f"Blind Nudge {phase_idx} Completed")

    # Phase 3: audit rounds with feedback
    for i in range(audit_rounds):
        phase_idx = blind_nudges + i + 1
        if start_idx > phase_idx:
            print(f"Skipping audit round {i + 1}")
            continue
        print(f"\n=== Audit Round {i + 1} ===")
        # The auditor uses its own fresh session so it can't see the
        # creation agent's chain-of-thought.
        if backend == "cc":
            audit_session = str(uuid.uuid4())
            _claude_invoke(
                binary,
                _audit_explore_prompt(**prompt_paths),
                session_id=audit_session,
                resume=False,
                workspace=workspace,
                timeout=timeout_sec,
            )
            _claude_invoke(
                binary,
                _audit_run_prompt(
                    env_dir,
                    audits_dir,
                    memory_dir,
                    platform,
                    environment_spec,
                    task_instruction,
                    environment_initial_state,
                    software,
                    **prompt_paths,
                ),
                session_id=audit_session,
                resume=True,
                workspace=workspace,
                timeout=timeout_sec,
            )
        else:
            audit_session = _codex_new_session(binary, workspace)
            _codex_invoke(
                binary,
                _audit_explore_prompt(**prompt_paths),
                session_id=audit_session,
                resume=True,
                workspace=workspace,
                timeout=timeout_sec,
            )
            _codex_invoke(
                binary,
                _audit_run_prompt(
                    env_dir,
                    audits_dir,
                    memory_dir,
                    platform,
                    environment_spec,
                    task_instruction,
                    environment_initial_state,
                    software,
                    **prompt_paths,
                ),
                session_id=audit_session,
                resume=True,
                workspace=workspace,
                timeout=timeout_sec,
            )

        audit_path = audits_dir / f"audit_{env_dir}.md"
        if not audit_path.exists():
            print(
                f"[creation_audit] audit file missing at {audit_path}; skipping feedback"
            )
            append_log(f"Audit Round {i + 1} produced no audit file")
            continue
        audit_text = audit_path.read_text(encoding="utf-8")
        invoke(
            _audit_feedback_prompt(
                audit_text,
                environment_spec,
                task_instruction,
                environment_initial_state,
                software,
                **prompt_paths,
            ),
            resume=True,
        )

        if i < audit_rounds - 1:
            # Remove the audit so the next round writes fresh evidence.
            try:
                audit_path.unlink()
            except OSError:
                pass
        append_log(f"Audit Round {i + 1} Completed")

    print(
        f"\nCreation–Audit complete: software={software}, env_dir={env_dir}, "
        f"session={session_id}"
    )
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _packaged_memory_dir() -> Path:
    return Path(__file__).resolve().parent / "memory"


def _default_workspace() -> Path:
    """Default to the gym-anything project root (4 levels up from this file)."""
    here = Path(__file__).resolve()
    candidate = here.parents[
        4
    ]  # extras/research/software_as_env/creation_audit/method.py -> repo root
    return candidate if (candidate / "src" / "gym_anything").is_dir() else Path.cwd()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gym-anything-extras research software_as_env creation_audit",
        description="Run the creation–audit loop on one software target.",
    )
    parser.add_argument(
        "--software",
        help=(
            "Human-readable software name (e.g. 'Moodle'); required unless "
            "--environment-spec supplies environment_spec.software.name"
        ),
    )
    task_instruction_group = parser.add_mutually_exclusive_group()
    task_instruction_group.add_argument(
        "--task-instruction",
        help="Exact benchmark task instruction to preserve in the generated task",
    )
    task_instruction_group.add_argument(
        "--task-instruction-file",
        type=Path,
        help=(
            "AAB task-instruction selection artifact; reads "
            "output.selected_instruction"
        ),
    )
    parser.add_argument(
        "--environment-spec",
        type=Path,
        help=(
            "AAB Environment Specification JSON for stable shared-world "
            "reconstruction"
        ),
    )
    parser.add_argument(
        "--environment-initial-state",
        type=Path,
        help="AAB Environment Initial State JSON for per-task setup and start state",
    )
    parser.add_argument(
        "--env-dir", required=True, help="Target env folder name (e.g. 'moodle_env')"
    )
    parser.add_argument(
        "--platform",
        choices=SUPPORTED_PLATFORMS,
        default="linux",
        help=(
            "Target platform. 'linux' (default) → benchmarks/cua_world with "
            "QEMU/Apptainer + ubuntu-gnome. 'macos' → benchmarks/cua_world-macos "
            "with UseComputerRunner + the use.computer fleet (see "
            "env_creation_notes/12_macos_environments.md). Selects per-platform "
            "creation + audit prompts."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=("cc", "codex"),
        default="cc",
        help="Agent backend: cc=Claude Code, codex=Codex CLI",
    )
    parser.add_argument("--blind-nudges", type=int, default=DEFAULT_BLIND_NUDGES)
    parser.add_argument("--audit-rounds", type=int, default=DEFAULT_AUDIT_ROUNDS)
    parser.add_argument(
        "--start-idx",
        type=int,
        default=0,
        help="Resume from this phase index (0=initial, 1=first nudge, ...)",
    )
    parser.add_argument(
        "--session-id", default=None, help="Resume an existing agent session"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Path the agent operates from; defaults to the gym-anything repo root",
    )
    parser.add_argument(
        "--reference-root",
        type=Path,
        default=None,
        help="Read-only Gym-Anything repository used to resolve reference paths",
    )
    parser.add_argument(
        "--environment-dir",
        type=Path,
        default=None,
        help="Exact directory where the generated environment must be written",
    )
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=None,
        help="Override the packaged memory directory",
    )
    parser.add_argument(
        "--audits-dir",
        type=Path,
        default=None,
        help="Where audit files are written; defaults to <workspace>/audits",
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=None,
        help="Where run logs are written; defaults to <workspace>/creation_audit_logs",
    )
    parser.add_argument("--claude-bin", default=None, help="Path to claude CLI")
    parser.add_argument("--codex-bin", default=None, help="Path to codex CLI")
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help="Per-agent-invocation timeout in seconds (default 7200)",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    task_instruction_path = None
    environment_spec_path = None
    environment_initial_state_path = None
    environment_spec = None
    environment_initial_state = None
    try:
        task_instruction = _normalize_task_instruction(args.task_instruction)
        if args.task_instruction_file is not None:
            task_instruction_path = (
                args.task_instruction_file.expanduser().resolve()
            )
            task_instruction = load_task_instruction_artifact(task_instruction_path)
        if args.environment_spec is not None:
            environment_spec_path = args.environment_spec.expanduser().resolve()
            environment_spec = load_environment_spec(environment_spec_path)
        if args.environment_initial_state is not None:
            environment_initial_state_path = (
                args.environment_initial_state.expanduser().resolve()
            )
            environment_initial_state = load_environment_initial_state(
                environment_initial_state_path
            )
        software = _resolve_software(args.software, environment_spec)
    except ValueError as exc:
        parser.error(str(exc))

    workspace = (args.workspace or _default_workspace()).resolve()
    memory_dir = (args.memory_dir or _packaged_memory_dir()).resolve()
    audits_dir = (args.audits_dir or workspace / "audits").resolve()
    logs_dir = (args.logs_dir or workspace / "creation_audit_logs").resolve()
    reference_root = (
        args.reference_root.expanduser().resolve()
        if args.reference_root is not None
        else None
    )
    environment_dir = (
        args.environment_dir.expanduser().resolve()
        if args.environment_dir is not None
        else None
    )

    return run_creation_audit(
        software=software,
        env_dir=args.env_dir,
        backend=args.backend,
        platform=args.platform,
        blind_nudges=args.blind_nudges,
        audit_rounds=args.audit_rounds,
        start_idx=args.start_idx,
        session_id=args.session_id,
        workspace=workspace,
        memory_dir=memory_dir,
        audits_dir=audits_dir,
        logs_dir=logs_dir,
        claude_bin=args.claude_bin,
        codex_bin=args.codex_bin,
        timeout_sec=args.timeout_sec,
        environment_spec=environment_spec,
        environment_spec_path=environment_spec_path,
        task_instruction=task_instruction,
        task_instruction_path=task_instruction_path,
        environment_initial_state=environment_initial_state,
        environment_initial_state_path=environment_initial_state_path,
        reference_root=reference_root,
        environment_dir=environment_dir,
    )


if __name__ == "__main__":
    raise SystemExit(run())
