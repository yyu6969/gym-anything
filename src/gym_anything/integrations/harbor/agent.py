"""Harbor agent that runs a gym-anything reference agent loop.

Harbor's built-in computer-use agent (computer-1) provisions its own desktop
(Xvfb + XFCE + Chromium) inside the environment, which collides with benchmark
guests that already run a full desktop with the target application open. This
agent drives the guest through the gym-anything observation/action surface
instead, running a reference agent from ``agents/agents`` verbatim — the same
loop the local evaluation harness and the prime-rl adapter use.

It works on two environment shapes, auto-detected:

* ``GymAnythingEnvironment`` (the custom backend): drives the booted
  ``GymAnythingEnv`` directly through ``environment.gym_env``.
* Docker-shaped tasks on any standard backend: drives the in-container
  runtime API (see ``container``) through ``environment.exec`` + curl,
  downloading each screenshot to the host.

Usage::

    harbor run --path <compiled-task> \\
        --agent-import-path gym_anything.integrations.harbor:CuaWorldAgent \\
        --model gemini-3.5-flash \\
        [--env gym_anything.integrations.harbor:GymAnythingEnvironment]

``--model`` is passed to the reference agent verbatim (no provider-prefix
rewriting), so use the exact name the reference agent expects. The reference
agent class defaults to ``GeminiQwen3Agent`` and can be changed with
``--agent-kwarg reference_agent=Qwen3VLAgent``.

The agent records an ATIF trajectory (``trajectory.json`` in its logs dir),
validated against Harbor's trajectory models, so runs replay in the viewer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from .portable_agent import _TrajectoryRecorder

_CONTAINER_PORT = 7317
logger = logging.getLogger(__name__)


# -- environment drivers --------------------------------------------------------


class _DirectDriver:
    """Drives the GymAnythingEnv exposed by the gym-anything backend."""

    def __init__(self, gym_env: Any, images_dir: Path):
        self._env = gym_env
        self._images_dir = images_dir
        self._counter = 0

    async def observe(self) -> dict:
        obs = await asyncio.to_thread(self._env.capture_observation)
        return self._localize(obs)

    async def step(self, actions: list) -> tuple[dict, bool, Any]:
        obs, _reward, done, info = await asyncio.to_thread(self._env.step, actions or [])
        return self._localize(obs), bool(done), info.get("action_result")

    def _localize(self, obs: dict) -> dict:
        screen = (obs or {}).get("screen") or {}
        source = screen.get("path")
        local = self._next_image_path()
        if source:
            shutil.copy2(source, local)
        return _obs_dict(str(local), screen.get("resolution"))

    def _next_image_path(self) -> Path:
        self._counter += 1
        self._images_dir.mkdir(parents=True, exist_ok=True)
        return self._images_dir / f"obs_{self._counter:04d}.png"


class _ContainerDriver:
    """Drives the in-container runtime API over ``environment.exec`` + curl."""

    def __init__(self, environment: BaseEnvironment, images_dir: Path, port: int):
        self._environment = environment
        self._images_dir = images_dir
        self._port = port
        self._counter = 0

    async def observe(self) -> dict:
        payload = await self._request("observe", {})
        return await self._localize(payload)

    async def step(self, actions: list) -> tuple[dict, bool, Any]:
        payload = await self._request("step", {"actions": actions or []})
        obs = await self._localize(payload)
        return obs, bool(payload.get("done")), payload.get("action_result")

    async def _request(self, endpoint: str, body: dict) -> dict:
        encoded = base64.b64encode(json.dumps(body).encode()).decode()
        command = (
            f"echo {encoded} | base64 -d | "
            f"curl -fsS -X POST --data-binary @- -H 'Content-Type: application/json' "
            f"http://127.0.0.1:{self._port}/{endpoint}"
        )
        result = await self._environment.exec(command, timeout_sec=600)
        if result.return_code != 0:
            raise RuntimeError(
                f"container runtime request /{endpoint} failed "
                f"(rc={result.return_code}): {result.stderr or result.stdout}"
            )
        payload = json.loads(result.stdout or "{}")
        if "error" in payload:
            raise RuntimeError(f"container runtime /{endpoint}: {payload['error']}")
        return payload

    async def _localize(self, payload: dict) -> dict:
        container_path = payload.get("screenshot_path")
        self._counter += 1
        self._images_dir.mkdir(parents=True, exist_ok=True)
        local = self._images_dir / f"obs_{self._counter:04d}.png"
        if container_path:
            await self._environment.download_file(container_path, local)
        return _obs_dict(str(local), payload.get("resolution"))


def _obs_dict(path: str, resolution: Any) -> dict:
    return {"screen": {"path": path, "resolution": tuple(resolution or (1920, 1080))}}


def _make_driver(environment: Any, images_dir: Path, port: int):
    gym_env = getattr(environment, "gym_env", None)
    if gym_env is not None:
        return _DirectDriver(gym_env, images_dir)
    if hasattr(environment, "exec"):
        return _ContainerDriver(environment, images_dir, port)
    raise RuntimeError(
        "CuaWorldAgent needs either the gym-anything backend "
        "(GymAnythingEnvironment) or a docker-shaped task running the "
        "gym-anything container runtime."
    )


# -- the agent --------------------------------------------------------------------


class CuaWorldAgent(BaseAgent):
    """Runs a gym-anything reference agent against a gym-anything trial env."""

    SUPPORTS_ATIF = True

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *args: Any,
        reference_agent: str = "GeminiQwen3Agent",
        max_steps: int = 15,
        temperature: float = 1.0,
        container_port: int = _CONTAINER_PORT,
        **kwargs: Any,
    ):
        super().__init__(logs_dir, model_name, *args, **kwargs)
        self._reference_agent_name = reference_agent
        self._max_steps = int(max_steps)
        self._temperature = float(temperature)
        self._container_port = int(container_port)

    @staticmethod
    def name() -> str:
        return "cua-world-agent"

    def version(self) -> str | None:
        return "0.2.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        # The benchmark's own hooks provision the desktop and application.
        return None

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        images_dir = Path(self.logs_dir) / "images"
        driver = _make_driver(environment, images_dir, self._container_port)
        agent = self._make_reference_agent(environment)
        recorder = _TrajectoryRecorder(
            Path(self.logs_dir), self.name(), self.version() or "unknown", self.model_name
        )

        initialized = False
        failure = None
        try:
            obs = await driver.observe()
            resolution = tuple(obs["screen"]["resolution"])
            agent.init(
                task_description=instruction,
                display_resolution=resolution,
                save_path=str(self.logs_dir),
            )
            initialized = True

            action_outputs: list[dict[str, Any]] = []
            for _ in range(self._max_steps):
                started = time.perf_counter()
                action_groups = await asyncio.to_thread(agent.step, obs, action_outputs)
                agent_ms = (time.perf_counter() - started) * 1000
                env_started = time.perf_counter()
                action_outputs = []
                done = False
                for group in action_groups or []:
                    obs, done, result = await driver.step(group.get("actions") or [])
                    action_result = result or {
                        "action": "other",
                        "output": "Executed the action",
                    }
                    action_outputs.append({**action_result, "tool_id": group.get("tool_id")})
                recorder.record_turn(
                    response_text=self._last_response(agent, action_groups),
                    groups=action_groups or [],
                    screenshot_path=Path(obs["screen"]["path"]),
                    usage=(agent.raw_responses[-1].usage.model_dump(exclude_none=True)
                           if getattr(agent, "raw_responses", None) and agent.raw_responses[-1].usage else None),
                )
                with (Path(self.logs_dir) / "timing.jsonl").open("a") as handle:
                    handle.write(json.dumps({"agent_ms": agent_ms,
                                             "environment_ms": (time.perf_counter() - env_started) * 1000}) + "\n")
                if getattr(agent, "done", False) or done:
                    break
        except BaseException as exc:
            failure = exc
            (Path(self.logs_dir) / "failure.json").write_text(
                json.dumps({"error": type(exc).__name__, "message": str(exc)}) + "\n")
            raise
        finally:
            try:
                if initialized:
                    try:
                        await asyncio.to_thread(agent.finish)
                    except Exception as exc:
                        if failure is None:
                            raise
                        failure.add_note(f"Reference agent cleanup failed: {exc}")
                        logger.exception("Reference agent cleanup failed")
            finally:
                recorder.write(self.session_id)

    @staticmethod
    def _last_response(agent: Any, action_groups: list) -> str:
        raw = getattr(agent, "raw_responses", None)
        if raw:
            return raw[-1].choices[0].message.content or ""
        responses = getattr(agent, "all_model_responses", None)
        if responses:
            return str(responses[-1])
        for group in action_groups or []:
            conclusion = (group.get("metadata") or {}).get("conclusion")
            if conclusion:
                return str(conclusion)
        return ""

    def _make_reference_agent(self, environment: Any):
        import agents.agents as registry

        cls = getattr(registry, self._reference_agent_name, None)
        if cls is None:
            available = ", ".join(
                n for n in dir(registry) if n.endswith("Agent") and not n.startswith("_")
            )
            raise ValueError(
                f"Unknown reference agent {self._reference_agent_name!r}; "
                f"available: {available}"
            )
        task_name = getattr(environment, "environment_name", None) or "task"
        agent_args = {
            "model": self.model_name or "",
            "exp_name": "harbor",
            "task_name": str(task_name),
            "temperature": self._temperature,
        }
        return cls(agent_args=agent_args, verbose=False, debug=False)


__all__ = ["CuaWorldAgent"]
