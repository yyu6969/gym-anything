"""Deployment profiles and fail-closed bootstrap checks without live sandboxes."""
import json
from pathlib import Path
import tomllib
from unittest.mock import Mock

import pytest
import yaml

from gym_anything.integrations.harbor.compile import compile_task
from gym_anything.integrations.harbor.container import boot_env, load_task_config
from gym_anything.integrations.harbor.deployment import prepare_sandweave_worker, collect_sandweave_logs
from gym_anything.integrations.harbor.packaging import directory_digest


@pytest.fixture
def recipe(tmp_path):
    root = tmp_path / 'friendly_gitlab_folder'
    (root / 'tasks/add_license').mkdir(parents=True)
    (root / 'assets').mkdir()
    (root / 'assets/seed.bundle').write_bytes(b'immutable repository seed')
    (root / 'env.json').write_text(json.dumps({
        'id': 'gitlab_task_environment@0.1', 'runner': 'sandweave',
        'resources': {'cpu': 6, 'mem_gb': 12, 'gpu': 0},
        'mounts': [{'source': 'assets', 'target': '/workspace/assets', 'mode': 'ro'}],
    }))
    (root / 'tasks/add_license/task.json').write_text(json.dumps({
        'id': 'add_license@1', 'env_id': 'gitlab_task_environment@0.1', 'description': 'Add a license',
    }))
    return root


def compile_recipe(recipe, tmp_path, **kwargs):
    return compile_task(recipe.name, 'add_license', tmp_path / 'export', env_dir=recipe,
                        packaging_mode='vendored_snapshot', **kwargs)


def test_sandweave_profile_packages_selected_recipe_and_required_deployment(recipe, tmp_path):
    before = directory_digest(recipe)
    task = compile_recipe(recipe, tmp_path)
    config = load_task_config(task / 'environment/gym-anything.json')
    dockerfile = (task / 'environment/Dockerfile').read_text()
    compose = yaml.safe_load((task / 'environment/docker-compose.yaml').read_text())
    main = compose['services']['main']
    assert config['runner'] == config['deployment_profile'] == 'sandweave'
    assert config['sandweave_version'] == '0.2.41'
    assert 'sandweave[desktop]==0.2.41' in dockerfile and 'litellm==1.103.0' in dockerfile
    assert 'gym-runtime[sandweave,agents,vlm]' in dockerfile
    assert 'install_apptainer' in dockerfile and 'squashfs-tools' in dockerfile
    assert 'qemu-system-x86' not in dockerfile and '/dev/kvm' not in dockerfile
    assert 'GA_HARBOR_RUNNER=qemu' not in dockerfile
    assert main['environment']['GYM_ANYTHING_RUNNER'] == 'sandweave'
    assert main['environment']['GA_HARBOR_RUNNER'] == 'sandweave'
    assert main['environment']['APPTAINER_UNSQUASH'] == '1'
    assert 'devices' not in main and not main.get('privileged')
    assert 'network_mode' not in main and 'pid' not in main and 'security_opt' not in main
    assert main['cap_add'] == ['SYS_ADMIN']
    assert main['environment']['SANDWEAVE_HOME'] == '/sandweave-cache'
    assert main['environment']['SANDWEAVE_LOCAL_DIR'] == '/sandweave-scratch'
    assert compose['volumes']['sandweave-cache']['external']
    resource = tomllib.loads((task / 'task.toml').read_text())['environment']
    assert resource['cpus'] == 7 and resource['memory_mb'] == 16384
    assert resource['storage_mb'] == 32768
    snapshot = task / 'environment/gym-environment'
    assert (snapshot / 'assets/seed.bundle').read_bytes() == b'immutable repository seed'
    assert directory_digest(snapshot) == config['environment_snapshot_sha256']
    assert directory_digest(recipe) == before
    assert (task / 'agent/gym_controller_agent.py').is_file()
    assert config['portable_agent'] is True
    assert 'gym_controller_agent:GymControllerAgent' in (task / 'run-harbor.sh').read_text()
    assert 'Harbor 0.24.0' in (task / 'README.md').read_text()


@pytest.mark.parametrize('runner', ['qemu', 'qemu_native'])
def test_explicit_qemu_override_keeps_qemu_deployment(recipe, tmp_path, runner):
    task = compile_recipe(recipe, tmp_path, runner=runner)
    config = load_task_config(task / 'environment/gym-anything.json')
    dockerfile = (task / 'environment/Dockerfile').read_text()
    compose = yaml.safe_load((task / 'environment/docker-compose.yaml').read_text())
    assert config['runner'] == runner and config['use_cache'] is True
    assert 'deployment_profile' not in config
    assert 'qemu-system-x86' in dockerfile and 'qemu-utils' in dockerfile
    assert 'GA_HARBOR_RUNNER=qemu' in dockerfile
    assert 'sandweave[desktop]' not in dockerfile
    assert compose['services']['main']['devices'] == ['/dev/kvm']
    assert compose['volumes']['gym-anything-cache']['name'] == 'harbor-gym-anything-cache'
    assert 'cpus' not in tomllib.loads((task / 'task.toml').read_text())['environment']


def test_unsupported_deployment_runner_fails_before_writing(recipe, tmp_path):
    with pytest.raises(ValueError, match='Unsupported Harbor deployment runner'):
        compile_recipe(recipe, tmp_path, runner='unknown')
    assert not (tmp_path / 'export').exists()


@pytest.mark.parametrize('variable', ['GYM_ANYTHING_RUNNER', 'GA_HARBOR_RUNNER'])
def test_conflicting_runtime_override_fails_before_constructing_environment(recipe, tmp_path, monkeypatch, variable):
    task = compile_recipe(recipe, tmp_path)
    config = load_task_config(task / 'environment/gym-anything.json')
    constructor = Mock(side_effect=AssertionError('environment constructed'))
    monkeypatch.setattr('gym_anything.config.loading.from_config', constructor)
    monkeypatch.setenv(variable, 'qemu')
    with pytest.raises(ValueError, match='conflicts'):
        boot_env(config)
    constructor.assert_not_called()


@pytest.mark.parametrize('failure', [None, 'version', 'permission'])
def test_bootstrap_records_success_and_preserves_actionable_failures(tmp_path, monkeypatch, failure):
    monkeypatch.setattr('gym_anything.integrations.harbor.deployment.importlib.metadata.version',
                        lambda name: '0.2.40' if failure == 'version' else '0.2.41')
    monkeypatch.setattr('gym_anything.integrations.harbor.deployment.shutil.which', lambda name: '/tools/apptainer')
    for name in ['SANDWEAVE_HOME', 'SANDWEAVE_LOCAL_DIR', 'APPTAINER_TMPDIR', 'APPTAINER_CACHEDIR']:
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    check = Mock(side_effect=RuntimeError('namespace permission denied') if failure == 'permission' else None,
                 return_value={'architecture': 'x86_64', 'kernel': (5, 14, 0)})
    monkeypatch.setattr('sandweave.releases.check_host', check)
    logs = tmp_path / 'logs'
    config = {'sandweave_version': '0.2.41'}
    if failure:
        with pytest.raises(RuntimeError, match='permission denied' if failure == 'permission' else 'requires 0.2.41'):
            prepare_sandweave_worker(config, logs)
    else:
        assert prepare_sandweave_worker(config, logs)['status'] == 'passed'
    report = json.loads((logs / 'bootstrap.json').read_text())
    assert report['status'] == ('failed' if failure else 'passed')
    if failure == 'version':
        check.assert_not_called()
    else:
        check.assert_called_once_with(tmp_path / 'sandweave_home')


def test_worker_logs_are_collected_without_connection_credentials(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    monkeypatch.setenv('SANDWEAVE_HOME', str(cache))
    monkeypatch.setattr('gym_anything.integrations.harbor.deployment.socket.gethostname', lambda: 'controller')
    for name, host in [('own-worker', 'controller'), ('other-worker', 'another-host')]:
        directory = cache / 'connections' / name
        directory.mkdir(parents=True)
        (directory / 'worker.json').write_text(json.dumps({'hostname': host, 'token': 'private-connection-token'}))
        (directory / 'worker.log').write_text('worker diagnostic\n')
    logs = tmp_path / 'logs'
    collect_sandweave_logs(logs)
    files = list(logs.rglob('*'))
    assert [p.name for p in files if p.is_file()] == ['own-worker.log']
    assert (logs / 'worker-logs/own-worker.log').read_text() == 'worker diagnostic\n'
    assert 'private-connection-token' not in (logs / 'worker-logs/own-worker.log').read_text()
