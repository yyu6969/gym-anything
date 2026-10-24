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
import selectors
import codecs
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

DEFAULT_TIMEOUT_SEC = 7200  # 2 hours per agent invocation
DISALLOWED_TOOLS = "AskUserQuestion,EnterPlanMode,ExitPlanMode,Task(Plan)"
DEFAULT_BLIND_NUDGES = 1
DEFAULT_AUDIT_ROUNDS = 2
CREATE_NEW_ENVIRONMENT = "CREATE_NEW_ENVIRONMENT"
EXTEND_EXISTING_ENVIRONMENT = "EXTEND_EXISTING_ENVIRONMENT"


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


class AgentInvocationError(RuntimeError):
    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.timed_out = False


def _run_agent(
    binary: Path,
    args: list[str],
    timeout: int,
    cwd: Path,
    capture_stdout: bool = False,
    *,
    output_path: Path | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    environment: dict[str, str] | None = None,
    lock_fd: int | None = None,
    display_output: bool = True,
    stdin_text: str | None = None,
) -> str | None:
    """Stream output while enforcing a bounded deadline and successful exit.

    Event callbacks run as lines arrive, including thread identities before a
    turn ends. A killed process never counts as a successful invocation.
    """
    pgid: int | None = None
    proc: subprocess.Popen | None = None
    chunks: list[str] = []
    log = None
    stdin_file = None
    failure: BaseException | None = None
    previous_sigterm = None
    install_handler = threading.current_thread() is threading.main_thread()

    def interrupted(signum: int, frame: object) -> None:
        raise InterruptedError(f"Agent invocation interrupted by signal {signum}")

    if install_handler:
        previous_sigterm = signal.signal(signal.SIGTERM, interrupted)
    try:
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            log = output_path.open("x", encoding="utf-8")
        if stdin_text is not None:
            # File-backed stdin avoids argv limits and blocking pipe writes
            # before output streaming, timeout and interruption handling begin.
            stdin_file = tempfile.TemporaryFile()
            stdin_file.write(stdin_text.encode("utf-8"))
            stdin_file.seek(0)
        proc = subprocess.Popen(
            [str(binary)] + args,
            cwd=str(cwd),
            stdin=stdin_file if stdin_file is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
            env=environment,
            pass_fds=(lock_fd,) if lock_fd is not None else (),
        )
        pgid = os.getpgid(proc.pid)
        if on_event is not None:
            on_event({"type": "aab.process.started", "pid": proc.pid, "pgid": pgid})
        deadline = time.monotonic() + timeout
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        assert proc.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Agent invocation timed out after {timeout}s")
                if not selector.select(min(remaining, 0.2)):
                    continue
                raw = os.read(proc.stdout.fileno(), 65536)
                value = decoder.decode(raw, final=not raw)
                if capture_stdout:
                    chunks.append(value)
                if not capture_stdout and display_output:
                    print(value, end="", flush=True)
                if log is not None:
                    log.write(value)
                    log.flush()
                    os.fsync(log.fileno())
                pending += value
                if not raw and pending and not pending.endswith("\n"):
                    pending += "\n"
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if on_event is not None:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            on_event(event)
                if not raw:
                    selector.unregister(proc.stdout)
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        if proc.returncode != 0:
            raise AgentInvocationError(f"Agent invocation failed with exit code {proc.returncode}", proc.returncode)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if pgid is not None:
            _kill_process_group(pgid)
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
            if failure is not None:
                failure.returncode = proc.returncode
                failure.timed_out = isinstance(failure, (TimeoutError, subprocess.TimeoutExpired))
        if log is not None:
            log.close()
        if stdin_file is not None:
            stdin_file.close()
        if install_handler:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return "".join(chunks) if capture_stdout else None


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
    output_path: Path | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    model: str | None = None,
    lock_fd: int | None = None,
    display_output: bool = True,
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
    if model is not None:
        args += ["--model", model]
    _run_agent(binary, args, timeout, cwd=workspace, output_path=output_path,
               on_event=on_event, lock_fd=lock_fd, display_output=display_output)


def _codex_invoke(
    binary: Path,
    prompt: str,
    *,
    session_id: str | None,
    resume: bool,
    workspace: Path,
    timeout: int,
    output_path: Path | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    environment: dict[str, str] | None = None,
    model: str | None = None,
    lock_fd: int | None = None,
    display_output: bool = True,
) -> str | None:
    # Explicitly override a saved cwd when a run was relocated between nodes.
    args = ["--yolo", "--cd", str(workspace), "exec"]
    if session_id and resume:
        args += ["resume", session_id]
    if model is not None:
        args += ["--model", model]
    args += ["--json", "-"]
    completed = False
    failed = False
    thread_id = session_id

    def event_received(event: dict[str, Any]) -> None:
        nonlocal completed, failed, thread_id
        if event.get("type") == "thread.started":
            emitted = event.get("thread_id")
            if not isinstance(emitted, str) or not emitted or (session_id and emitted != session_id):
                raise RuntimeError("Codex returned an unexpected thread identity")
            thread_id = emitted
        completed = completed or event.get("type") == "turn.completed"
        failed = failed or event.get("type") in {"turn.failed", "error"}
        if on_event is not None:
            on_event(event)

    _run_agent(binary, args, timeout, cwd=workspace, output_path=output_path,
               on_event=event_received, environment=environment, lock_fd=lock_fd,
               display_output=display_output, stdin_text=prompt)
    if failed or not completed or not thread_id:
        raise AgentInvocationError("Codex invocation failed or did not emit turn.completed", 0)
    return thread_id


def _codex_new_session(binary: Path, workspace: Path, timeout: int = 60) -> str:
    """Bootstrap a Codex session and extract its resumable session id."""
    session_id = _codex_invoke(binary, "hi", session_id=None, resume=False,
                               workspace=workspace, timeout=timeout)
    if not session_id:
        raise RuntimeError("Codex bootstrap output did not contain a session id")
    return session_id


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
    runner: str | None = None,
) -> str:
    """Render explicit path ownership without changing the legacy defaults."""

    runtime = (
        f"\n\nRequested application runner: {runner}. Write an explicit \"runner\": \"{runner}\" "
        "in env.json and test with that runner. For Sandweave use Gym runner APIs, "
        "not QEMU, KVM or QEMU SSH; retain QEMU instructions only for QEMU."
        if runner is not None else ""
    )
    if reference_root is None and workspace is None and environment_dir is None:
        return runtime
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
        "for any other temporary working files. The absolute paths in this section "
        "are creator-run locations only: never persist them in env.json, task.json, "
        "hooks, mount sources, recording paths, or other runtime configuration. Use "
        "environment-relative host paths such as ./scripts, ./config, ./assets, "
        "./tasks, and ./artifacts. Guest paths may still use runtime filesystem "
        "locations such as /workspace/scripts, /workspace/config, /workspace/assets, "
        "and /workspace/tasks."
        + runtime
    )


def _aab_context_prompt(
    *,
    software: str,
    task_instruction: str | None,
    video: Path | None,
    environment_spec: dict[str, Any] | None,
    environment_initial_state: dict[str, Any] | None,
    audit: bool,
    creation_mode: str | None = None,
) -> str:
    if (
        task_instruction is None
        and video is None
        and environment_spec is None
        and environment_initial_state is None
    ):
        return ""

    sections = ["## Creation Inputs"]
    if task_instruction is not None:
        sections.append(f"Task instruction: >>>\n{task_instruction}\n<<<")
    if video is not None:
        video_objective = (
            "Extend the cloned shared environment so the demonstrated new task can "
            "be successfully executed without regressing any existing task."
            if creation_mode == EXTEND_EXISTING_ENVIRONMENT
            else "Reconstruct an environment in which the demonstrated task can be "
            "successfully executed."
        )
        sections.append(
            f"Video: >>>\n@{video.as_posix()}\n<<<\n\n"
            f"{video_objective}\n\n"
            "Use the video as the primary evidence for:\n"
            "- application/software\n"
            "- task-relevant UI state\n"
            "- task-relevant data\n"
            "- repository/project state\n"
            "- functionality required by the demonstrated workflow"
        )
    if environment_spec is not None:
        rendered_spec = json.dumps(
            {"environment_spec": environment_spec},
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        sections.append(
            f"Additional environment specification: >>>\n{rendered_spec}\n<<<"
        )
    if environment_initial_state is not None:
        rendered_initial_state = json.dumps(
            {"environment_initial_state": environment_initial_state},
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        sections.append(
            "Additional environment initial-state information: >>>\n"
            f"{rendered_initial_state}\n"
            "<<<"
        )

    responsibilities = [
        "Keep these inputs semantically separate; do not flatten them into one "
        "undifferentiated set of environment facts.",
        f"Treat {software!r} only as a software hint when it is not established "
        "by the video or structured inputs.",
    ]
    if video is not None:
        responsibilities.append(
            "The video remains the primary evidence even when structured "
            "context is supplied. Inspect the accessible video file directly; do "
            "not treat additional JSON as a replacement for it."
        )
    if environment_spec is not None:
        responsibilities.append(
            "Additional environment specification is helpful structured context "
            "for the stable, shared world. Use it for environment construction/"
            "configuration, persistent entities and data, relationships, "
            "permissions, and capabilities. Do not move transient task state into "
            "permanent environment setup."
        )
    if environment_initial_state is not None:
        responsibilities.append(
            "Additional environment initial-state information is helpful structured "
            "context for per-task episode setup. Implement task_preconditions in the "
            "existing task setup/reset hook, and use episode_start to prepare the "
            "initial application, page, authenticated session, and visible UI state. "
            "These are not permanent world properties."
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
            "evidence, including the video when supplied. Do not accept "
            "the creation agent's claims as proof."
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


def _reference_environment_reuse_prompt(root: Path, excluded_output: Path) -> str:
    return (
        "\n\n## Reference Environment Reuse\n\n"
        f"Historical environment directory (read-only reference): @{root.as_posix()}\n"
        f"Exclude the current task output and all its descendants: @{excluded_output.as_posix()}\n\n"
        "Before implementing common software infrastructure from scratch, check for "
        "previously generated environments for the same software under this directory. "
        "Inspect immediate task-output directories only, excluding the current task. "
        "Use result.json and environment/env.json to identify successfully created, "
        "validated and promoted environments for the same software. Each output may "
        "contain environment/config/, environment/scripts/, environment/assets/, and "
        "environment/tasks/. Inspect metadata and relevant implementation files first; "
        "do not recursively read or copy historical creation_audit/ workspaces, caches, "
        "checkpoints or logs.\n\n"
        "Prefer reusing working, compatible implementations over rebuilding equivalent "
        "infrastructure. Useful references include Docker images and Compose configuration, "
        "installation scripts, application startup and readiness checks, browser/CDP "
        "helpers, common runtime setup, and implementations of required functionality. "
        "Adapt components to the current task's instruction, demonstration, required "
        "initial state and runtime assumptions, accounting for application versions, "
        "configuration and data differences. There is no requirement to reconstruct "
        "all software infrastructure independently.\n\n"
        "Do not blindly copy an entire previous environment. Do not copy old task "
        "identity, reconstruction.json, ownership metadata or old task definitions into "
        "the new environment. Do not blindly reuse another task's initial state, "
        "completed outcome or evaluator. Do not modify or extend previous environments, "
        "and never execute setup/reset scripts against the original reference environment. "
        "Create all new environment files in the current candidate environment; keep "
        "runtime paths independent of the historical output location. The current task "
        "instruction and demonstration remain authoritative.\n\n"
        "If the directory is missing, unreadable, empty, or contains no useful compatible "
        "reference, continue normal creation. Briefly report which historical environments "
        "and implementation files you actually reused and adapted, or state that none "
        "were reused."
    )


def _operation_mode_prompt(
    *,
    creation_mode: str | None,
    canonical_environment_id: str | None,
    protected_task_names: Sequence[str],
    task_instruction: str | None,
    reference_environment_root: Path | None = None,
    reference_environment_exclude: Path | None = None,
) -> str:
    """Render the create-versus-extend contract for the creation backend."""

    if creation_mode is None:
        return ""
    if creation_mode == CREATE_NEW_ENVIRONMENT:
        restriction = (
            "Use the supplied environment identity; do not discover, extend or "
            "modify another task's environment or the reference registry. "
        )
        reuse_prompt = ""
        if reference_environment_root is not None:
            if reference_environment_exclude is None:
                raise ValueError("Reference reuse requires the current task output to exclude")
            restriction = (
                "Use the supplied environment identity. You may read historical "
                "environments only as permitted in Reference Environment Reuse below. "
                "Do not extend or modify another task's environment or the reference registry. "
            )
            reuse_prompt = _reference_environment_reuse_prompt(
                reference_environment_root.expanduser().resolve(),
                reference_environment_exclude.expanduser().resolve(),
            )
        return (
            "\n\n## Backend Operation Mode\n\n"
            f"Mode: {CREATE_NEW_ENVIRONMENT}\n\n"
            "The target directory is staging for one task-owned environment. "
            "Create exactly one task with its own initial-state setup and assets. "
            + restriction
            + "Immutable software images and dependencies may be reused."
            + reuse_prompt
        )
    if creation_mode != EXTEND_EXISTING_ENVIRONMENT:
        raise ValueError(f"Unknown creation_mode: {creation_mode!r}")
    if not isinstance(canonical_environment_id, str) or not canonical_environment_id:
        raise ValueError(
            "canonical_environment_id is required in EXTEND_EXISTING_ENVIRONMENT mode"
        )
    if task_instruction is None:
        raise ValueError(
            "task_instruction is required in EXTEND_EXISTING_ENVIRONMENT mode"
        )

    protected = (
        "\n".join(f"- {name}" for name in protected_task_names)
        if protected_task_names
        else "- (none)"
    )
    expected = "\n".join(
        [
            "tasks/",
            *(f"    {name}/" for name in protected_task_names),
            "    <new-task-directory-for-the-requested-task>/",
        ]
    )
    return (
        "\n\n## Backend Operation Mode\n\n"
        f"Mode: {EXTEND_EXISTING_ENVIRONMENT}\n\n"
        "This target directory is already a clone of the canonical persistent "
        "shared environment. You are extending the existing shared environment.\n\n"
        "Do NOT create a new environment. Do NOT change the environment identity. "
        "Do NOT delete, rename, move, replace, or modify pre-existing task "
        "directories. Do NOT treat existing tasks, assets, scripts, configuration, "
        "or seed data as stale reconstruction output. Add support for the new task "
        "while preserving support for all existing tasks.\n\n"
        f"Canonical environment ID: {canonical_environment_id}\n\n"
        "Existing protected tasks (byte-for-byte immutable):\n"
        f"{protected}\n\n"
        "New requested task: >>>\n"
        f"{task_instruction}\n"
        "<<<\n\n"
        "Expected final task structure:\n"
        f"{expected}\n\n"
        "Create exactly one new task directory for the requested task. Its task.json "
        "must preserve the requested instruction and use the canonical environment "
        "ID above as env_id. You MAY update shared environment implementation and "
        "state when necessary, including env.json fields other than id, scripts/, "
        "config/, assets/, shared seed data, and environment hooks."
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
    video: Path | None = None,
    creation_mode: str | None = None,
    canonical_environment_id: str | None = None,
    protected_task_names: Sequence[str] = (),
    runner: str | None = None,
    reference_environment_root: Path | None = None,
    reference_environment_exclude: Path | None = None,
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
            video=video,
            environment_spec=environment_spec,
            environment_initial_state=environment_initial_state,
            audit=False,
            creation_mode=creation_mode,
        )
        + _operation_mode_prompt(
            creation_mode=creation_mode,
            canonical_environment_id=canonical_environment_id,
            protected_task_names=protected_task_names,
            task_instruction=task_instruction,
            reference_environment_root=reference_environment_root,
            reference_environment_exclude=reference_environment_exclude,
        )
        + _path_context_prompt(
            reference_root=reference_root,
            workspace=workspace,
            environment_dir=environment_dir,
            runner=runner,
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
    video: Path | None = None,
    creation_mode: str | None = None,
    canonical_environment_id: str | None = None,
    protected_task_names: Sequence[str] = (),
    runner: str | None = None,
    reference_environment_root: Path | None = None,
    reference_environment_exclude: Path | None = None,
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
        runner=runner,
    )
    if all(
        value is None
        for value in (
            task_instruction,
            video,
            environment_spec,
            environment_initial_state,
        )
    ):
        return (
            prompt
            + _operation_mode_prompt(
                creation_mode=creation_mode,
                canonical_environment_id=canonical_environment_id,
                protected_task_names=protected_task_names,
                task_instruction=task_instruction,
                reference_environment_root=reference_environment_root,
                reference_environment_exclude=reference_environment_exclude,
            )
            + path_context
        )
    return (
        prompt
        + _aab_context_prompt(
            software=_resolve_context_software(software, environment_spec),
            task_instruction=task_instruction,
            video=video,
            environment_spec=environment_spec,
            environment_initial_state=environment_initial_state,
            audit=False,
            creation_mode=creation_mode,
        )
        + _operation_mode_prompt(
            creation_mode=creation_mode,
            canonical_environment_id=canonical_environment_id,
            protected_task_names=protected_task_names,
            task_instruction=task_instruction,
            reference_environment_root=reference_environment_root,
            reference_environment_exclude=reference_environment_exclude,
        )
        + path_context
    )


def _audit_explore_prompt(
    *,
    reference_root: Path | None = None,
    workspace: Path | None = None,
    environment_dir: Path | None = None,
    runner: str | None = None,
) -> str:
    prompt = (
        "deep explore this repository to understand what it is about, "
        "how each individual components work, etc"
    )
    return prompt + _path_context_prompt(
        reference_root=reference_root,
        workspace=workspace,
        environment_dir=environment_dir,
        runner=runner,
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
    video: Path | None = None,
    creation_mode: str | None = None,
    canonical_environment_id: str | None = None,
    protected_task_names: Sequence[str] = (),
    runner: str | None = None,
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
        runner=runner,
    )
    if all(
        value is None
        for value in (
            task_instruction,
            video,
            environment_spec,
            environment_initial_state,
        )
    ):
        return (
            prompt
            + _operation_mode_prompt(
                creation_mode=creation_mode,
                canonical_environment_id=canonical_environment_id,
                protected_task_names=protected_task_names,
                task_instruction=task_instruction,
            )
            + path_context
        )
    return (
        prompt
        + _aab_context_prompt(
            software=_resolve_context_software(software, environment_spec),
            task_instruction=task_instruction,
            video=video,
            environment_spec=environment_spec,
            environment_initial_state=environment_initial_state,
            audit=True,
            creation_mode=creation_mode,
        )
        + _operation_mode_prompt(
            creation_mode=creation_mode,
            canonical_environment_id=canonical_environment_id,
            protected_task_names=protected_task_names,
            task_instruction=task_instruction,
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
    video: Path | None = None,
    creation_mode: str | None = None,
    canonical_environment_id: str | None = None,
    protected_task_names: Sequence[str] = (),
    runner: str | None = None,
    reference_environment_root: Path | None = None,
    reference_environment_exclude: Path | None = None,
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
        runner=runner,
    )
    if all(
        value is None
        for value in (
            task_instruction,
            video,
            environment_spec,
            environment_initial_state,
        )
    ):
        return (
            prompt
            + _operation_mode_prompt(
                creation_mode=creation_mode,
                canonical_environment_id=canonical_environment_id,
                protected_task_names=protected_task_names,
                task_instruction=task_instruction,
                reference_environment_root=reference_environment_root,
                reference_environment_exclude=reference_environment_exclude,
            )
            + path_context
        )
    return (
        prompt
        + _aab_context_prompt(
            software=_resolve_context_software(software, environment_spec),
            task_instruction=task_instruction,
            video=video,
            environment_spec=environment_spec,
            environment_initial_state=environment_initial_state,
            audit=False,
            creation_mode=creation_mode,
        )
        + _operation_mode_prompt(
            creation_mode=creation_mode,
            canonical_environment_id=canonical_environment_id,
            protected_task_names=protected_task_names,
            task_instruction=task_instruction,
            reference_environment_root=reference_environment_root,
            reference_environment_exclude=reference_environment_exclude,
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
    video_path: Path | None = None,
    environment_initial_state: dict[str, Any] | None = None,
    environment_initial_state_path: Path | None = None,
    reference_root: Path | None = None,
    environment_dir: Path | None = None,
    creation_mode: str | None = None,
    canonical_environment_id: str | None = None,
    protected_task_names: Sequence[str] = (),
    pre_audit_validation: Callable[[], None] | None = None,
    runner: str | None = None,
    phase_executor: Callable[[str, str, str, Path | None], None] | None = None,
    reference_environment_root: Path | None = None,
    reference_environment_exclude: Path | None = None,
    candidate_validation_feedback: Callable[[], str | None] | None = None,
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
    if runner is not None:
        prompt_paths["runner"] = runner
    operation_context = {
        "creation_mode": creation_mode,
        "canonical_environment_id": canonical_environment_id,
        "protected_task_names": tuple(protected_task_names),
    }
    creator_operation_context = dict(operation_context)
    if reference_environment_root is not None:
        creator_operation_context.update(
            reference_environment_root=reference_environment_root,
            reference_environment_exclude=reference_environment_exclude,
        )
    _operation_mode_prompt(
        **creator_operation_context,
        task_instruction=task_instruction,
    )

    if phase_executor is not None:
        # The caller owns durable invocation/session state; prompt assembly and
        # phase order remain here, shared with the standalone loop.
        if backend not in {"cc", "codex"}:
            raise ValueError(f"Unknown backend: {backend}")
    elif backend == "cc":
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

    audit_sessions: dict[str, str] = {}

    def execute_phase(name: str, role: str, prompt: str, artifact: Path | None = None) -> None:
        if phase_executor is not None:
            phase_executor(name, role, prompt, artifact)
            return
        if artifact is not None and artifact.exists():
            artifact.rename(artifact.with_name(f"previous-{uuid.uuid4().hex}.md"))
        if role == "creator":
            invoke(prompt, resume=backend == "codex" or name != "generation")
            return
        fresh = role not in audit_sessions
        if fresh:
            audit_sessions[role] = str(uuid.uuid4()) if backend == "cc" else _codex_new_session(binary, workspace)
        agent = _claude_invoke if backend == "cc" else _codex_invoke
        agent(binary, prompt, session_id=audit_sessions[role],
              resume=backend == "codex" or not fresh, workspace=workspace, timeout=timeout_sec)

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
    if creation_mode is not None:
        log_text += f"Creation Mode: {creation_mode}\n"
    if canonical_environment_id is not None:
        log_text += f"Canonical Environment ID: {canonical_environment_id}\n"
    if protected_task_names:
        log_text += "Protected Tasks: " + ", ".join(protected_task_names) + "\n"
    if video_path is not None:
        log_text += f"Video Source: {video_path}\n"
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

    def announce(message: str) -> None:
        if phase_executor is None:
            print(message)

    # Phase 1: initial creation pass
    if start_idx <= 0:
        announce("\n=== Initial Attempt ===")
        execute_phase(
            "generation", "creator",
            _initial_prompt(
                software,
                env_dir,
                memory_dir,
                platform,
                environment_spec,
                task_instruction,
                environment_initial_state,
                video=video_path,
                **creator_operation_context,
                **prompt_paths,
            ),
            # Codex cannot accept a caller-selected session id for a fresh exec.
            # Its session was bootstrapped above, so keep the actual creation
            # prompt in that same resumable session. Claude can start directly
            # with the UUID supplied by this workflow.
        )
        append_log("Initial Attempt Completed")
    else:
        announce(f"Resuming from index {start_idx}, skipping initial attempt")

    # Phase 2: blind nudges
    for i in range(blind_nudges):
        phase_idx = i + 1
        if start_idx > phase_idx:
            announce(f"Skipping blind nudge {phase_idx}")
            continue
        announce(f"\n=== Blind Nudge {phase_idx} ===")
        execute_phase(
            f"nudge-{i + 1}", "creator",
            _nudge_prompt(
                memory_dir,
                platform,
                environment_spec,
                task_instruction,
                environment_initial_state,
                software,
                video=video_path,
                **creator_operation_context,
                **prompt_paths,
            ),
        )
        append_log(f"Blind Nudge {phase_idx} Completed")

    if pre_audit_validation is not None:
        announce("\n=== Candidate Structural Validation ===")
        pre_audit_validation()
        append_log("Candidate Structural Validation Completed")

    # Phase 3: audit rounds with feedback
    for i in range(audit_rounds):
        phase_idx = blind_nudges + i + 1
        if start_idx > phase_idx:
            announce(f"Skipping audit round {i + 1}")
            continue
        announce(f"\n=== Audit Round {i + 1} ===")
        round_dir = audits_dir / f"round-{i + 1}"
        round_dir.mkdir(parents=True, exist_ok=True)
        audit_path = round_dir / f"audit_{env_dir}.md"
        # The auditor uses its own fresh session so it can't see the
        # creation agent's chain-of-thought.
        execute_phase(f"audit-{i + 1}-explore", f"auditor-{i + 1}", _audit_explore_prompt(**prompt_paths))
        append_log(f"Audit Round {i + 1} Exploration Completed")
        execute_phase(
            f"audit-{i + 1}-report", f"auditor-{i + 1}",
            _audit_run_prompt(env_dir, round_dir, memory_dir, platform, environment_spec,
                              task_instruction, environment_initial_state, software,
                              video=video_path, **operation_context, **prompt_paths),
            audit_path,
        )

        if not audit_path.is_file() or not audit_path.read_text(encoding="utf-8").strip():
            raise RuntimeError(f"Audit Round {i + 1} missing or empty audit report: {audit_path}")
        append_log(f"Audit Round {i + 1} Report Completed")
        audit_text = audit_path.read_text(encoding="utf-8")
        shutil.copy2(audit_path, audits_dir / f"audit_{env_dir}.md")
        if candidate_validation_feedback is not None:
            validation_feedback = candidate_validation_feedback()
            if validation_feedback:
                audit_text += "\n\n## Candidate validation failure\n\n" + validation_feedback
        execute_phase(
            f"audit-{i + 1}-feedback", "creator",
            _audit_feedback_prompt(
                audit_text,
                environment_spec,
                task_instruction,
                environment_initial_state,
                software,
                video=video_path,
                **creator_operation_context,
                **prompt_paths,
            ),
        )

        append_log(f"Audit Round {i + 1} Feedback Completed")
        append_log(f"Audit Round {i + 1} Completed")

    announce(
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
