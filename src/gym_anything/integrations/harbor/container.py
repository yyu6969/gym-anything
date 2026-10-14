"""In-container runtime for docker-shaped gym-anything Harbor tasks.

The compiled task's Dockerfile bakes ``environment/gym-anything.json`` into
the image and sets this module as the entrypoint (``serve`` mode). At
container start it boots the task's guest through the standard runner stack
(``qemu_native`` by default: QEMU directly inside the container, the same
shape the ModalRunner sandbox uses) and then serves a small HTTP API on
localhost:

    GET  /health    -> 200 once the environment is booted
    POST /observe   -> {"screenshot_path": ..., "resolution": [w, h]}
    POST /step      -> {"actions": [...]} -> {"done": ..., "action_result": ..., obs}
    POST /finalize  -> run the task's real grading pipeline once;
                       returns {"reward": float, "verifier": {...}}

``tests/test.sh`` calls ``python -m gym_anything.integrations.harbor.container
finalize`` inside the container, which hits ``/finalize`` and writes
``/logs/verifier/reward.json`` in the shape Harbor's Verifier reads. Harbor
agents on the host drive the guest through the same API via
``environment.exec`` + curl (see ``agent.ContainerDriver``).

Grading runs in the container, not in the guest: the same trust boundary the
OSWorld adapter uses (the container wraps the agent-controlled VM).

This module must not import ``harbor``: it runs inside task containers where
only gym-anything is installed. The Harbor-facing backend
(``integrations/harbor/environment.py``) imports the shared boot/grading helpers from
here.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.request import Request, urlopen

from .packaging import directory_digest, resolve_asset

DEFAULT_TASK_CONFIG_PATH = "/harbor-task/gym-anything.json"
DEFAULT_PORT = 7317
DEFAULT_CONTAINER_RUNNER = "qemu"  # auto-selects apptainer/native per host
logger = logging.getLogger(__name__)

_REWARD_JSON_PATH = "/logs/verifier/reward.json"
_VERIFIER_JSON_PATH = "/logs/verifier/verifier.json"


# -- shared boot / grading helpers (also used by the Harbor backend) ----------


def load_task_config(path: Path | str) -> Dict[str, Any]:
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    config["_asset_root"] = str(path.parent)
    return config


def boot_env(config: Dict[str, Any], *, force_build: bool = False, default_runner: Optional[str] = None,
             artifact_root: Optional[Path] = None):
    """Boot a GymAnythingEnv from a compiled task's gym-anything.json config.

    The same boot path the verifiers adapter uses, checkpoint caching
    included. Harbor owns the trial's step/time budget, so episode limits are
    effectively disabled.
    """
    from ...config.loading import from_config
    from ...registry import resolve_environment_dir
    from ...runtime.runners.sandweave import SandweaveRunner
    from ...runtime_paths import reusable_cache_paths

    if config.get("deployment_profile") == "sandweave":
        if config.get("runner") != "sandweave":
            raise ValueError("Sandweave deployment profile requires runner=sandweave")
        for name in ("GA_HARBOR_RUNNER", "GYM_ANYTHING_RUNNER"):
            if os.environ.get(name, "sandweave").lower() != "sandweave":
                raise ValueError(f"{name} conflicts with the Sandweave deployment profile")

    asset_root = Path(config.get("_asset_root", "/harbor-task"))
    if config.get("environment_snapshot"):
        env_dir = resolve_asset(config, "environment_snapshot", asset_root)
        if directory_digest(env_dir) != config["environment_snapshot_sha256"]:
            raise ValueError("Packaged environment snapshot digest mismatch")
    else:
        env_dir = config.get("env_dir") or resolve_environment_dir(
            config["env_name"], config.get("benchmark")
        )
    # Resolve and validate external grading inputs before boot, fail closed.
    if config.get("verifier_bundle"):
        _external_checklist(config)
    runner = config.get("runner") or default_runner
    overrides = {"runner": runner} if runner else {}
    if config.get("environment_snapshot"):
        # Hooks, writable mounts and Python verifier imports may write into
        # an environment. Give each trial its own copy of the immutable
        # packaged snapshot, with recordings alongside it.
        trial_root = Path(tempfile.mkdtemp(prefix="harbor-gym-episode-"))
        runtime_env_dir = trial_root / "gym-environment"
        shutil.copytree(env_dir, runtime_env_dir)
        env_dir = runtime_env_dir
        spec = json.loads((Path(env_dir) / "env.json").read_text())
        overrides["mounts"] = [
            {**mount, "source": str((Path(env_dir) / mount["source"]).resolve())}
            for mount in spec.get("mounts", [])
        ]
        # Recording must never modify a content-addressed deployment snapshot.
        overrides["recording"] = {"output_dir": str(artifact_root or trial_root / "artifacts")}
    env = from_config(env_dir, task_id=config["task_id"], overrides=overrides)
    if config.get("environment_snapshot") and env.env_spec.dockerfile:
        from ... import __file__ as package_file
        dockerfile = Path(env.env_spec.dockerfile)
        if str(dockerfile).startswith("gym_anything/"):
            dockerfile = Path(package_file).parent.parent / dockerfile
        elif not dockerfile.is_absolute():
            dockerfile = Path(env_dir) / dockerfile
        if not dockerfile.is_file():
            raise FileNotFoundError(f"Packaged Dockerfile is missing: {dockerfile}")
        env.env_spec.dockerfile = str(dockerfile.resolve())
    if config.get("instruction") is not None:
        env.task_spec.description = config["instruction"]
    apply_verifier_config(env, config)
    if isinstance(env.runner, SandweaveRunner):
        # Native CLI setup applies this machine-cache default before reset.
        # Custom Harbor backends bypass that CLI; retain any explicit SDK home.
        os.environ.setdefault("SANDWEAVE_HOME", str(reusable_cache_paths().sandweave))
    try:
        env.reset(
            seed=int(config.get("seed", 0)),
            use_cache=bool(config.get("use_cache", True)) and not force_build,
            cache_level=config.get("cache_level", "post_start"),
            use_savevm=bool(config.get("use_savevm", False)),
        )
    except BaseException as exc:
        try:
            env.close()
        except Exception as cleanup_error:
            exc.add_note(f"Environment cleanup failed: {cleanup_error}")
            logger.exception("Environment cleanup after failed boot failed")
        raise
    env.set_episode_limits(max_steps=100_000, timeout_sec=10**9)
    return env


def _external_checklist(config: Dict[str, Any]) -> Path:
    bundle = resolve_asset(config, "verifier_bundle", Path(config.get("_asset_root", "/harbor-task")))
    if directory_digest(bundle) != config["verifier_bundle_sha256"]:
        raise ValueError("External verifier bundle digest mismatch")
    checklist = bundle / "checklist.json"
    if not checklist.is_file():
        raise FileNotFoundError(checklist)
    return checklist


def apply_verifier_config(
    env, config: Dict[str, Any], extra_env: Optional[Dict[str, str]] = None
) -> None:
    """(Re)apply the task's verifier overrides (same shape the prime hub
    loader builds). ``extra_env`` overlays os.environ for the key lookup:
    Harbor delivers the grader key via ``[verifier.env]`` to the verifier
    phase's process, so the key may only become available at finalize time.
    """
    verifier = config.get("verifier") or {}
    if not verifier and not config.get("verifier_bundle"):
        return
    lookup = {**os.environ, **(extra_env or {})}
    overrides: Dict[str, str] = {}
    if verifier.get("mode"):
        overrides["GYM_ANYTHING_VERIFIER_MODE"] = verifier["mode"]
    if verifier.get("vlm_backend"):
        overrides["VLM_BACKEND"] = verifier["vlm_backend"]
    if verifier.get("vlm_model"):
        overrides["VLM_MODEL"] = verifier["vlm_model"]
    if verifier.get("vlm_base_url"):
        overrides["VLM_BASE_URL"] = verifier["vlm_base_url"]
    key_var = verifier.get("vlm_api_key_var")
    if key_var and lookup.get(key_var):
        overrides["VLM_API_KEY"] = lookup[key_var]
    if config.get("verifier_bundle"):
        checklist_path = _external_checklist(config)
        if json.loads(checklist_path.read_text()).get("schema_version") == "binary_outcome_checklist":
            env.task_spec.init.reward_type = "sparse"
        overrides["GYM_ANYTHING_VERIFIER_MODE"] = "vlm_checklist"
        overrides["GYM_ANYTHING_VLM_CHECKLIST_CHECKLIST"] = str(checklist_path)
        overrides["GYM_ANYTHING_VLM_CHECKLIST_FRAME_STRATEGY"] = "all"
    env.set_verifier_overrides(overrides)


def finalize_episode(env) -> Tuple[float, Optional[Dict[str, Any]]]:
    """Run the task's real grading pipeline (post_task hook + verifier.py)."""
    _obs, reward, _done, step_info = env.step([], mark_done=True)
    verifier = step_info.get("verifier")
    if verifier is None:
        verifier = _read_summary_verifier(env)
    return float(reward or 0.0), verifier


def rewards_from_verdict(
    reward: float, verifier: Optional[Dict[str, Any]]
) -> Dict[str, float]:
    """Map a gym-anything verdict onto Harbor's named-rewards dict."""
    rewards: Dict[str, float] = {"reward": float(reward or 0.0)}
    if isinstance(verifier, dict):
        if "passed" in verifier:
            rewards["passed"] = 1.0 if verifier.get("passed") else 0.0
        score = verifier.get("score")
        if isinstance(score, (int, float)):
            rewards["score"] = float(score)
    return rewards


def _read_summary_verifier(env) -> Optional[Dict[str, Any]]:
    episode_dir = env.episode_dir
    if not episode_dir:
        return None
    try:
        summary = json.loads((Path(episode_dir) / "summary.json").read_text())
    except (OSError, ValueError):
        return None
    return summary.get("verifier")


# -- serve mode ----------------------------------------------------------------


class _Runtime:
    """Owns the booted environment behind a lock; one trial per container."""

    def __init__(self, env, config: Optional[Dict[str, Any]] = None):
        self.env = env
        self.config = config or {}
        self.lock = threading.Lock()
        self._finalize_result: Optional[Dict[str, Any]] = None
        self.policy = None

    def policy_start(self, request: dict) -> dict:
        from .policy import ReferencePolicy
        with self.lock:
            if not self.config.get("portable_agent"):
                raise RuntimeError("This task does not enable the packaged reference policy")
            if self.policy is not None:
                raise RuntimeError("Policy has already been initialized")
            for key, value in (request.get("env") or {}).items():
                if key in {"GEMINI_API_KEY", "GOOGLE_API_KEY"} and value:
                    os.environ[key] = value
            self.policy = ReferencePolicy(self.env, model=request["model"],
                                          max_steps=request.get("max_steps", 50), logs_dir=Path("/logs/agent"))
        return {"status": "ready"}

    def policy_step(self) -> dict:
        with self.lock:
            if self.policy is None:
                raise RuntimeError("Policy has not been initialized")
            result = self.policy.step()
            result.update(_obs_payload(self.policy.obs))
            if result["done"]:
                self.policy.finish()
            return result

    def policy_finish(self, *, wait: bool = True) -> dict:
        if not self.lock.acquire(blocking=wait):
            raise RuntimeError("Policy request is still active during controller shutdown")
        try:
            if self.policy is not None:
                self.policy.finish()
        finally:
            self.lock.release()
        return {"status": "finished"}

    def record_error(self, stage: str, error: BaseException) -> None:
        root = Path(self.env.episode_dir).parent
        try:
            with (root / "errors.jsonl").open("a") as handle:
                handle.write(json.dumps({"stage": stage, "error": type(error).__name__,
                                         "message": str(error)}) + "\n")
        except OSError as exc:
            error.add_note(f"Could not persist controller error: {exc}")
            logger.exception("Could not persist controller error")

    def observe(self) -> Dict[str, Any]:
        with self.lock:
            obs = self.env.capture_observation()
        return _obs_payload(obs)

    def step(self, actions: list) -> Dict[str, Any]:
        with self.lock:
            obs, reward, done, info = self.env.step(actions or [])
        payload = _obs_payload(obs)
        payload.update(
            {
                "reward": float(reward or 0.0),
                "done": bool(done),
                "action_result": info.get("action_result"),
            }
        )
        return payload

    def finalize(self, extra_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        with self.lock:
            if self._finalize_result is None:
                if self.policy is not None:
                    self.policy.finish()
                # The grader key arrives with the verifier phase (Harbor's
                # [verifier.env]); re-resolve overrides before grading.
                apply_verifier_config(self.env, self.config, extra_env)
                reward, verifier = finalize_episode(self.env)
                self._finalize_result = {"reward": reward, "verifier": verifier}
                if self.config.get("deployment_profile") == "sandweave":
                    from .deployment import collect_sandweave_logs
                    collect_sandweave_logs(Path(self.env.episode_dir).parent)
        return self._finalize_result


def _obs_payload(obs: Dict[str, Any]) -> Dict[str, Any]:
    screen = (obs or {}).get("screen") or {}
    path = screen.get("path")
    return {
        # Absolute: callers (test.sh, the host-side driver) have a different
        # working directory than the serve process.
        "screenshot_path": str(Path(path).resolve()) if path else None,
        "resolution": list(screen.get("resolution") or []),
    }


class _Handler(BaseHTTPRequestHandler):
    runtime: _Runtime  # set by serve()

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        pass

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/health":
            self._send_json({"status": "ready"})
        else:
            self._send_json({"error": f"unknown path {self.path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        try:
            if self.path == "/observe":
                self._send_json(self.runtime.observe())
            elif self.path == "/step":
                request = self._read_json()
                self._send_json(self.runtime.step(request.get("actions") or []))
            elif self.path == "/finalize":
                request = self._read_json()
                self._send_json(self.runtime.finalize(request.get("env") or None))
            elif self.path == "/policy/start":
                self._send_json(self.runtime.policy_start(self._read_json()))
            elif self.path == "/policy/step":
                self._send_json(self.runtime.policy_step())
            elif self.path == "/policy/finish":
                self._send_json(self.runtime.policy_finish())
            else:
                self._send_json({"error": f"unknown path {self.path}"}, status=404)
        except Exception as exc:  # surface errors to the caller, keep serving
            self.runtime.record_error(self.path, exc)
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)


_AGENT_MANUAL = """# GUI control API

This container runs a desktop VM with the task's application open. You cannot
see the VM directly; drive it through the local HTTP API:

- `curl -s -X POST http://127.0.0.1:{port}/observe` returns
  `{{"screenshot_path": "...", "resolution": [width, height]}}`. Read the PNG
  at that path to see the current screen.
- `curl -s -X POST -H 'Content-Type: application/json' -d '{{"actions": [...]}}'
  http://127.0.0.1:{port}/step` executes actions and returns the new
  screenshot path. Coordinates are real pixels at the given resolution.

Action objects:

- `{{"mouse": {{"left_click": [x, y]}}}}` (also `double_click`, `right_click`,
  `move`)
- `{{"mouse": {{"scroll": -3}}}}` (negative scrolls down)
- `{{"keyboard": {{"text": "hello"}}}}` types text;
  `{{"keyboard": {{"keys": ["ctrl", "s"]}}}}` presses a chord
- `{{"action": "wait", "time": 2}}` waits

Observe after every step; the screen changes asynchronously. Complete the task
in the GUI; grading runs automatically afterwards against application state.
"""


def _write_agent_manual(port: int) -> None:
    """Give installed CLI agents (Claude Code, Codex, ...) the GUI-control
    affordance: they auto-read AGENTS.md/CLAUDE.md from their working
    directory and are multimodal, so with this manual they can drive the VM
    by reading screenshots and curling actions. Best-effort: root-owned
    paths do not exist outside the task container."""
    content = _AGENT_MANUAL.format(port=port)
    for path in ("/AGENTS.md", "/CLAUDE.md"):
        try:
            Path(path).write_text(content)
        except OSError:
            pass


def _close_runtime(runtime: _Runtime, failure: Optional[BaseException] = None) -> None:
    errors = []
    # A cancelled host request may leave an RPC thread in a provider call. Do
    # not block shutdown on that thread; completed responses are checkpointed.
    try:
        runtime.policy_finish(wait=False)
    except Exception as exc:
        errors.append(exc)
    try:
        runtime.env.close()
    except Exception as exc:
        errors.append(exc)
    if errors:
        primary = failure or errors[0]
        for error in errors:
            primary.add_note(f"Controller cleanup failed: {error}")
            logger.error("Controller cleanup failed: %s", error)
        if failure is None:
            raise primary


def _serve(config_path: str, port: int) -> None:
    config = load_task_config(config_path)
    if config.get("deployment_profile") == "sandweave":
        from .deployment import prepare_sandweave_worker
        prepare_sandweave_worker(config, Path("/logs/verifier/gym"))
    print(f"[harbor-container] booting {config.get('env_name')}/{config.get('task_id')}")
    env = boot_env(
        config,
        default_runner=os.environ.get("GA_HARBOR_RUNNER") or config.get("runner") or DEFAULT_CONTAINER_RUNNER,
        # Harbor downloads verifier logs AFTER verification, including late
        # final.png, summary.json and post_verification.png. Agent artifacts
        # may have been collected before those files existed.
        artifact_root=Path("/logs/verifier/gym"),
    )
    runtime = _Runtime(env, config)
    server = None
    failure = None
    try:
        if config.get("deployment_profile") == "sandweave":
            info = {"runner": type(env.runner).__name__, "environment_id": env.env_spec.id,
                    "task_id": env.task_spec.id, "instance_name": env.runner.get_runtime_info().instance_name}
            Path("/logs/verifier/gym/runtime-binding.json").write_text(json.dumps(info, indent=2) + "\n")
        _write_agent_manual(port)
        for path in ("/logs/agent", "/logs/verifier", "/logs/artifacts"):
            Path(path).mkdir(parents=True, exist_ok=True)
        _Handler.runtime = runtime
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        print(f"[harbor-container] ready on :{port}")
        server.serve_forever()
    except BaseException as exc:
        failure = exc
        runtime.record_error("serve", exc)
        raise
    finally:
        try:
            if server is not None:
                server.server_close()
        finally:
            _close_runtime(runtime, failure)


def serve(config_path: str, port: int) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(_signum, _frame):
        raise KeyboardInterrupt("Harbor controller received SIGTERM")
    signal.signal(signal.SIGTERM, terminate)
    try:
        _serve(config_path, port)
    finally:
        try:
            from .deployment import collect_sandweave_logs
            collect_sandweave_logs(Path("/logs/verifier/gym"))
        finally:
            signal.signal(signal.SIGTERM, previous)


# -- finalize mode (invoked by tests/test.sh inside the container) -------------


def finalize_main(port: int, reward_path: str, verifier_path: str) -> int:
    # Harbor resolves [verifier.env] into THIS process's environment; the
    # serve process booted earlier without it, so forward the credentials.
    payload = json.dumps(
        {"env": {k: v for k, v in os.environ.items() if k.endswith("_API_KEY")}}
    ).encode()
    request = Request(f"http://127.0.0.1:{port}/finalize", data=payload, method="POST")
    with urlopen(request, timeout=1800) as response:
        result = json.loads(response.read())
    if "error" in result:
        print(f"finalize failed: {result['error']}", file=sys.stderr)
        return 1

    verifier = result.get("verifier")
    rewards = rewards_from_verdict(result.get("reward", 0.0), verifier)
    Path(reward_path).parent.mkdir(parents=True, exist_ok=True)
    Path(reward_path).write_text(json.dumps(rewards) + "\n")
    if isinstance(verifier, dict):
        Path(verifier_path).write_text(json.dumps(verifier, default=str, indent=2) + "\n")
        feedback = verifier.get("feedback")
        if feedback:
            print(feedback)
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="gym_anything.integrations.harbor.container")
    sub = parser.add_subparsers(dest="mode", required=True)

    serve_parser = sub.add_parser("serve", help="boot the task environment and serve the API")
    serve_parser.add_argument(
        "--config", default=os.environ.get("GA_HARBOR_TASK_CONFIG", DEFAULT_TASK_CONFIG_PATH)
    )
    serve_parser.add_argument(
        "--port", type=int, default=int(os.environ.get("GA_HARBOR_PORT", DEFAULT_PORT))
    )

    finalize_parser = sub.add_parser("finalize", help="grade the episode and write reward.json")
    finalize_parser.add_argument(
        "--port", type=int, default=int(os.environ.get("GA_HARBOR_PORT", DEFAULT_PORT))
    )
    finalize_parser.add_argument("--reward-path", default=_REWARD_JSON_PATH)
    finalize_parser.add_argument("--verifier-path", default=_VERIFIER_JSON_PATH)

    args = parser.parse_args(argv)
    if args.mode == "serve":
        serve(args.config, args.port)
        return 0
    return finalize_main(args.port, args.reward_path, args.verifier_path)


if __name__ == "__main__":
    sys.exit(main())
