"""Run the existing reference policy against the controller's existing Gym env."""
from __future__ import annotations

import json
from pathlib import Path
import pickle
import time


class ReferencePolicy:
    def __init__(self, env, *, model: str, max_steps: int, logs_dir: Path):
        if not model or not isinstance(max_steps, int) or not 1 <= max_steps <= 1000:
            raise ValueError("Policy requires a model and max_steps between 1 and 1000")
        from agents.agents.claude_gemini import Gemini3Agent
        self.env = env
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.agent = Gemini3Agent(agent_args={
            "model": model.removeprefix("gemini/"), "exp_name": "harbor-portable",
            "task_name": env.task_spec.id.split("@", 1)[0],
        }, verbose=False, debug=False)
        self.obs = env.capture_observation()
        resolution = tuple(self.obs["screen"].get("resolution") or (1920, 1080))
        self.agent.init(env.task_spec.description, resolution, str(self.logs_dir))
        self.outputs = []
        self.steps = 0
        self.max_steps = max_steps
        self.done = False
        self.finished = False

    def step(self) -> dict:
        if self.done:
            raise RuntimeError("Policy is already done")
        start = time.perf_counter()
        groups = self.agent.step(self.obs, self.outputs)
        agent_ms = (time.perf_counter() - start) * 1000
        # Keep the provider response even if applying its GUI action fails.
        self._checkpoint()
        usage = self.agent.raw_responses[-1].usage
        usage = usage.model_dump(exclude_none=True) if usage is not None else {}
        with (self.logs_dir / "usage.jsonl").open("a") as handle:
            handle.write(json.dumps({"step": self.steps, **usage}) + "\n")
        self.outputs = []
        env_done = False
        env_start = time.perf_counter()
        for group in groups:
            self.obs, _reward, env_done, info = self.env.step(group["actions"])
            result = info.get("action_result") or {"action": "other", "output": "Executed the action"}
            self.outputs.append({**result, "tool_id": group["tool_id"], "obs": self.obs})
        self.steps += 1
        self.done = bool(self.agent.done or env_done or self.steps >= self.max_steps)
        timing = {"step": self.steps - 1, "agent_ms": agent_ms,
                  "environment_ms": (time.perf_counter() - env_start) * 1000}
        with (self.logs_dir / "timing.jsonl").open("a") as handle:
            handle.write(json.dumps(timing) + "\n")
        # Public assistant text only; private reasoning remains in raw_responses.pkl.
        text = self.agent.messages[-1]["content"]
        return {"groups": [{"tool_id": g["tool_id"], "actions": g["actions"]} for g in groups],
                "response_text": text, "done": self.done, "timing": timing, "usage": usage}

    def _checkpoint(self) -> None:
        # Checkpoint data without invoking the reference finalizer multiple times.
        for name, data in (("messages", self.agent.messages), ("raw_responses", self.agent.raw_responses)):
            temporary = self.logs_dir / f".{name}.pkl"
            with temporary.open("wb") as handle:
                pickle.dump(data, handle)
            temporary.replace(self.logs_dir / f"{name}.pkl")

    def finish(self) -> None:
        if not self.finished:
            self.finished = True
            self.agent.finish()
