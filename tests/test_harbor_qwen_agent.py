"""Authenticated Qwen GUI requests through the existing Harbor adapter, offline."""

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

pytest.importorskip("harbor")

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from agents.shared import llm_clients
from gym_anything.integrations.harbor.agent import CuaWorldAgent
from gym_anything.integrations.harbor.environment import GymAnythingEnvironment


@pytest.mark.parametrize("model", ["qwen3.5-plus", "Qwen/Qwen3.5-122B-A10B"])
def test_qwen35_harbor_gui_loop_propagates_connection_and_model(tmp_path, monkeypatch, model):
    endpoint = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    monkeypatch.setenv("VLM_BASE_URL", endpoint)
    monkeypatch.setenv("VLM_API_KEY", "test-dashscope-key")
    monkeypatch.setenv("VLM_MODEL", "unrelated-global-model")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-provider-key")
    monkeypatch.delenv("VLM_DISABLE_THINKING", raising=False)
    monkeypatch.setenv("GYM_ANYTHING_OUTPUT_DIR", str(tmp_path / "runtime"))

    screen = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(screen)
    obs = {"screen": {"path": str(screen), "resolution": (100, 100)}}
    gym_env = SimpleNamespace(
        capture_observation=Mock(return_value=obs),
        step=Mock(return_value=(obs, 0.0, False, {"action_result": {"action": "ok", "output": "done"}})),
    )
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    (environment_dir / "gym-anything.json").write_text(json.dumps({
        "runner": "sandweave", "deployment_profile": "sandweave", "task_id": "website",
    }))
    environment = GymAnythingEnvironment(
        environment_dir=environment_dir, environment_name="qwen-gui-test", session_id="qwen-gui-test",
        trial_paths=TrialPaths(tmp_path / "trial"), task_env_config=EnvironmentConfig(),
    )
    environment._env = gym_env  # No guest, environment startup or verifier.
    calls = [
        '<tool_call>{"action":"left_click","coordinate":[500,500]}</tool_call>',
        '<tool_call>{"action":"terminate","status":"success"}</tool_call>',
    ]
    create = Mock(side_effect=[SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content),
    )]) for content in calls])
    constructor = Mock(return_value=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm_clients.openai, "OpenAI", constructor)
    agent = CuaWorldAgent(
        logs_dir=tmp_path / "agent", model_name=model, reference_agent="Qwen35VLAgent", max_steps=3,
    )
    # Avoid real thread scheduling; retain the actual driver, agent and GUI loop.
    async def in_process(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", in_process)
    instruction = "Update the profile website through the GUI."
    asyncio.run(agent.run(instruction, environment, SimpleNamespace()))

    assert constructor.call_count == 2
    for call in constructor.call_args_list:
        assert call.kwargs == {"base_url": endpoint, "api_key": "test-dashscope-key"}
    assert create.call_count == 2
    for call in create.call_args_list:
        assert call.kwargs["model"] == model
        parts = [part for message in call.kwargs["messages"] for part in message["content"]]
        assert any(instruction in part.get("text", "") for part in parts)
        images = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
        assert images
        image = Image.open(io.BytesIO(base64.b64decode(images[-1].split(",", 1)[1])))
        assert image.getpixel((0, 0)) == (255, 255, 255)
    gym_env.capture_observation.assert_called_once()
    assert gym_env.step.call_args_list[0].args == ([{"mouse": {"left_click": [50, 50]}}],)
    assert gym_env.step.call_args_list[1].args == ([],)
    assert gym_env.step.call_count == 2
    trajectory = json.loads((tmp_path / "agent/trajectory.json").read_text())
    assert trajectory["agent"]["model_name"] == model
    assert (tmp_path / "agent/responses.json").exists()
