# GitLab CE environment notes

## Chosen architecture

- Run the official `gitlab/gitlab-ce:18.11.12-ce.0` image with Docker Compose
  inside the required QEMU/Apptainer Ubuntu GNOME guest.
- Allocate 4 vCPUs and 12 GB RAM. Use GitLab's constrained-memory settings:
  Puma single mode (`worker_processes = 0`), Sidekiq concurrency 10, and the
  bundled Prometheus/exporter and KAS services disabled.
- Pin both GitLab CE and Docker Compose (`v2.39.4`, verified by SHA-256) so the
  benchmark does not silently change when upstream tags move.

## Boot and lifecycle behavior

The GitLab image is about 1.7 GB compressed and unpacking it is especially slow
when Docker itself runs inside software-emulated QEMU. A synchronous image pull
can exceed the framework's pre-start hook window. `install_gitlab.sh` therefore
starts a `setsid`/`nohup` pull worker and writes an explicit status file under
`/var/lib/gym-anything-gitlab/`.

`setup_gitlab.sh` follows the same pattern. Its foreground invocation only
checks Docker and launches a detached worker. The worker waits for the pinned
image, creates the container, polls `/-/readiness`, creates the deterministic
setup token with `gitlab-rails runner`, and imports data. The task hook waits on
`setup.status` and fails with the worker log if it contains `failed:<status>`.

Use the `post_task` cache level. `post_start` deliberately returns before the
background setup is complete, so caching at `post_start` could preserve a
partially initialized GitLab database.

On hosts without KVM, first initialization is measured in tens of minutes:
schema migration, cache clearing, and initial Rails boot are all CPU-bound.
Keep progress logging and long bounded timeouts; do not replace readiness
polling with a fixed sleep.

## Credentials and configuration

GitLab 18.11 rejects common passwords during the initial administrator fixture.
The environment uses the deterministic but non-dictionary password recorded in
the environment README. A rejected initial root password presents as a failed
`gitlab:db:configure` near `db:seed_fu`, not as a browser login problem.

The local hostname is `gitlab.local`; add it to `/etc/hosts` before starting the
container. Persist only the environment's explicit `config`, `logs`, and `data`
directories under `/home/ga/gitlab`. This makes development reruns safe to
reset without touching unrelated Docker state.

## Real source data

The seed script imports the public `gitlab-org/cli` Git repository and fetches
the project, default-branch commit, and seven issues from GitLab's public API at
setup time. It stores the unmodified responses in `source_snapshot.json` and a
source-to-local ID mapping in `seed_manifest.json`. The triage task targets
source issue #8551; source issue #8565 supplies the existing
`automation:quick-win-judged` project label required by the task.

## Browser automation

Ubuntu's GNOME session exposes the working X cookie at
`/run/user/1000/gdm/Xauthority` on this base image; fall back to
`/home/ga/.Xauthority`. Pass `DISPLAY=:1`, `XAUTHORITY`, `XDG_RUNTIME_DIR`, and
the session DBus address when launching GNOME Web as `ga`.

Use `xwd` plus ImageMagick to capture the browser window. Login coordinates are
defined for the environment's actual 1920x1080 display and must be checked from
a live screenshot whenever the pinned GitLab version changes.

## Validation gotchas

- Check both HTTP 200 and `.status == "ok"` from `/-/readiness`.
- Probe `/-/readiness` through `127.0.0.1` inside the GitLab container. The
  guest reaches Docker through its bridge address, which is intentionally not
  in GitLab's default monitoring allowlist and therefore receives HTTP 404.
- Validate the setup token through `/api/v4/user` before importing anything.
- Reset the target issue through the API in the task hook, including original
  labels, assignees, due date, state, and exact prior task comments.
- Keep the verifier as the framework compatibility stub; benchmark verification
  is external and screenshot/VLM based.
