# Shared GitLab CE environment

This environment installs pinned GitLab Community Edition `18.11.12-ce.0`
directly in the Ubuntu guest. PostgreSQL, Redis, Gitaly, Puma, Sidekiq,
Workhorse, and NGINX are managed by Omnibus under
`gitlab-runsvdir.service`; there is no nested Docker runtime.

It reconstructs one dependency-complete GitLab baseline shared by all 172
Wonderbread/WebArena GitLab tasks. It does not create an environment or
dataset per task, and it does not implement per-task reset logic.

## Logical dataset

The read-only source dataset is mounted from:

`/data/user_data/yingjiey/webarena-reference/extracted/gitlab_shared`

to `/workspace/gitlab_shared` in the guest. Setup performs three phases:

1. `prepare_shared_baseline.py` verifies schemas, checksums, all 28 Git
   bundles, required refs, and all 168 declared initial-state exclusions.
2. `seed_gitlab.rb` reconstructs application entities through GitLab Rails
   services and restores repositories through GitLab's `RepoRestorer`.
3. `validate_shared_baseline.py` independently validates the live API,
   repository refs and files, declared absences, aggregate counts, and
   loopback-only listeners.

The seeded baseline contains:

- 39 projects and 22 personal namespace dependencies
- 28 complete repositories with canonical history and refs
- 70 extracted identity records: 59 GitLab-account records normalized to 45
  unique task-relevant accounts, plus 11 commit-only identities
- 14 additional dependency-only accounts required to realize personal
  namespaces that are not among the 45 task-relevant accounts
- 13 direct project memberships
- 29 labels, 1 milestone, 10 issues, 8 merge requests, and 85 notes
- 168 task-created entities or relationships that are verified absent

Repository creation and merge-request callbacks can create target-managed refs.
After application entities are created, the seeder reapplies only MR-touched
bundles so every extracted ref and OID remains canonical.

## Access

- URL: `http://gitlab.local`
- Browser account: `byteblaze` / `N7v!4Qz@8Lm#2Rx%`
- Administrative account: `root` / `N7v!4Qz@8Lm#2Rx%`
- Deterministic administrative API token: `gitlab-seed-token123`
- Native service manager: `gitlab-ctl` / `gitlab-runsvdir.service`

Firefox is launched already authenticated as `byteblaze`. GitLab's HTTP,
status, Puma, and Workhorse listeners bind only to guest loopback; runner-owned
SSH/VNC/VM forwarding is unchanged.

## Validation artifacts

A successful setup writes user-readable reports to:

- `/home/ga/gitlab/seed/dataset_preflight.json`
- `/home/ga/gitlab/seed/seed_manifest.json`
- `/home/ga/gitlab/seed/baseline_validation.json`

The setup prints `Environment Ready: shared GitLab baseline validated` only
after the readiness endpoint, API token, live baseline validator, and
authenticated browser dashboard have all passed.

Intentional target-version normalizations are recorded in
`seed_manifest.json`: source database IDs are remapped to semantic
identifiers, repository storage paths are target-managed, and six
`LegacyDiffNote` rows are represented as ordinary notes because the logical
extraction has no legacy diff-position payload. Bodies, authors, parents, and
timestamps remain preserved.

The environment uses the successful `post_start` state as its default cache
boundary. Filesystem restores restart native services and recheck readiness;
task-specific deterministic resets are a separate follow-up phase.

## Source references

- GitLab Linux package installation: <https://docs.gitlab.com/install/package/>
- GitLab constrained-memory configuration: <https://docs.gitlab.com/omnibus/settings/memory_constrained_envs/>
- GitLab health and readiness endpoints: <https://docs.gitlab.com/administration/monitoring/health_check/>
- Programmatic personal access tokens: <https://docs.gitlab.com/user/profile/personal_access_tokens/>
