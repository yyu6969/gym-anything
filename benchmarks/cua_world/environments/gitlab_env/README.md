# GitLab CE environment

This environment installs the pinned GitLab Community Edition package
`18.11.12-ce.0` directly in the Ubuntu guest. GitLab's Omnibus-managed
PostgreSQL, Redis, Gitaly, Puma, Sidekiq, Workhorse, and NGINX services run
under `gitlab-runsvdir.service`; no nested Docker runtime is required. The
12 GB guest retains the constrained-memory configuration: single-process
Puma, lower Sidekiq concurrency, and disabled monitoring exporters.

The installation hook configures the official GitLab package repository,
installs and pins the exact package version, writes `/etc/gitlab/gitlab.rb`,
and runs `gitlab-ctl reconfigure`. The synchronous setup hook then waits for
the full readiness endpoint, creates the deterministic root token, explicitly
enables repository-by-URL import, seeds the application, and opens Firefox at
`http://gitlab.local`. A successful `post_start` hook is the stable cache
boundary, so the environment uses `post_start` as its default cache level.

A filesystem cache restore does not assume that cached processes remain alive.
The task hook starts `gitlab-runsvdir.service` and the Omnibus services when
needed, waits for full readiness, validates the root API token, resets the
task-specific state, authenticates Firefox, and navigates to the project's
Issues page before returning.

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
- Native service manager: `gitlab-ctl` / `gitlab-runsvdir.service`

The fixed API token exists only for deterministic environment and task setup.
The interactive task starts with Firefox already authenticated and focused on
the imported project's real issue backlog.

GitLab NGINX listens only on guest loopback. There are no Docker-published
GitLab ports and no GitLab listener is bound to `0.0.0.0` or `[::]`.
Host-facing VM, VNC, and SSH forwarding remains owned by the runner and is not
changed by this environment.

## Task

`triage_windows_build_failure` asks the agent to identify the Windows-only
compilation failure from technical evidence and complete a realistic triage
workflow across issue search, assignment, labels, due date, and discussion.

## Source references

- GitLab Linux package installation: <https://docs.gitlab.com/install/package/>
- GitLab constrained-memory configuration: <https://docs.gitlab.com/omnibus/settings/memory_constrained_envs/>
- GitLab health and readiness endpoints: <https://docs.gitlab.com/administration/monitoring/health_check/>
- Programmatic personal access tokens: <https://docs.gitlab.com/user/profile/personal_access_tokens/>
- GitLab CLI source project: <https://gitlab.com/gitlab-org/cli>
