"""Policy, agent lifecycle and late evidence collection using offline models."""
import asyncio
import json
import os
from pathlib import Path
import pickle
import shutil
import shlex
import subprocess
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
from PIL import Image
from litellm.types.utils import ModelResponse

from gym_anything.integrations.harbor.agent import CuaWorldAgent
from gym_anything.integrations.harbor.container import _Runtime
from gym_anything.integrations.harbor.policy import ReferencePolicy
from gym_anything.integrations.harbor.portable_agent import GymControllerAgent


CLICK = '<tool_call>{"action":"click","coordinate":[650,305]}</tool_call>'
TYPE = '<tool_call>{"action":"type","text":"dotfiles"}</tool_call>'
TERMINATE = '<tool_call>{"action":"terminate","status":"success"}</tool_call>'


def response(content):
    return ModelResponse(model='gemini/gemini-3.8-flash', choices=[{
        'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content},
    }], usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('GYM_ANYTHING_OUTPUT_DIR', str(tmp_path / 'runtime'))
    screen = tmp_path / 'screen.png'
    Image.new('RGB', (64, 64), 'white').save(screen)
    episode = tmp_path / 'controller/logs/verifier/gym/episode'
    episode.mkdir(parents=True)
    actions = []
    obs = {'screen': {'path': str(screen), 'resolution': (1920, 1080)}}

    def step(actual, mark_done=False):
        actions.append((actual, mark_done))
        if mark_done:
            (episode / 'summary.json').write_text(json.dumps({'verifier': {'passed': False, 'score': 0}}))
            for name in ('final.png', 'post_verification.png'):
                shutil.copy2(screen, episode / name)
            return obs, 0.0, True, {'verifier': {'passed': False, 'score': 0}}
        return obs, 0.0, False, {'action_result': {'action': 'other', 'output': 'Executed the action'}}

    return SimpleNamespace(
        capture_observation=Mock(return_value=obs), step=Mock(side_effect=step),
        task_spec=SimpleNamespace(id='add_license@1', description='Add a license.'),
        episode_dir=episode, set_verifier_overrides=Mock(), actions=actions,
    )


def test_packaged_gemini_policy_keeps_history_parser_feedback_and_finalizes_once(world, tmp_path, monkeypatch):
    requests = []
    raw = [response(CLICK + TYPE + TERMINATE), response(TERMINATE)]
    def model(messages, *args, **kwargs):
        import copy
        requests.append(copy.deepcopy(messages))
        return raw[len(requests) - 1]
    monkeypatch.setattr('agents.agents.claude_gemini.call_gemini_with_retry', model)
    policy = ReferencePolicy(world, model='gemini-3.8-flash', max_steps=50, logs_dir=tmp_path / 'agent')
    finish = Mock(wraps=policy.agent.finish)
    policy.agent.finish = finish
    runtime = _Runtime(world)
    runtime.policy = policy
    first = runtime.policy_step()
    assert not first['done']
    assert first['groups'][0]['actions'] == [{'mouse': {'left_click': [1248, 329]}}]
    assert (tmp_path / 'agent/raw_responses.pkl').is_file()
    second = runtime.policy_step()
    assert second['done'] and second['groups'] == []
    assert world.actions == [([{'mouse': {'left_click': [1248, 329]}}], False)]
    assistant = [m['content'] for m in requests[1] if m['role'] == 'assistant']
    assert assistant == [raw[0].choices[0].message.content]
    feedback = '\n'.join(p.get('text', '') for p in requests[1][-1]['content'])
    assert 'ignored 2' in feedback and 'skipped tool calls were not executed' in feedback
    assert 'submitted_actions' in feedback
    runtime.policy_finish()
    runtime.finalize()
    runtime.finalize()
    finish.assert_called_once()
    assert world.actions[-1] == ([], True)
    assert world.step.call_count == 2
    assert (world.episode_dir / 'summary.json').is_file()
    with (tmp_path / 'agent/messages.pkl').open('rb') as handle:
        assert all(isinstance(m['content'], str) for m in pickle.load(handle) if m['role'] == 'assistant')
    with (tmp_path / 'agent/raw_responses.pkl').open('rb') as handle:
        saved = pickle.load(handle)
    assert len(saved) == 2 and saved[0].model_dump() == raw[0].model_dump()
    assert len((tmp_path / 'agent/timing.jsonl').read_text().splitlines()) == 2


@pytest.mark.parametrize('fail', [False, True])
def test_custom_agent_calls_finish_once_even_when_policy_raises(world, tmp_path, monkeypatch, fail):
    agent = CuaWorldAgent(logs_dir=tmp_path / 'host-agent', model_name='offline', max_steps=2)
    reference = SimpleNamespace(
        init=Mock(), finish=Mock(), done=True,
        step=Mock(side_effect=RuntimeError('model request failed') if fail else None, return_value=[]),
    )
    monkeypatch.setattr(agent, '_make_reference_agent', lambda environment: reference)
    environment = SimpleNamespace(gym_env=world)
    if fail:
        with pytest.raises(RuntimeError, match='model request failed'):
            asyncio.run(agent.run('Add a license', environment, SimpleNamespace()))
    else:
        asyncio.run(agent.run('Add a license', environment, SimpleNamespace()))
    reference.finish.assert_called_once()
    assert world.step.call_count == 0
    assert (tmp_path / 'host-agent/trajectory.json').is_file()


def test_portable_bridge_requires_no_gym_import_and_does_not_provision_browser(world, tmp_path, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'offline-test-credential')
    agent = GymControllerAgent(logs_dir=tmp_path / 'portable', model_name='gemini-3.8-flash')
    requests = []
    async def exec_request(command, env=None, **kwargs):
        assert 'offline-test-credential' not in command
        endpoint = next(name for name in ['start', 'step', 'finish'] if f'policy/{name}' in command)
        requests.append(endpoint)
        if endpoint == 'start':
            assert env == {'GEMINI_API_KEY': 'offline-test-credential'}
        data = {'status': 'ready'} if endpoint != 'step' else {
            'done': True, 'response_text': TERMINATE, 'groups': [],
            'screenshot_path': str(world.capture_observation()['screen']['path']),
        }
        return SimpleNamespace(return_code=0, stdout=json.dumps(data), stderr='')
    async def download(source, destination):
        shutil.copy2(source, destination)
    environment = SimpleNamespace(exec=exec_request, download_file=download)
    asyncio.run(agent.setup(environment))
    assert requests == []
    asyncio.run(agent.run('Add a license', environment, SimpleNamespace()))
    assert requests == ['start', 'step', 'finish']
    trajectory = json.loads((tmp_path / 'portable/trajectory.json').read_text())
    assert trajectory['steps'][0]['message'] == TERMINATE
    assert (tmp_path / 'portable/images/obs_0001.png').is_file()


def test_boot_failure_closes_owned_environment_and_preserves_original_error(tmp_path, monkeypatch):
    from gym_anything.integrations.harbor.container import boot_env
    environment = Mock()
    environment.reset.side_effect = RuntimeError('post_start failed')
    environment.close.side_effect = RuntimeError('cleanup failed')
    monkeypatch.setattr('gym_anything.config.loading.from_config', lambda *a, **kw: environment)
    with pytest.raises(RuntimeError, match='post_start failed') as failure:
        boot_env({'env_dir': str(tmp_path), 'task_id': 'add_license'})
    environment.close.assert_called_once()
    assert 'cleanup failed' in '\n'.join(failure.value.__notes__)


def test_real_harbor_verifier_collects_evidence_created_after_agent_collection(world, tmp_path):
    from harbor.models.task.task import Task
    from harbor.models.task.config import TaskOS
    from harbor.models.trial.paths import TrialPaths
    from harbor.verifier.verifier import Verifier
    task_dir = tmp_path / 'harbor-task'
    (task_dir / 'tests').mkdir(parents=True)
    (task_dir / 'instruction.md').write_text('Add a license')
    (task_dir / 'task.toml').write_text('schema_version = "1.3"\n')
    (task_dir / 'tests/test.sh').write_text('#!/bin/sh\nexit 0\n')
    paths = TrialPaths(trial_dir=tmp_path / 'host-trial')
    paths.mkdir()
    controller_verifier = world.episode_dir.parents[1]
    runtime = _Runtime(world)
    early_collection = list(paths.artifacts_dir.rglob('final.png'))
    assert not early_collection and not (world.episode_dir / 'final.png').exists()

    async def execute(command, **kwargs):
        if '/tests/test.sh' in command and '/logs/verifier' in command:
            result = runtime.finalize()
            (controller_verifier / 'reward.json').write_text(json.dumps({'reward': result['reward']}))
            (controller_verifier / 'verifier.json').write_text(json.dumps(result['verifier']))
        return SimpleNamespace(return_code=0, stdout='', stderr='')

    async def download(source_dir, target_dir, **kwargs):
        assert source_dir == '/logs/verifier'
        shutil.copytree(controller_verifier, target_dir, dirs_exist_ok=True)

    environment = SimpleNamespace(os=TaskOS.LINUX, exec=execute,
                                  capabilities=SimpleNamespace(mounted=False),
                                  upload_dir=AsyncMock(), download_dir=download)
    verifier = Verifier(Task(task_dir), paths, environment)
    result = asyncio.run(verifier.verify())
    assert result.rewards == {'reward': 0.0}
    for name in ['summary.json', 'final.png', 'post_verification.png']:
        assert (paths.verifier_dir / 'gym/episode' / name).is_file()
    assert world.step.call_count == 1


def test_model_response_survives_failed_gui_action(world, tmp_path, monkeypatch):
    raw = response(CLICK)
    monkeypatch.setattr('agents.agents.claude_gemini.call_gemini_with_retry', lambda *a, **kw: raw)
    world.step.side_effect = RuntimeError('desktop action failed')
    policy = ReferencePolicy(world, model='gemini-3.8-flash', max_steps=50, logs_dir=tmp_path / 'agent')
    with pytest.raises(RuntimeError, match='desktop action failed'):
        policy.step()
    with (tmp_path / 'agent/raw_responses.pkl').open('rb') as handle:
        assert pickle.load(handle)[0].model_dump() == raw.model_dump()
    assert json.loads((tmp_path / 'agent/usage.jsonl').read_text())['total_tokens'] == 15
    assert world.step.call_count == 1


def test_shutdown_does_not_wait_for_busy_policy_or_hide_original_failure(world):
    from gym_anything.integrations.harbor.container import _close_runtime
    world.close = Mock()
    runtime = _Runtime(world)
    failure = RuntimeError('controller startup failed')
    runtime.lock.acquire()
    try:
        _close_runtime(runtime, failure)
    finally:
        runtime.lock.release()
    world.close.assert_called_once()
    assert 'still active' in '\n'.join(failure.__notes__)


def test_portable_bridge_preserves_rpc_failure_when_finish_also_fails(tmp_path, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'offline-test-credential')
    agent = GymControllerAgent(logs_dir=tmp_path / 'agent', model_name='gemini-3.8-flash')
    calls = []
    async def request(environment, endpoint, *args, **kwargs):
        calls.append(endpoint)
        if endpoint == 'policy/step':
            raise RuntimeError('desktop failed')
        if endpoint == 'policy/finish':
            raise RuntimeError('finish failed')
        return {}
    monkeypatch.setattr(agent, '_request', request)
    with pytest.raises(RuntimeError, match='desktop failed') as failure:
        asyncio.run(agent.run('instruction', SimpleNamespace(), SimpleNamespace()))
    assert calls == ['policy/start', 'policy/step', 'policy/finish']
    assert 'finish failed' in '\n'.join(failure.value.__notes__)
    assert json.loads((tmp_path / 'agent/failure.json').read_text())['message'] == 'desktop failed'
    assert (tmp_path / 'agent/trajectory.json').is_file()


def test_shipped_bridge_uses_real_controller_rpc_without_host_gym(world, tmp_path, monkeypatch):
    """Separate Harbor-only interpreter and real HTTP transport; no live worker."""
    from gym_anything.integrations.harbor.container import _Handler
    from gym_anything.integrations.harbor import portable_agent
    harbor = shutil.which('harbor')
    if not harbor:
        pytest.skip('A separate Harbor installation is required')
    shebang = Path(harbor).read_text().splitlines()[0]
    clean_python = shebang.removeprefix('#!')
    probe = subprocess.run([clean_python, '-B', '-I', '-c',
        "import importlib.util; print(importlib.util.find_spec('gym_anything') is None)"],
        capture_output=True, text=True, timeout=10)
    if probe.returncode or probe.stdout.strip() != 'True':
        pytest.skip('Harbor executable does not use a Gym-free interpreter')
    monkeypatch.setenv('GEMINI_API_KEY', 'offline-test-credential')
    replies = iter([response(CLICK + TYPE + TERMINATE), response(TERMINATE)])
    monkeypatch.setattr('agents.agents.claude_gemini.call_gemini_with_retry', lambda *a, **kw: next(replies))
    policy = ReferencePolicy(world, model='gemini-3.8-flash', max_steps=50, logs_dir=tmp_path / 'controller-agent')
    runtime = _Runtime(world, {'portable_agent': True})
    def start(request):
        assert request['model'] == 'gemini-3.8-flash' and request['max_steps'] == 50
        runtime.policy = policy
        return {'status': 'ready'}
    runtime.policy_start = start
    monkeypatch.setattr(_Handler, 'runtime', runtime, raising=False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shipped = tmp_path / 'task/agent/gym_controller_agent.py'
    shipped.parent.mkdir(parents=True)
    shutil.copy2(portable_agent.__file__, shipped)
    script = '''
import asyncio,importlib.util,json,shlex,shutil,sys
from pathlib import Path
from types import SimpleNamespace
assert importlib.util.find_spec('gym_anything') is None
assert importlib.util.find_spec('sandweave') is None
spec=importlib.util.spec_from_file_location('shipped_bridge',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class Environment:
 async def exec(self,command,env=None,timeout_sec=None):
  args=shlex.split(command);args[0]=sys.executable
  process=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
  stdout,stderr=await process.communicate()
  return SimpleNamespace(return_code=process.returncode,stdout=stdout.decode(),stderr=stderr.decode())
 async def download_file(self,source,target):
  shutil.copy2(source,target)
agent=module.GymControllerAgent(logs_dir=Path(sys.argv[2]),model_name='gemini-3.8-flash',container_port=int(sys.argv[3]))
asyncio.run(agent.run('Add a license',Environment(),SimpleNamespace()))
print('HOST_GYM_ABSENT_REAL_RPC_PASSED')
'''
    try:
        result = subprocess.run([clean_python, '-B', '-I', '-c', script, str(shipped),
                                 str(tmp_path / 'host-agent'), str(server.server_address[1])],
                                capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert 'HOST_GYM_ABSENT_REAL_RPC_PASSED' in result.stdout
    atif = json.loads((tmp_path / 'host-agent/trajectory.json').read_text())
    assert len(atif['steps']) == 2
    assert atif['final_metrics']['total_prompt_tokens'] == 20
    assert atif['final_metrics']['total_completion_tokens'] == 10
    assert policy.finished
    assert world.step.call_count == 1
    assert runtime.finalize()['reward'] == 0.0  # Model's success termination cannot award credit.
