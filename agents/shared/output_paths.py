"""Agent output paths derived from Gym-Anything's canonical runtime root."""

from __future__ import annotations

from gym_anything.runtime_paths import runtime_paths


def agent_run_base(experiment: str, model: str, task: str) -> str:
    return str(runtime_paths().all_runs / experiment / model / task)
