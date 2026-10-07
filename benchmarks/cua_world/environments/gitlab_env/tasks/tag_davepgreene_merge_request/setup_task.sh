#!/bin/bash
# pre_task: restore the real upstream branches and merge request, remove any
# prior episode's mention, then open the authenticated Projects dashboard shown
# at the beginning of the demonstration.
set -euo pipefail

echo "=== Preparing tag_davepgreene_merge_request episode ==="
export DISPLAY=:1
export XAUTHORITY=/home/ga/.Xauthority

GITLAB_URL=http://localhost:8929
PROJECT_PATH=byteblaze/a11y-webring-club
VIDEO_MAIN=83d965d99adaf5adaee708fb093fa81063cd0db2
VIDEO_SOURCE=4817a445d1b74904bd695059aea63705370f9205
SOURCE_BRANCH=github/fork/davepgreene/add-verification-function
SEED_REPO=/home/ga/gitlab_seed/a11y-webring
SOURCE_BUNDLE=/workspace/assets/a11y-webring.bundle
GITLAB_REMOTE='http://byteblaze:V9xQ4mL2pR7s%21@localhost:8929/byteblaze/a11y-webring-club.git'
DISTRACTOR_PROJECT_PATH=the-a11y-project/a11yproject-com
DISTRACTOR_MAIN=a303bf71be0a2673f1a81096291e0e963dd80e0d
DISTRACTOR_1485=352da2a0bb2ff347bdb0ffe0007f26afc1aebd2c
DISTRACTOR_1270=7b5c8cbff2aff1b474e3b3877671b83bdc6a734b
DISTRACTOR_BRANCH_1485=github/fork/Roshanjossey/1478-fix-404-urls
DISTRACTOR_BRANCH_1270=github/fork/agch-dev/feat/add-wcag-levels
DISTRACTOR_REPO=/home/ga/gitlab_seed/a11yproject-com
DISTRACTOR_BUNDLE=/workspace/assets/a11yproject-distractors.bundle
DISTRACTOR_REMOTE='http://byteblaze:V9xQ4mL2pR7s%21@localhost:8929/the-a11y-project/a11yproject-com.git'

# A post-start disk checkpoint reboots the VM, so Docker may still be bringing
# GitLab online when pre_task begins. Poll the real HTTP endpoint and Rails.
gitlab_ready=false
for attempt in $(seq 1 180); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$GITLAB_URL/users/sign_in" 2>/dev/null || true)
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

rails_ready=false
for attempt in $(seq 1 60); do
  if docker exec gitlab-ce gitlab-rails runner 'puts "RAILS_READY"' 2>/dev/null | grep -q RAILS_READY; then
    rails_ready=true
    break
  fi
  sleep 2
done
if [ "$rails_ready" != "true" ]; then
  echo "ERROR: GitLab Rails was not ready for task reset"
  exit 1
fi

docker cp \
  /workspace/tasks/tag_davepgreene_merge_request/seed_merge_request.rb \
  gitlab-ce:/tmp/seed_tag_davepgreene_merge_request.rb

docker exec -e TASK_SEED_PHASE=prepare gitlab-ce \
  gitlab-rails runner /tmp/seed_tag_davepgreene_merge_request.rb \
  | tee /tmp/gitlab_task_mr_prepare.log
grep -q 'TASK_MR_PREPARE_OK' /tmp/gitlab_task_mr_prepare.log

# Recreate both sides of the merge request from a self-contained bundle of the
# real public repository. Force-pushing makes the episode deterministic even if
# a previous interaction modified or deleted a branch.
mkdir -p "$SEED_REPO"
if [ ! -d "$SEED_REPO/.git" ]; then
  git -C "$SEED_REPO" init
fi
git -C "$SEED_REPO" fetch --force "$SOURCE_BUNDLE" \
  refs/heads/video-main:refs/remotes/asset/video-main \
  refs/heads/video-source:refs/remotes/asset/video-source
git -C "$SEED_REPO" checkout -f -B main "$VIDEO_MAIN"
git -C "$SEED_REPO" branch -f "$SOURCE_BRANCH" "$VIDEO_SOURCE"
git -C "$SEED_REPO" config user.name 'Byte Blaze'
git -C "$SEED_REPO" config user.email 'byteblaze@example.test'
git -C "$SEED_REPO" remote remove gitlab 2>/dev/null || true
git -C "$SEED_REPO" remote add gitlab "$GITLAB_REMOTE"
git -C "$SEED_REPO" push --force gitlab main:main
git -C "$SEED_REPO" push --force gitlab \
  "$SOURCE_BRANCH:refs/heads/$SOURCE_BRANCH"

# Reconstruct the two authentic A11Y Project pull requests that appear beside
# the target in the video's Assigned list. Their branches and commit objects
# come from the upstream public repository bundle.
mkdir -p "$DISTRACTOR_REPO"
if [ ! -d "$DISTRACTOR_REPO/.git" ]; then
  git -C "$DISTRACTOR_REPO" init
fi
git -C "$DISTRACTOR_REPO" fetch --force "$DISTRACTOR_BUNDLE" \
  refs/heads/video-main:refs/remotes/asset/video-main \
  refs/heads/pr-1485:refs/remotes/asset/pr-1485 \
  refs/heads/pr-1270:refs/remotes/asset/pr-1270
git -C "$DISTRACTOR_REPO" checkout -f -B main "$DISTRACTOR_MAIN"
git -C "$DISTRACTOR_REPO" branch -f "$DISTRACTOR_BRANCH_1485" "$DISTRACTOR_1485"
git -C "$DISTRACTOR_REPO" branch -f "$DISTRACTOR_BRANCH_1270" "$DISTRACTOR_1270"
git -C "$DISTRACTOR_REPO" config user.name 'Byte Blaze'
git -C "$DISTRACTOR_REPO" config user.email 'byteblaze@example.test'
git -C "$DISTRACTOR_REPO" remote remove gitlab 2>/dev/null || true
git -C "$DISTRACTOR_REPO" remote add gitlab "$DISTRACTOR_REMOTE"
git -C "$DISTRACTOR_REPO" push --force gitlab main:main
git -C "$DISTRACTOR_REPO" push --force gitlab \
  "$DISTRACTOR_BRANCH_1485:refs/heads/$DISTRACTOR_BRANCH_1485"
git -C "$DISTRACTOR_REPO" push --force gitlab \
  "$DISTRACTOR_BRANCH_1270:refs/heads/$DISTRACTOR_BRANCH_1270"

actual_main=$(git ls-remote "$GITLAB_REMOTE" refs/heads/main | awk '{print $1}')
actual_source=$(git ls-remote "$GITLAB_REMOTE" "refs/heads/$SOURCE_BRANCH" | awk '{print $1}')
if [ "$actual_main" != "$VIDEO_MAIN" ] || [ "$actual_source" != "$VIDEO_SOURCE" ]; then
  echo "ERROR: failed to restore merge-request branches"
  exit 1
fi

actual_distractor_main=$(git ls-remote "$DISTRACTOR_REMOTE" refs/heads/main | awk '{print $1}')
actual_distractor_1485=$(git ls-remote "$DISTRACTOR_REMOTE" "refs/heads/$DISTRACTOR_BRANCH_1485" | awk '{print $1}')
actual_distractor_1270=$(git ls-remote "$DISTRACTOR_REMOTE" "refs/heads/$DISTRACTOR_BRANCH_1270" | awk '{print $1}')
if [ "$actual_distractor_main" != "$DISTRACTOR_MAIN" ] || \
   [ "$actual_distractor_1485" != "$DISTRACTOR_1485" ] || \
   [ "$actual_distractor_1270" != "$DISTRACTOR_1270" ]; then
  echo "ERROR: failed to restore assigned-list distractor branches"
  exit 1
fi

docker exec -e TASK_SEED_PHASE=finalize gitlab-ce \
  gitlab-rails runner /tmp/seed_tag_davepgreene_merge_request.rb \
  | tee /tmp/gitlab_task_mr_finalize.log
grep -q 'TASK_MR_READY' /tmp/gitlab_task_mr_finalize.log

# Let GitLab finish the asynchronous new-MR work, then reconcile terminal
# pipeline states and historical timestamps once more for deterministic UI.
sleep 5
docker exec -e TASK_SEED_PHASE=settle gitlab-ce \
  gitlab-rails runner /tmp/seed_tag_davepgreene_merge_request.rb \
  | tee /tmp/gitlab_task_mr_settle.log
grep -q 'TASK_MR_SETTLED' /tmp/gitlab_task_mr_settle.log
chown -R ga:ga "$SEED_REPO" "$DISTRACTOR_REPO"

# Start a clean authenticated browser session on the same Projects dashboard
# used as the entry point in the demonstration.
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

PROFILE=/home/ga/.config/gitlab-tag-task-chrome
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
  $GITLAB_URL/users/sign_in \
  > /tmp/gitlab_chrome.log 2>&1 &"

for attempt in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:9222/json >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:9222/json >/dev/null

python3 /workspace/scripts/chrome_cdp.py \
  "$GITLAB_URL/dashboard/projects?sort=name_asc"

# Dismiss any native password bubble after login, then focus and maximize the
# visible Chrome window.
xdotool key Escape 2>/dev/null || true
sleep 1

window_id=""
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

echo "PROJECT=$PROJECT_PATH"
echo "MERGE_REQUEST=!40 Add verification functions"
echo "ASSIGNEE=byteblaze"
echo "MENTION_TARGET=@davepgreene"
echo "ASSIGNED_OPEN_MERGE_REQUESTS=3"
echo "DISTRACTORS=!1485 update or remove 404 links; !1270 feat: add WCAG levels"
echo "MAIN_SHA=$actual_main"
echo "SOURCE_SHA=$actual_source"
echo "DISTRACTOR_MAIN_SHA=$actual_distractor_main"
echo "DISTRACTOR_1485_SHA=$actual_distractor_1485"
echo "DISTRACTOR_1270_SHA=$actual_distractor_1270"
echo "START_URL=$GITLAB_URL/dashboard/projects?sort=name_asc"
echo "=== Episode ready ==="
