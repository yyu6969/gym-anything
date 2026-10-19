"""Codex verification transport and checklist integration, with no model requests."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gym_anything import vlm
from gym_anything.specs import EnvSpec, TaskSpec
from gym_anything.verification.runner import VerifierRunner
from gym_anything.verification import vlm_checklist
from gym_anything.verification.vlm_checklist import (
    build_verification_prompt,
    compute_binary_scores,
    verification_output_schema,
)


CHECKLIST = {
    "schema_version": "binary_outcome_checklist",
    "scoring_policy": "aab_binary_outcome_v1",
    "items": [
        {"id": "website", "criterion": "Website is https://egg.tart.com.", "scoring": "binary"},
        {"id": "saved", "criterion": "Profile settings were saved.", "scoring": "binary"},
    ],
}


def verdict(*values):
    return {"items": [
        {"id": item["id"], "verdict": value, "evidence": "Visible in the screenshots."}
        for item, value in zip(CHECKLIST["items"], values)
    ], "overall_reasoning": "Judged only the supplied screenshots."}


@pytest.fixture
def codex(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("GYM_ANYTHING_VLM_CHECKLIST_", "VLM_")):
            monkeypatch.delenv(name)
    auth_home = tmp_path / "actor-codex-home"
    auth_home.mkdir()
    (auth_home / "auth.json").write_text('{"test_auth": "private-test-token"}')
    (auth_home / "config.toml").write_text("# actor-only configuration")
    (auth_home / "AGENTS.md").write_text("actor-only instructions")
    (auth_home / "sessions").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(auth_home))
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-api-secret")
    monkeypatch.setenv("CODEX_THREAD_ID", "actor-thread")
    monkeypatch.setattr(vlm.shutil, "which", lambda name: "/mock/bin/codex")
    run = Mock(side_effect=AssertionError("Configure a mocked Codex response first"))
    monkeypatch.setattr(vlm.subprocess, "run", run)
    for name in ("_query_local", "_query_openai_api", "_query_gemini", "_query_anthropic"):
        monkeypatch.setattr(vlm, name, Mock(side_effect=AssertionError("Unexpected API backend")))
    config = {"backend": "codex_cli", "model": "gpt-6.1-sol", "timeout": 23,
              "output_schema": verification_output_schema(CHECKLIST)}
    return SimpleNamespace(run=run, config=config, auth_home=auth_home)


def respond(codex, payload, *, returncode=0, stderr="", inspect=None):
    def execute(command, **kwargs):
        if inspect:
            inspect(command, kwargs)
        output = Path(command[command.index("--output-last-message") + 1])
        if payload is not None:
            output.write_text(payload, encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, stdout="ignored CLI events", stderr=stderr)

    codex.run.side_effect = execute


def test_fresh_session_authentication_prompt_and_read_only_evidence(codex, tmp_path):
    images = [tmp_path / "before.png", tmp_path / "after.png"]
    for index, image in enumerate(images):
        image.write_bytes(b"screenshot-" + str(index).encode())
    directories = []

    def inspect(command, kwargs):
        directories.append(kwargs["cwd"].parent)
        assert command[:4] == ["/mock/bin/codex", "--no-daemon", "exec", "--ephemeral"]
        assert "resume" not in command and "fork" not in command
        assert "--ignore-user-config" in command and "--ignore-rules" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[command.index("--model") + 1] == "gpt-6.1-sol"
        assert command[-1] == "-" and kwargs["input"] == "unchanged hidden rubric prompt"
        assert kwargs["timeout"] == 23 and kwargs["capture_output"] and not kwargs["check"]
        assert kwargs["text"]
        assert kwargs["cwd"].is_relative_to(directories[0])
        home = Path(kwargs["env"]["CODEX_HOME"])
        assert home != codex.auth_home
        assert (home / "auth.json").read_bytes() == (codex.auth_home / "auth.json").read_bytes()
        assert (home / "auth.json").stat().st_mode & 0o777 == 0o600
        assert not any((home / name).exists() for name in ("sessions", "config.toml", "AGENTS.md"))
        assert Path(kwargs["env"]["HOME"]) != Path.home()
        assert "OPENAI_API_KEY" not in kwargs["env"] and "CODEX_THREAD_ID" not in kwargs["env"]
        copied = [Path(command[i + 1]) for i, arg in enumerate(command) if arg == "--image"]
        assert [path.read_bytes() for path in copied] == [path.read_bytes() for path in images]
        assert all(path.parent == kwargs["cwd"] and path.stat().st_mode & 0o222 == 0 for path in copied)
        schema = Path(command[command.index("--output-schema") + 1])
        assert json.loads(schema.read_text()) == verification_output_schema(CHECKLIST)
        disabled = {command[i + 1] for i, arg in enumerate(command) if arg == "--disable"}
        assert {"shell_tool", "unified_exec", "plugins", "apps", "multi_agent", "view_image"} <= disabled

    response = json.dumps(verdict("PASS", "PASS"))
    respond(codex, response, inspect=inspect)
    result = vlm.query_vlm("unchanged hidden rubric prompt", image=str(images[0]),
                           images=[str(images[1])], config=codex.config)
    assert result == {"success": True, "response": response, "parsed": json.loads(response), "error": ""}
    assert not directories[0].exists()
    assert (codex.auth_home / "auth.json").read_text() == '{"test_auth": "private-test-token"}'
    assert [path.read_bytes() for path in images] == [b"screenshot-0", b"screenshot-1"]
    codex.run.assert_called_once()


@pytest.mark.parametrize("payload", [
    "PASS", "```json\n{}\n```", "{broken", "[]", "{}",
    json.dumps({"items": [], "overall_reasoning": True}),
    json.dumps(verdict("PASS", "partial")),
    json.dumps({**verdict("PASS", "PASS"), "unexpected": True}),
])
def test_malformed_or_wrong_shape_output_fails_closed(codex, payload):
    respond(codex, payload)
    result = vlm.query_vlm("rubric", config=codex.config)
    assert not result["success"] and result["parsed"] == {} and result["response"] == ""
    assert result["error"]


@pytest.mark.parametrize("failure", ["auth", "exit", "timeout", "missing", "encoding"])
def test_subprocess_failures_never_accept_a_passing_verdict(codex, failure):
    respond(codex, json.dumps(verdict("PASS", "PASS")))
    if failure == "auth":
        respond(codex, json.dumps(verdict("PASS", "PASS")), returncode=1,
                stderr="401 Unauthorized private-test-token unrelated-api-secret")
    elif failure == "exit":
        respond(codex, json.dumps(verdict("PASS", "PASS")), returncode=7)
    elif failure == "timeout":
        codex.run.side_effect = subprocess.TimeoutExpired("codex", 23, stderr="private-test-token")
    elif failure == "missing":
        respond(codex, None)
    elif failure == "encoding":
        def invalid_utf8(command, **kwargs):
            Path(command[command.index("--output-last-message") + 1]).write_bytes(b"\xff")
            return subprocess.CompletedProcess(command, 0)
        codex.run.side_effect = invalid_utf8
    result = vlm.query_vlm("rubric", config=codex.config)
    assert not result["success"] and result["parsed"] == {} and result["error"]
    assert "private-test-token" not in result["error"] and "unrelated-api-secret" not in result["error"]
    if failure == "auth":
        assert "authentication failed" in result["error"]


@pytest.mark.parametrize("failure", ["binary", "auth", "schema", "invalid_schema", "evidence", "timeout", "model"])
def test_configuration_and_input_failures_do_not_launch_codex(codex, monkeypatch, tmp_path, failure):
    images = []
    if failure == "binary":
        monkeypatch.setattr(vlm.shutil, "which", lambda name: None)
    elif failure == "auth":
        (codex.auth_home / "auth.json").unlink()
    elif failure == "schema":
        del codex.config["output_schema"]
    elif failure == "invalid_schema":
        codex.config["output_schema"] = {"type": "invalid"}
    elif failure == "evidence":
        images = [str(tmp_path / "missing.png")]
    elif failure == "timeout":
        codex.config["timeout"] = 0
    elif failure == "model":
        codex.config["model"] = None
    result = vlm.query_vlm("rubric", images=images, config=codex.config)
    assert not result["success"] and result["error"]
    codex.run.assert_not_called()


def test_unsupported_backend_fails_without_api_fallback(codex):
    result = vlm.query_vlm("rubric", config={"backend": "typo", "model": "some-model"})
    assert not result["success"] and result["error"] == "unsupported VLM backend: typo"
    codex.run.assert_not_called()


@pytest.mark.parametrize("values,score,passed", [
    (("PASS", "PASS"), 100, True), (("PASS", "FAIL"), 50, False),
    (("FAIL", "FAIL"), 0, False), (("PASS",), 50, False),
])
def test_saved_episode_runner_reuses_prompt_evidence_parsing_and_binary_scores(
    codex, tmp_path, monkeypatch, values, score, passed,
):
    episode = tmp_path / "episode"
    episode.mkdir()
    frames = [episode / f"frame_{index:05d}.png" for index in range(4)] + [episode / "final.png"]
    for frame in frames:
        frame.write_bytes(frame.name.encode())
    rubric = tmp_path / "checklist.json"
    rubric.write_text(json.dumps(CHECKLIST))
    original_rubric = rubric.read_bytes()
    monkeypatch.setenv("GYM_ANYTHING_VLM_CHECKLIST_BACKEND", "codex_cli")
    monkeypatch.setenv("GYM_ANYTHING_VLM_CHECKLIST_MODEL", "gpt-6.1-sol")
    env_spec = EnvSpec.from_dict({"id": "gitlab"})
    task_spec = TaskSpec.from_dict({
        "id": "website", "description": "Update my profile's website URL to https://egg.tart.com.",
        "success": {"mode": "program", "spec": {}},
    })
    response = verdict(*values)

    def inspect(command, kwargs):
        assert kwargs["input"] == build_verification_prompt(
            checklist=CHECKLIST, task_description=task_spec.description, image_count=len(frames),
        )
        copied = [Path(command[i + 1]) for i, arg in enumerate(command) if arg == "--image"]
        assert [path.read_bytes() for path in copied] == [path.read_bytes() for path in frames]

    respond(codex, json.dumps(response), inspect=inspect)
    result = VerifierRunner().evaluate(
        runner=None, env_spec=env_spec, task_spec=task_spec, episode_dir=episode,
        env_root=tmp_path, task_root=tmp_path, verifier_env={
            "GYM_ANYTHING_VERIFIER_MODE": "vlm_checklist",
            "GYM_ANYTHING_VLM_CHECKLIST_CHECKLIST": str(rubric),
            "GYM_ANYTHING_VLM_CHECKLIST_FRAME_STRATEGY": "all",
            "VLM_BACKEND": "local", "VLM_MODEL": "gemini-export-default",
        },
    )
    assert result["mode"] == "vlm_checklist" and result["mode_source"] == "env"
    assert result["config"]["backend"] == "codex_cli" and result["config"]["model"] == "gpt-6.1-sol"
    assert result["image_count"] == len(frames)
    assert result["verdicts"] == response
    assert result["scores"] == compute_binary_scores(response, CHECKLIST)
    assert result["score"] == score and result["passed"] is passed
    assert rubric.read_bytes() == original_rubric


def test_runner_transport_failure_is_a_failed_grade(codex, tmp_path):
    (tmp_path / "frame_00000.png").write_bytes(b"evidence")
    (tmp_path / "checklist.json").write_text(json.dumps(CHECKLIST))
    codex.run.side_effect = subprocess.TimeoutExpired("codex", 23)
    result = VerifierRunner().evaluate(
        runner=None, env_spec=EnvSpec.from_dict({"id": "test"}),
        task_spec=TaskSpec.from_dict({"id": "test", "success": {"mode": "vlm_checklist", "spec": {
            "backend": "codex_cli", "checklist": "checklist.json",
        }}}), episode_dir=tmp_path, env_root=tmp_path, task_root=tmp_path,
    )
    assert result["decided"] and not result["passed"] and result["score"] == 0
    assert "timed out" in result["error"]


@pytest.mark.parametrize("checklist,response", [
    ({"task_completion": [], "integrity": []}, {
        "task_completion": [{"id": "done", "verdict": "partial", "confidence": 0.8, "evidence": "visible"}],
        "integrity": [], "overall_reasoning": "Some progress",
    }),
    ({"scoring_policy": "reference_evidence"}, {"items": [
        {"id": "done", "status": "UNOBSERVABLE", "evidence_refs": [], "reasoning": "No proof"},
    ]}),
])
def test_other_existing_checklist_verdict_formats(codex, checklist, response):
    codex.config["output_schema"] = verification_output_schema(checklist)
    respond(codex, json.dumps(response))
    result = vlm.query_vlm("existing prompt", config=codex.config)
    assert result["success"] and result["parsed"] == response


def test_gemini_dispatch_and_options_are_preserved(codex, monkeypatch):
    query = Mock(return_value={"success": True, "response": "{}", "parsed": {}, "error": ""})
    monkeypatch.setattr(vlm, "_query_gemini", query)
    config = {"backend": "gemini", "model": "gemini-3-flash-preview", "max_retries": 3, "api_key": "test"}
    result = vlm.query_vlm("existing Gemini prompt", config=config, max_tokens=900, temperature=0.2, top_p=0.9)
    assert result["success"]
    query.assert_called_once_with("existing Gemini prompt", [], config, 900, 0.2, 0.9)
    codex.run.assert_not_called()


def test_gemini_transport_still_parses_verdicts_and_passes_provider_options(monkeypatch):
    response = json.dumps(verdict("PASS", "FAIL"))
    completion = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=response),
    )]))
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    result = vlm.query_vlm("Gemini rubric", config={
        "backend": "gemini", "model": "gemini-3-flash-preview", "api_key": "test-only",
        "max_retries": 3, "timeout": 37,
    }, max_tokens=900, temperature=0.2, top_p=0.9)
    assert result == {"success": True, "response": response, "parsed": json.loads(response), "error": ""}
    completion.assert_called_once_with(
        model="gemini/gemini-3-flash-preview",
        messages=vlm._build_openai_messages("Gemini rubric", []),
        max_tokens=900, temperature=0.2, top_p=0.9, timeout=37, api_key="test-only",
    )


def test_reference_policy_default_transport_receives_its_schema(codex, tmp_path, monkeypatch):
    checklist = {"scoring_policy": "reference_evidence", "items": [
        {"id": "done", "criterion": "Saved website is correct", "evidence_scope": "terminal"},
    ]}
    image = tmp_path / "final.png"
    image.write_bytes(b"final evidence")
    evidence = [{"id": "final", "kind": "image", "path": str(image),
                 "availability": "available", "scope": "terminal"}]
    response = {"items": [{"id": "done", "status": "PASS", "evidence_refs": ["final"], "reasoning": "Visible"}]}
    respond(codex, json.dumps(response))
    # Keep this regression synchronous; only the production API's thread handoff is mocked.
    thread = AsyncMock(side_effect=lambda function, **kwargs: function(**kwargs))
    monkeypatch.setattr(vlm_checklist.asyncio, "to_thread", thread)
    config = {"backend": "codex_cli", "model": "gpt-6.1-sol"}
    result = asyncio.run(vlm_checklist.evaluate_reference_checklist(
        checklist=checklist, evidence=evidence, task_description="Update website", config=config,
    ))
    assert result["items"][0]["status"] == "PASS" and result["items"][0]["score"] == 1
    assert thread.call_args.kwargs["config"]["output_schema"] == verification_output_schema(checklist)
    assert "output_schema" not in config


def test_non_finite_json_constants_fail_closed(codex):
    codex.config["output_schema"] = verification_output_schema({})
    respond(codex, '{"task_completion": [{"id": "done", "verdict": "pass", '
                   '"confidence": NaN, "evidence": "visible"}], "integrity": [], "overall_reasoning": "done"}')
    result = vlm.query_vlm("rubric", config=codex.config)
    assert not result["success"] and result["parsed"] == {}


def test_repeated_verification_calls_never_reuse_session_state(codex):
    homes = []

    def inspect(command, kwargs):
        home = Path(kwargs["env"]["CODEX_HOME"])
        assert not (home / "sessions").exists()
        homes.append(home)
        (home / "sessions").mkdir()

    respond(codex, json.dumps(verdict("PASS", "PASS")), inspect=inspect)
    for _ in range(2):
        assert vlm.query_vlm("rubric", config=codex.config)["success"]
    assert homes[0] != homes[1] and all(not home.exists() for home in homes)
