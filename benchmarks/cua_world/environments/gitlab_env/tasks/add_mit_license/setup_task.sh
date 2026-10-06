#!/bin/bash
# pre_task: restore the exact no-license baseline and open the authenticated
# Projects dashboard shown at the start of the video.
set -euo pipefail

echo "=== Preparing add_mit_license episode ==="
export DISPLAY=:1
export XAUTHORITY=/home/ga/.Xauthority
BASELINE_COMMIT=218b5e72424aca8b580e52342dbb92bd4bd076c8
SEED_REPO=/home/ga/gitlab_seed/dotfiles
GITLAB_REMOTE='http://byteblaze:V9xQ4mL2pR7s%21@localhost:8929/byteblaze/dotfiles.git'

# A post-start disk checkpoint reboots the VM, so Docker may still be bringing
# GitLab back online when pre_task begins. Wait for the real endpoint.
gitlab_ready=false
for attempt in $(seq 1 180); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8929/users/sign_in 2>/dev/null || true)
  if [ "$code" = "200" ]; then
    gitlab_ready=true
    break
  fi
  sleep 2
done
if [ "$gitlab_ready" != "true" ]; then
  echo "ERROR: GitLab was not ready for task reset"
  exit 1
fi

# Restore main so repeated episodes never inherit a previously created LICENSE.
git -C "$SEED_REPO" checkout -f "$BASELINE_COMMIT"
git -C "$SEED_REPO" branch -f main "$BASELINE_COMMIT"

# GitLab may asynchronously protect the default branch after a browser commit.
# That is compatible with normal owner edits but rejects the force-push needed
# to restore an episode. Remove protection only from this task's main branch
# immediately before the deterministic reset.
docker exec gitlab-ce gitlab-rails runner \
  "p=Project.find_by_full_path('byteblaze/dotfiles'); raise 'missing project' unless p; p.protected_branches.where(name: 'main').destroy_all; puts 'MAIN_RESET_READY'" \
  | tee /tmp/gitlab_task_branch_reset.log
grep -q 'MAIN_RESET_READY' /tmp/gitlab_task_branch_reset.log

git -C "$SEED_REPO" push --force "$GITLAB_REMOTE" main:main

actual_sha=$(git ls-remote "$GITLAB_REMOTE" refs/heads/main | awk '{print $1}')
if [ "$actual_sha" != "$BASELINE_COMMIT" ]; then
  echo "ERROR: failed to restore baseline branch"
  exit 1
fi
if git -C "$SEED_REPO" ls-tree -r --name-only "$BASELINE_COMMIT" | grep -qE '(^|/)(LICENSE|LICENSE\.md)$'; then
  echo "ERROR: baseline unexpectedly contains a license file"
  exit 1
fi

# Use a fresh, task-owned profile each episode. This removes stale auth and UI
# state while leaving all unrelated user files untouched.
# Chrome's real process executable is /opt/google/chrome/chrome (the
# google-chrome command is only a wrapper). Terminate every process from the
# previous task-owned profile and wait for its DevTools endpoint to disappear;
# otherwise a later episode can authenticate an old hidden window while the
# newly visible window remains at the sign-in page.
pkill -u ga -TERM -f '/opt/google/chrome/chrome' 2>/dev/null || true
for attempt in $(seq 1 20); do
  if ! pgrep -u ga -f '/opt/google/chrome/chrome' >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
pkill -u ga -KILL -f '/opt/google/chrome/chrome' 2>/dev/null || true
for attempt in $(seq 1 20); do
  if ! curl -fsS http://127.0.0.1:9222/json >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
PROFILE=/home/ga/.config/gitlab-task-chrome
if [ -d "$PROFILE" ]; then
  find "$PROFILE" -mindepth 1 -delete
else
  mkdir -p "$PROFILE"
fi
chown -R ga:ga "$PROFILE"

su - ga -c "setsid env \
  DISPLAY=:1 \
  XAUTHORITY=/home/ga/.Xauthority \
  XDG_RUNTIME_DIR=/run/user/1000 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  google-chrome \
  --user-data-dir=$PROFILE \
  --remote-debugging-port=9222 \
  --remote-allow-origins='*' \
  --no-first-run \
  --no-default-browser-check \
  --password-store=basic \
  --disable-search-engine-choice-screen \
  --disable-session-crashed-bubble \
  --start-maximized \
  http://localhost:8929/users/sign_in \
  > /tmp/gitlab_chrome.log 2>&1 &"

for attempt in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:9222/json >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:9222/json >/dev/null

python3 /workspace/scripts/chrome_cdp.py \
  'http://localhost:8929/dashboard/projects?sort=name_asc'

# Chrome 154 may still display a native save-password bubble even when the
# managed PasswordManagerEnabled policy is false. Dismiss that browser chrome
# prompt after the login navigation has completed.
xdotool key Escape 2>/dev/null || true
sleep 1

for attempt in $(seq 1 30); do
  window_id=$(xdotool search --onlyvisible --class 'google-chrome' 2>/dev/null | tail -1 || true)
  if [ -n "$window_id" ]; then
    xdotool windowactivate "$window_id" || true
    wmctrl -i -r "$window_id" -b add,maximized_vert,maximized_horz || true
    break
  fi
  sleep 1
done

window_title=$(xdotool getwindowname "$window_id" 2>/dev/null || true)
if [[ "$window_title" != *"Projects · GitLab"* ]]; then
  echo "ERROR: unexpected task-start window: ${window_title:-none}"
  exit 1
fi

echo "BASELINE_SHA=$actual_sha"
echo "START_URL=http://localhost:8929/dashboard/projects?sort=name_asc"
echo "=== Episode ready ==="
