# GitLab runtime investigation

## Scope and findings

Only this environment was changed. The QEMU runner, agents, AAB, and Harbor
were inspected but left untouched.

The successful control episode is
`episode_20261008_011026_b215878d-b9d4-4eb2-bd44-40c94294b853` under
`/home/yingjiey/.local/state/gym-anything/artifacts`. Its saved agent inputs,
trajectory, initial screenshot, `frame_00007.png`, and final screenshot confirm
authenticated GitLab, the MIT template, and an `Add LICENSE` commit on `main`.
Its later finalization errors do not invalidate that UI completion.

Canonical episodes `episode_20261008_000636_d499f4fb-981e-4b16-8c07-e4c92f459b13`
and `episode_20261008_014012_76757b45-e7b9-4e9d-9394-f9ae6dceb36a` also completed
their setup hooks and supplied an authenticated Projects dashboard to Gemini.
Each contains one model response and two empty environment steps. The model
response contained 11 and 15 tool calls, respectively, ending in `terminate`.
`parse_qwen3vl_response` selects the last tool call. Consequently no preceding
mouse or keyboard action was executed. This is distinct from SSH failures.

Reissuing the saved initial inputs reproduced both a valid single action and
premature multi-action termination with the **old** screenshot. The canonical
screenshot also produced valid single actions. The system and task text prompts
are identical. These observations do not establish an environment regression
as the cause of premature agent termination.

The subsequent [promotion audit](PROMOTION_AUDIT.md) found a separate, reproduced
environment reset regression: deleting main's protected-branch rules left a
stale Redis protection decision, hiding Add LICENSE despite an apparently
successful reset. The old task explicitly refreshed this cache. That omission
predates the task 415 promotion; the promotion changed no shared setup or task
414 source files. Task 414 now refreshes it and checks push permission before
continuing. This addresses the repeated-reset workflow, not the premature
multi-call termination observed on fresh boots.

## Direct environment comparison

| Area | Old environment | Canonical environment |
| --- | --- | --- |
| GitLab image | CE 15.11.13, digest `798b18325a90851922c916fcded34d1ba7decf810a311765b43e43f452bf564c` | Same digest |
| VM resources | 6 CPUs, 12 GB RAM | 4 CPUs, 12 GB RAM |
| GitLab processes | 2 Puma workers, Sidekiq concurrency 10, monitoring disabled | Same |
| Container shared memory | 1 GB | 256 MB |
| Browser | Firefox snap, GUI login | Pinned Chrome 154, CDP login, fresh task-owned profile |
| Browser setup | Both `post_start` and `pre_task` | `pre_task` |
| GitLab URL/container | Port 8023, `gitlab` | Port 8929, `gitlab-ce` |
| Service/clone directories | `gitlab-service`, `gitlab-seed` | `gitlab`, `gitlab_seed` |
| Mounts | scripts, config, assets, tasks | Same four guest destinations; additional real bundles and task evidence files |
| Hook commands | `bash</workspace/...` | Quoted `/bin/bash /workspace/...` commands |
| Reset | API token, force push, Rails protection/license cache refresh | Rails protection deletion, force push, SHA/tree validation; this fix restores protection-cache refresh and push permission validation |
| Task baseline | `d2bba4f3f829699aed557709e6f4f5734144ba5a` | `218b5e72424aca8b580e52342dbb92bd4bd076c8`, plus a historical `master` branch |
| Projects/stars | 22/0 | 24/3, including shared namespaces needed by task 415 |
| Recording configuration | Explicitly disabled | Default enabled; QEMU has no live recorder, so this only assembles saved frames after completion |
| Cache level | `post_start` | `post_start` |
| Recorded cache use | No cache or checkpoint restore | No cache or checkpoint restore |
| Task metadata/timeouts | WebArena metadata, task timeout 240, hook timeout 300 | No source metadata, task timeout 300, default hook timeout 600 |

The additional Rails, HTTP, CDP, and validation commands run **inside** the
guest's hook command. They do not create extra host SSH/SFTP connections.
Neither environment's scripts reconfigure networking, restart sshd, reboot the
VM, or invoke `savevm`/`loadvm`.

## Local bootstrap defect and fix

GitLab rejected the canonical `initial_root_password` during its first database
seed:

```
Could not create the default administrator account:
--> Password must not contain commonly used combinations of words and letters
```

The container restarted once, then became HTTP-ready with no root administrator.
GitLab's actual user validator also rejects the old environment's root password.
This is a shared bootstrap defect, **not** the distinguishing cause of the old
episode's success versus the canonical episode's premature termination.

`config/docker-compose.yml` now supplies a password accepted by that validator.
On a fresh interactive boot, GitLab completed its database seed without a
container restart; the root administrator exists. HTTP readiness took 125
seconds, versus 195 seconds after the rejected seed in the baseline reproduction.
No sleeps, timeouts, SSH limits, task bypasses, or resource settings were changed.

## Remaining issues outside the permitted scope

The runner's missing-key fallback calls `SSHClient.connect` twice on one client
without closing the first transport. The second connection replaces the stored
transport, so closing the client leaves the first unauthenticated connection
alive. Repeated SSH/SFTP calls reach guest sshd's `MaxStartups` limit and cause
banner failures. Guest journals show throttling while sshd remains running,
`NRestarts=0`, GitLab is healthy, memory is available, and the same forwarded
port accepts correctly closed password-authenticated clients. This can affect
either environment; it does not explain a model's immediate termination.

`VM ready!` is printed by the runner before `pre_start`, `post_start`, and
`pre_task` execute. It proves that the desktop/control endpoint booted, not that
GitLab or task setup completed. In the fresh canonical reproduction, the first
agent request followed the successful task hook; the first saved action was
empty. Guest SSH throttling and failed finalization transfers occurred with a
healthy application and a correct browser start page.

Preventing premature termination requires correcting the agent's handling of
multi-call responses. Eliminating the leaked control connections requires
correcting the runner's authentication fallback. Neither change is permitted by
the requested environment-only scope.

The checkpoint hash currently includes the base and hook strings, but does not
hash the mounted configuration or scripts. Existing disk checkpoints therefore
need regeneration to incorporate a configuration fix. The normal commands below
do not request cache reuse.

## Focused live regression checks

Run against a fresh idle VM after the task hook completes. Use the forwarded SSH
port printed by Gym. The checks use one properly closed password-authenticated
client, independently of the runner's fallback bug.

```bash
GITLAB_ENV_SSH_PORT=<port> .venv/bin/python -m pytest \
  third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env/tests/test_lifecycle.py -q
```

For task 415, also set
`GITLAB_ENV_TASK=tag_davepgreene_merge_request`. This enables checks for the real
merge request, its author and assignee, historical branches, absence of a prior
mention, and the three assigned open merge requests.

The common tests check first-boot migration logs and restart count, root account
creation, the writable no-license baseline, and both the browser page and active
window. The task verifiers currently return an unconditional compatibility
score, so their CLI success alone is not evidence of UI task completion.

## Validation after the configuration fix

Commands were run from `/data/user_data/yingjiey/automatic-agent-benchmarking`
using `.venv/bin/gym-anything`, with console logs redirected to this
environment's ignored `artifacts/runtime-investigation/` directory.

```bash
PYTHONUNBUFFERED=1 .venv/bin/gym-anything run \
  /data/user_data/yingjiey/automatic-agent-benchmarking/third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env \
  --task add_mit_license -i

PYTHONUNBUFFERED=1 .venv/bin/gym-anything verify task \
  third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env \
  --task add_mit_license

PYTHONUNBUFFERED=1 .venv/bin/gym-anything benchmark \
  /data/user_data/yingjiey/automatic-agent-benchmarking/third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env \
  --task add_mit_license --agent Gemini3Agent --model gemini-3.8-flash --steps 50

PYTHONUNBUFFERED=1 .venv/bin/gym-anything verify task \
  third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env \
  --task tag_davepgreene_merge_request
```

- Interactive episode
  `episode_20261008_014856_3232a56e-acb9-4c2f-9b5f-e3c88b13c3c3`: the correct
  Chrome dashboard was visually inspected. The three common live checks passed.
  Thirty-one fresh host-to-guest SSH connections and health commands passed over
  306.7 seconds after task setup, with sshd `NRestarts=0`, container restarts zero,
  and healthy GitLab throughout. The same VM then successfully ran task 415's
  actual setup script; all four live checks passed, and its dashboard screenshot
  showed three assigned merge requests.
- `verify task --task add_mit_license`: status OK, episode
  `episode_20261008_020052_58e01ec9-afa1-4717-b73f-e431ddb821d2`. Its task setup
  completed correctly. Finalization still emitted banner failures. The reported
  verifier score is the existing unconditional compatibility score.
- Benchmark episode
  `episode_20261008_020659_9413302e-5a16-45c2-961d-259c40ba67da`: setup reached
  the authenticated dashboard with healthy GitLab and sshd. Gemini returned ten
  tool calls ending in `terminate`. Only the terminal call was parsed; steps 0
  and 1 were empty, and the final screenshot remained the Projects dashboard.
  **The requested benchmark success criterion was not met.** Finalization also
  emitted SSH banner failures, and some hook-log artifact copies failed.
- Targeted benchmark/verification tests: 26 passed. The broader Gym test suite
  stopped during collection because `psutil` and `quart` are not installed.
- `verify task --task tag_davepgreene_merge_request`: status OK, episode
  `episode_20261008_021455_df99f938-7f8c-4ba9-9cf7-10ab9f0f7020`. The normal
  task hook restored the expected branches, merge request !40, author
  `davepgreene`, assignee `byteblaze`, failed pipelines, and three assigned open
  merge requests. Its initial screenshot confirmed the authenticated dashboard.
  Finalization still emitted banner failures; the compatibility verifier score
  does not test posting a mention.

All debugging QEMU/Apptainer processes and guest observers were stopped. The
process list was checked after the final verification command exited.

The live test commands used SSH port 2350 and were:

```bash
PYTHONDONTWRITEBYTECODE=1 GITLAB_ENV_SSH_PORT=2350 .venv/bin/python -m pytest \
  third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env/tests/test_lifecycle.py \
  -q -o cache_dir=third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env/artifacts/pytest-cache

PYTHONDONTWRITEBYTECODE=1 GITLAB_ENV_SSH_PORT=2350 \
  GITLAB_ENV_TASK=tag_davepgreene_merge_request .venv/bin/python -m pytest \
  third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env/tests/test_lifecycle.py \
  -q -o cache_dir=third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env/artifacts/pytest-cache
```

The fast checks were run from `third_party/gym-anything` with
`PYTHONDONTWRITEBYTECODE=1` and the pytest cache directed to this environment's
ignored artifact directory:

```bash
/data/user_data/yingjiey/automatic-agent-benchmarking/.venv/bin/python -m pytest \
  tests/test_benchmark_registry.py tests/test_verification_system.py \
  tests/test_verification_status.py -q \
  -o cache_dir=/data/user_data/yingjiey/automatic-agent-benchmarking/third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env/artifacts/pytest-cache

/data/user_data/yingjiey/automatic-agent-benchmarking/.venv/bin/python -m pytest tests -q \
  -o cache_dir=/data/user_data/yingjiey/automatic-agent-benchmarking/third_party/gym-anything/benchmarks/cua_world/environments/gitlab_env/artifacts/pytest-cache
```
