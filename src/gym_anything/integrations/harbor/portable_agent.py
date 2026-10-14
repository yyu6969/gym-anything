"""Self-contained Harbor client; the Gym policy and runner stay in the task image.

This module is copied into exported tasks and must import neither Gym nor AAB.
"""
from __future__ import annotations
import base64
import json
import logging
from pathlib import Path
import shlex
from typing import Optional

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.models.trajectories import (
    Agent as AtifAgent, ContentPart, FinalMetrics, ImageSource, Observation,
    ObservationResult, Step, ToolCall, Trajectory, Metrics,
)

logger = logging.getLogger(__name__)

class _TrajectoryRecorder:
    def __init__(self, logs_dir: Path, agent_name: str, version: str, model_name: str | None):
        self._logs_dir = logs_dir
        self._agent = AtifAgent(name=agent_name, version=version, model_name=model_name)
        self._steps: list[Step] = []

    def record_turn(
        self,
        response_text: str,
        groups: list,
        screenshot_path: Optional[Path],
        usage: Optional[dict] = None,
    ) -> None:
        step_id = len(self._steps) + 1
        tool_calls = [
            ToolCall(
                tool_call_id=f"step{step_id}-{idx}",
                function_name="computer_use",
                arguments={"actions": group.get("actions") or []},
            )
            for idx, group in enumerate(groups or [])
        ]
        observation = None
        if screenshot_path is not None:
            relative = screenshot_path.relative_to(self._logs_dir)
            observation = Observation(
                results=[
                    ObservationResult(
                        source_call_id=tool_calls[-1].tool_call_id if tool_calls else None,
                        content=[
                            ContentPart(
                                type="image",
                                source=ImageSource(
                                    media_type="image/png", path=str(relative)
                                ),
                            )
                        ],
                    )
                ]
            )
        self._steps.append(
            Step(
                step_id=step_id,
                source="agent",
                message=response_text,
                tool_calls=tool_calls or None,
                observation=observation,
                metrics=Metrics(prompt_tokens=usage.get("prompt_tokens"),
                                completion_tokens=usage.get("completion_tokens")) if usage else None,
            )
        )

    def write(self, session_id: str | None) -> None:
        if not self._steps:
            # ATIF requires a nonempty document. Record the real failed-turn
            # boundary rather than masking the original exception at teardown.
            self._steps.append(Step(step_id=1, source="system",
                                    message="Agent execution ended before its first completed model turn."))
        trajectory = Trajectory(
            schema_version="ATIF-v1.7",
            session_id=session_id,
            trajectory_id=f"cua-world-{session_id}" if session_id else None,
            agent=self._agent,
            steps=self._steps,
            final_metrics=FinalMetrics(
                total_steps=len(self._steps),
                total_prompt_tokens=sum(s.metrics.prompt_tokens or 0 for s in self._steps if s.metrics) or None,
                total_completion_tokens=sum(s.metrics.completion_tokens or 0 for s in self._steps if s.metrics) or None,
            ),
        )
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        (self._logs_dir / "trajectory.json").write_text(
            trajectory.model_dump_json(indent=2, exclude_none=True) + "\n"
        )


class GymControllerAgent(BaseAgent):
    """Drive the packaged reference policy over the existing controller API."""
    capabilities = AgentCapabilities(atif=True)

    def __init__(self, logs_dir, model_name=None, *args, max_steps=50, container_port=7317, **kwargs):
        super().__init__(logs_dir, model_name, *args, **kwargs)
        self.max_steps = int(max_steps)
        self.port = int(container_port)
        if not 1 <= self.max_steps <= 1000 or not 1 <= self.port <= 65535:
            raise ValueError("Invalid policy step budget or controller port")

    @staticmethod
    def name():
        return "gym-controller-agent"

    def version(self):
        return "1.0.0"

    async def setup(self, environment):
        # The packaged Gym hooks already own desktop and browser setup.
        return None

    async def _request(self, environment, endpoint, body=None, *, credentials=False):
        encoded = base64.b64encode(json.dumps(body or {}).encode()).decode()
        program = (
            "import base64,json,os,urllib.request,urllib.error; "
            f"body=json.loads(base64.b64decode({encoded!r})); "
        )
        extra = {}
        if credentials:
            extra = {key: self._get_env(key) for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY")
                     if self._get_env(key)}
            if not extra:
                raise RuntimeError("Set GEMINI_API_KEY or GOOGLE_API_KEY for the packaged Gemini policy")
            # Credential values are delivered through exec's environment, never
            # embedded in a shell command or written into the exported package.
            program += "body['env']={k:os.environ[k] for k in ('GEMINI_API_KEY','GOOGLE_API_KEY') if os.environ.get(k)}; "
        program += (
            f"request=urllib.request.Request('http://127.0.0.1:{self.port}/{endpoint}', "
            "data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}); "
            "\ntry:\n print(urllib.request.urlopen(request,timeout=1800).read().decode())"
            "\nexcept urllib.error.HTTPError as error:\n print(error.read().decode()); raise SystemExit(1)"
        )
        result = await environment.exec('python -c ' + shlex.quote(program), env=extra or None, timeout_sec=1800)
        if result.return_code:
            raise RuntimeError(f"Controller /{endpoint} failed: {result.stderr or result.stdout}")
        response = json.loads(result.stdout or '{}')
        if response.get('error'):
            raise RuntimeError(response['error'])
        return response

    async def run(self, instruction, environment, context):
        logs = Path(self.logs_dir)
        images = logs / 'images'
        images.mkdir(parents=True, exist_ok=True)
        recorder = _TrajectoryRecorder(logs, self.name(), self.version(), self.model_name)
        started = False
        failure = None
        try:
            await self._request(environment, 'policy/start',
                                {'model': self.model_name, 'max_steps': self.max_steps}, credentials=True)
            started = True
            for index in range(self.max_steps):
                result = await self._request(environment, 'policy/step')
                screenshot = images / f'obs_{index + 1:04d}.png'
                await environment.download_file(result['screenshot_path'], screenshot)
                recorder.record_turn(response_text=result['response_text'], groups=result['groups'],
                                     screenshot_path=screenshot, usage=result.get('usage'))
                if result['done']:
                    break
        except BaseException as exc:
            failure = exc
            (logs / 'failure.json').write_text(json.dumps({'error': type(exc).__name__, 'message': str(exc)}) + '\n')
            raise
        finally:
            try:
                if started:
                    try:
                        await self._request(environment, 'policy/finish')
                    except Exception as exc:
                        if failure is None:
                            raise
                        failure.add_note(f'Controller policy cleanup failed: {exc}')
                        logger.exception('Controller policy cleanup failed')
            finally:
                recorder.write(self.session_id)
