# GitLab CE environment

This environment runs pinned GitLab Community Edition `18.11.12-ce.0` in
Docker inside the required QEMU/Apptainer guest. It is sized at 12 GB RAM and
uses GitLab's documented memory-constrained settings: single-process Puma,
lower Sidekiq concurrency, and disabled monitoring exporters.

The installation hook starts the large pinned-image download as a detached
job. The setup hook similarly launches a detached worker that initializes
GitLab and imports the source data. The task hook waits for that worker's
explicit success marker before preparing the browser. Consequently the
environment uses `post_task` as its default cache level: an earlier cache could
capture a partially downloaded image or a partially initialized database.

## Real data

The setup imports the real public
[`gitlab-org/cli`](https://gitlab.com/gitlab-org/cli) Git repository, then
fetches seven named issues through the public GitLab API and recreates their
titles, descriptions, labels, issue types, and creation timestamps in the
local instance. Setup writes both the unmodified API records and a local/source
ID mapping to:

- `/home/ga/gitlab/seed/source_snapshot.json`
- `/home/ga/gitlab/seed/seed_manifest.json`

The selected public source issue IDs are `8551`, `8565`, `7554`, `7685`,
`979`, `939`, and `903`. The task targets source issue
[`#8551`](https://gitlab.com/gitlab-org/cli/-/work_items/8551).

## Access

- URL: `http://gitlab.local`
- Web account: `root` / `N7v!4Qz@8Lm#2Rx%`
- Container: `gitlab`
- Git-over-SSH guest port: `2224`

The fixed API token exists only for deterministic environment and task setup.
The interactive task starts with GNOME Web already authenticated and focused on
the imported project's real issue backlog.

## Evidence

[`evidence_docs/README.md`](evidence_docs/README.md) indexes the screenshots
and exact log excerpts captured from the final no-cache QEMU run. The task's
start-state image is also left in the guest at
`/tmp/gitlab_task_start.png` for runtime inspection.

## Task

`triage_windows_build_failure` asks the agent to identify the Windows-only
compilation failure from technical evidence and complete a realistic triage
workflow across issue search, assignment, labels, due date, and discussion.

## Source references

- GitLab Docker installation: <https://docs.gitlab.com/install/docker/installation/>
- GitLab constrained-memory configuration: <https://docs.gitlab.com/omnibus/settings/memory_constrained_envs/>
- GitLab health and readiness endpoints: <https://docs.gitlab.com/administration/monitoring/health_check/>
- Programmatic personal access tokens: <https://docs.gitlab.com/user/profile/personal_access_tokens/>
- GitLab CLI source project: <https://gitlab.com/gitlab-org/cli>
