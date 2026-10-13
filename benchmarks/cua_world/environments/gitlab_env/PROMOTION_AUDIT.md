# Task 414 / task 415 promotion audit

## References and attribution

The behavioral reference is the old environment at
`/data/user_data/yingjiey/outputs/selected_webarena_tasks/environment_reconstruction_cheating/414_414_@_2024-01-06-21-31-38/environment`.
Its successful episode is
`/home/yingjiey/.local/state/gym-anything/artifacts/episode_20261008_011026_b215878d-b9d4-4eb2-bd44-40c94294b853`.
The trajectory contains real clicks and typing for filtering dotfiles, opening
Add LICENSE, applying MIT, and committing. `frame_00003.png` shows the LICENSE
editor; `frame_00007.png` shows the committed MIT license. Later replay errors
do not negate this completion.

There are two commits in the canonical environment's recorded history:

- `2b52e44ddf0221d19e08ed63a5020cb42f67f357`, October 6: first canonical
  environment snapshot, including task 414 and all shared setup files.
- `9df1107348338457d0ec746fb38b1f0bc94d6b26`, October 7: adds task 415.

The task 415 commit changes exactly seven paths:

| Path | Change |
| --- | --- |
| `assets/README.md` | Appends documentation for the two task 415 bundles; existing dotfiles documentation is unchanged. |
| `assets/a11y-webring.bundle` | Adds the real task 415 repository bundle. |
| `assets/a11yproject-distractors.bundle` | Adds the real distractor repository bundle. |
| `tasks/tag_davepgreene_merge_request/seed_merge_request.rb` | Adds task-specific MR seeding. |
| `tasks/tag_davepgreene_merge_request/setup_task.sh` | Adds task-specific reset and browser startup. |
| `tasks/tag_davepgreene_merge_request/task.json` | Adds task registration. |
| `tasks/tag_davepgreene_merge_request/verifier.py` | Adds its compatibility verifier. |

SHA-256 comparisons of Git blobs at both commits and HEAD confirm that
`env.json`, the entire `config/` and `scripts/` trees, `assets/dotfiles.bundle`,
and the entire tracked `tasks/add_mit_license/` tree are unchanged by task 415.
The 24-project/three-star seed already existed in the first canonical snapshot.
All old-versus-canonical source differences below therefore predate that
promotion in recorded history. The old reference is outside this repository;
the history cannot establish when those external files were originally authored.

## Every task-414-relevant source difference

| File or area | Old reference → first canonical snapshot | Effect on task 414 |
| --- | --- | --- |
| `env.json` identity and metadata | `gitlab_task_414_env@0.1` → `gitlab_env@0.1`; shared description and different tags. | Registration/provenance; task description remains identical. |
| `env.json` resources | Six CPUs → four; both have 12 GB, no GPU, network enabled. | Less CPU capacity, but observed guest health does not establish starvation/OOM. |
| `env.json` recording | Explicit `enable: false` block removed. | Default recording assembles saved frames after the episode; QEMU does not use a live recorder here. |
| `env.json` VNC/security | Explicit port 5901 and `cap_drop: [ALL]` removed; group order changed. | Same defaults; no different SSH credentials or forwarded SSH configuration. |
| `env.json` mounts/hooks | `scripts` etc. → `./scripts` etc.; `bash</workspace/...` → quoted `/bin/bash /workspace/...`. | Same four destinations. Both hook forms execute successfully. Neither version restarts guest networking/sshd. |
| `env.json` cache | `default_cache_level` moved in the JSON, still `post_start`. | No semantic change. Successful old and reproduced canonical trajectories both record `use_cache: false`, `checkpoint_loaded: false`. |
| `config/docker-compose.yml` image/runtime | Adds the CE version tag to the same immutable image digest; container `gitlab` → `gitlab-ce`, hostname `.local` → `.localhost`; shared memory 1 GB → 256 MB. | Same GitLab 15.11.13 code. Lower shared memory is a resource difference, not proven to cause failure. |
| `config/docker-compose.yml` ports/paths | HTTP 8023 → 8929; publishes GitLab SSH 2224; volumes `gitlab-service` → `gitlab`. | References in seed, task and browser scripts change consistently. GitLab container SSH is separate from guest sshd/Gym SSH. |
| `config/docker-compose.yml` options | Removes explicit HTTP NGINX/Let's Encrypt, registry/pages disable flags and explicit issues/MR/wiki feature flags; adds usage-ping/signup/container-registry/default-protection settings. Puma remains two workers; Sidekiq concurrency remains ten; monitoring stays disabled. | Configuration differs, but both live UIs provide repository editing. No networking/guest SSH restart. |
| `config/docker-compose.yml` root password | `RootPassword414!` → `GitLabRoot123!`. | Both are rejected by GitLab's administrator password validator. The current working-tree fix replaces the latter with a validated password; it is not a task 415 change. |
| `config/chrome-policy.json` | New Chrome prompt/password/translation/session policies. | Affects browser chrome, not GitLab repository permissions. |
| `scripts/install_gitlab.sh` | Replaces Firefox/snapd with pinned native Chrome 154 plus websocket-client 1.9.2; changes supporting apt packages; pulls the pinned GitLab image here rather than in post_start; optional Docker Hub login. | Different browser installation/startup. The image digest is unchanged. No extra host SSH connection per apt, Docker or CDP command. |
| `scripts/setup_gitlab.sh` service sequencing | Different service paths; copies Chrome policy; polling HTTP and Rails readiness; image already pulled. | Both versions wait for actual service readiness. Canonical post_start no longer launches the browser; pre_task does. |
| `scripts/setup_gitlab.sh` account/import | Old inline Rails user/PAT creation and API project creation → external Rails metadata seed and password-authenticated Git pushes. | Old Byte Blaze password `hello1234`, API PAT and `.com` email → canonical password `V9xQ4mL2pR7s!`, no PAT and `.test` email. Canonical explicitly sets non-admin status and owner membership. |
| `scripts/setup_gitlab.sh` repository | Old single `main` at `d2bba4f…` → `main` at `218b5e7…` plus historical `master` at `e19ab89…`. | Branch/history counts change. Both main trees lack any license file; detailed tree comparison below. |
| `scripts/seed_gitlab.rb` | New file, replacing metadata embedded in the old setup script. | 22 personal projects/no stars → 24 accessible projects/three stars and A11Y Project/n-lab/Primer groups; most distractors are metadata-only. This already existed before task 415. |
| `scripts/chrome_cdp.py` | New CDP authentication/navigation helper. | Replaces xdotool Firefox login; validates navigation and dismisses a push banner. It uses guest-local HTTP/WebSocket connections. |
| `scripts/task_utils.sh` | Old-only shared Firefox, API and screenshot utilities. | Replaced by canonical task shell and CDP helper; not an omitted dependency of canonical scripts. |
| `scripts/validate_task_414.py` | Old-only live validation helper. | Not invoked by old env.json/task.json hooks; its removal does not disable setup. |
| `assets/dotfiles-source.bundle` → `assets/dotfiles.bundle` | Different filenames and bundle bytes; canonical includes additional upstream history/refs. | Baseline revision changes, not a bundled LICENSE addition. |
| `assets/source_manifest.json`, old root `README.md` → `assets/README.md` | Provenance documentation reorganized. | Not executed or supplied as the agent's task instruction. |
| `tasks/add_mit_license/task.json` | Environment ID changes; name/difficulty/WebArena metadata removed; init timeout 240 → 300; explicit hook timeout 300 removed, default 600 applies; hook syntax changes. | Task instruction, max_steps 40, sparse reward and verifier entry point remain the same. None is a task 415 change. |
| `tasks/add_mit_license/setup_task.sh` | API/token reset → direct Rails deletion and password-authenticated push; no old activity-date ordering reset; alphabetically sorted dashboard; fresh Chrome profile and CDP instead of Firefox; local SHA/tree assertions instead of API LICENSE status check. | Old reset also refreshes the protection and license caches and asserts actual push permission. Those checks were omitted during canonical migration. |
| `tasks/add_mit_license/verifier.py` | Docstrings/comments shortened. | Returned `passed`, `score`, and `feedback` are unchanged. Both are unconditional compatibility stubs; a score of 100 is not evidence that an agent added LICENSE. |

No task 415 seed/reset script is invoked by task 414. The additional bundle
files are copied as data in the existing assets mount. QEMU recursively copies
a directory over one SFTP connection; it does not open another SSH transport
for each file. More files increase transfer volume, not the number of mount
connections. Additional guest Rails/HTTP checks likewise do not create host
SSH/SFTP connections.

## Byte-for-byte comparison of the whole task directory

At audit time the old task directory has four files; canonical has fourteen.
None of the four common paths has identical bytes:

| Common path | Old bytes / SHA-256 | Canonical bytes / SHA-256 before this audit's fix |
| --- | --- | --- |
| `task.json` | 738 / `f2aaf768797f5ddabd8b193ed3ca836cce6e4616be7167526fec920b2bd2fe07` | 467 / `a5004b247d5644889ebac763b0042eee2580b333d57a4a583dfda18d2b11f890` |
| `setup_task.sh` | 3531 / `880b1eed113107c6b0e031df7035123bbf42909b98e29443e2faec7701e014d7` | 4946 / `5288d9a541cdbef84f6297e0f3b4b884e5d556066408b0554cf6234ba615612a` |
| `verifier.py` | 466 / `7c79e8a45f7d78581d5f95483a886780e3b402c61cd1c4684236f80615f232b6` | 295 / `611f5150e2a96d86907743ca9eabec30a8745f2dd35ece30bc31fc3572257b30` |
| `__pycache__/verifier.cpython-312.pyc` | 762 / `e1f3a0c134ab78915eebb5f8fd2f2c72742b2dba0670567fbe4a92dfc49e7c83` | 596 / `7d06280c54256026c951205ef811fd20aeafb16b70ea97930a7487018399cbfa` |

The ten canonical-only files, all under ignored `evidence_docs/`, are
`01_task_start.png`, `02_filtered_projects.png`, `03_repository.png`,
`04_editor.png`, `05_mit_template.png`, `06_completed.png`,
`07_repeatable_start.png`, `README.md`, `implementation_notes.md`, and
`setup_logs.txt`. They are not referenced by hooks or the agent prompt loader.
They and the generated bytecode are absent from both commits, so their addition
cannot be attributed to the task 415 commit. Old evidence lives at environment
root instead, outside the four mounted directories.

The complete fourteen-path size/SHA-256 manifest is saved in ignored
`artifacts/runtime-investigation/promotion-audit/task414-byte-comparison.json`.
Both setup scripts and verifiers have filesystem mode 0750; task.json is 0640
in the reference and 0644 in canonical. Hooks explicitly invoke bash, so these
modes do not explain a missing executable task hook.

The canonical evidence's `06_completed.png` and `07_repeatable_start.png` have
the same SHA-256, `2c505663bedc92a4a02cd645053acfb46c1ba03c36836e4c4cd02c7c0d14dc4b`.
Despite the documentation describing the latter as a clean reset, it is a copy
of the completed state. It cannot substantiate a repeatability claim. Live
validation is used instead.

## Bundle and baseline contents

| Property | Old | Canonical |
| --- | --- | --- |
| Bundle SHA-256 | `46ecb76649f5a8146f0789bc643a2cc56dd640f892d3493ae19eeef2c22ef526` | `8994cef34babacba5997a05d48628ee13407e1aa137122aadcea2582f15a8cb3` |
| Baseline commit | `d2bba4f3f829699aed557709e6f4f5734144ba5a` | `218b5e72424aca8b580e52342dbb92bd4bd076c8` |
| Author / subject | Eric Bailey / Update .macos | Eric Bailey / Update .macos |
| Date | 2023-05-29 | 2023-03-19 |
| Ancestor commit count | 555 | 553 |
| Tree files | 47 | 47 |
| License/LICENCE/COPYING paths | None | None |

`git diff` between these two baseline trees changes only `.macos`: removes the
Command X and Stempel install entries and reorders CiderTV. README and all other
files are identical. The canonical master branch also has no license-like
paths. These bundle differences cannot explain hiding Add LICENSE on main.

## Reproduced workflow regression and minimal fix

A fresh uncached canonical boot reached the authenticated Projects dashboard.
Fetching the dotfiles page as that browser user returned HTTP 200 and a real
Add LICENSE link to
`/byteblaze/dotfiles/-/new/main?commit_message=Add+LICENSE&file_name=LICENSE`.
Thus the fresh baseline itself does not lack the workflow.

The old reset's protection-cache refresh is nevertheless necessary. In that
same disposable canonical VM, GitLab's own `ProtectedBranches::CreateService`
created a normal main-branch rule allowing no direct pushes. Reading its
protection status warmed the actual Redis cache. Then the unmodified canonical
`setup_task.sh` ran and returned zero with `=== Episode ready ===`.

| Actual state after the old canonical reset | Value |
| --- | --- |
| Protected-branch database rows | 0 |
| `ProtectedBranch.protected?(project, 'main')` | true |
| Byte Blaze `can_push_to_branch?('main')` | false |
| Cached license present | false |
| Main SHA | Correct `218b5e72424aca8b580e52342dbb92bd4bd076c8` |
| Add LICENSE in the authenticated repository HTML | Absent |

GitLab 15.11's `ProjectPresenter#license_anchor_data` requires an absent
`repository.license_blob` **and** push permission on the default branch. The
direct `destroy_all` in canonical reset removes database rows without invoking
the GitLab service's `refresh_cache`; the Redis decision has a one-day TTL.
The old setup and task reset explicitly call
`ProtectedBranches::CacheService.new(project).refresh`. Canonical migration
omitted that behavior. This is an environment-specific reset regression,
present in `2b52e44dd`, not introduced by the task 415 promotion.

The minimal source fix is in `tasks/add_mit_license/setup_task.sh`: refresh
the protected-ref cache immediately after deleting main's rules, then assert
Byte Blaze can push before continuing. The regression test creates and warms
the genuine rule, invokes the actual task hook, and checks both permissions
and the authenticated Add LICENSE link. It does not reproduce the hook's
implementation in a mock. No browser, bundle, seed, task 415, CPU, memory,
timeout, sleep or SSH setting changes are needed for this defect.

This proves why a reset/checkpoint containing prior protection can hide Add
LICENSE even while setup claims success. It does not explain Gemini terminating
before its first click on a fresh boot: that boot already has the link. The
multi-call response/parser behavior and SSH fallback leak documented in
`RUNTIME_INVESTIGATION.md` remain separate observations, outside permitted
source scope. The password bootstrap fix is also separate from this reset bug.

## Audit commands

From the repository root, with `P` denoting the canonical environment path:

```bash
git log --all --format='%h %ad %s' --date=iso -- "$P"
git diff --name-status 2b52e44dd 9df110734 -- "$P"
git diff --quiet 2b52e44dd 9df110734 -- "$P/tasks/add_mit_license"
git diff --quiet 2b52e44dd 9df110734 -- "$P/env.json" "$P/config" "$P/scripts" "$P/assets/dotfiles.bundle"
```

Both `--quiet` comparisons return zero. A Python pathlib recursive walk with
SHA-256 covers ignored files too. The bundle trees were inspected with bare
clones under this environment's ignored artifact directory, `git ls-tree`,
`git rev-list --count`, and `git diff` between the two baseline SHAs. The old
environment was only read; no source outside canonical gitlab_env was changed.
