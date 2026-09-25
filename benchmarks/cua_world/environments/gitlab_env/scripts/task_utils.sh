#!/bin/bash

GITLAB_URL="http://gitlab.local"
GITLAB_ROOT_USER="root"
GITLAB_ROOT_PASSWORD="N7v!4Qz@8Lm#2Rx%"
GITLAB_ROOT_TOKEN="gitlab-seed-token123"
GITLAB_MANIFEST="/home/ga/gitlab/seed/seed_manifest.json"
GITLAB_SETUP_STATUS="/var/lib/gym-anything-gitlab/setup.status"
GITLAB_SETUP_LOG="/home/ga/gitlab_setup_background.log"

gitlab_api() {
  local method="$1"
  local endpoint="$2"
  shift 2
  curl -fsS -X "$method" -H "PRIVATE-TOKEN: ${GITLAB_ROOT_TOKEN}" \
    "${GITLAB_URL}/api/v4/${endpoint}" "$@"
}

wait_for_gitlab_api() {
  local timeout="${1:-300}"
  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if gitlab_api GET version >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  return 1
}

wait_for_gitlab_setup() {
  local timeout="${1:-10800}"
  local elapsed=0
  local status="pending"
  while [ "$elapsed" -lt "$timeout" ]; do
    if [ -f "$GITLAB_SETUP_STATUS" ]; then
      status=$(cat "$GITLAB_SETUP_STATUS")
      if [ "$status" = "success" ] && wait_for_gitlab_api 30; then
        echo "GitLab background setup is complete after ${elapsed}s"
        return 0
      fi
      if [[ "$status" == failed:* ]]; then
        echo "ERROR: GitLab background setup failed (${status})"
        tail -200 "$GITLAB_SETUP_LOG" 2>/dev/null || true
        return 1
      fi
    fi

    sleep 10
    elapsed=$((elapsed + 10))
    if [ $((elapsed % 60)) -eq 0 ]; then
      echo "  waiting for GitLab background setup... ${elapsed}s"
      tail -5 "$GITLAB_SETUP_LOG" 2>/dev/null || true
    fi
  done

  echo "ERROR: GitLab background setup did not finish within ${timeout}s"
  tail -200 "$GITLAB_SETUP_LOG" 2>/dev/null || true
  return 1
}

gitlab_project_id() {
  jq -r '.local.project_id' "$GITLAB_MANIFEST"
}

gitlab_local_issue_iid() {
  local source_iid="$1"
  jq -r --argjson source_iid "$source_iid" \
    '.issues[] | select(.source_iid == $source_iid) | .local_iid' "$GITLAB_MANIFEST"
}

gitlab_source_labels_csv() {
  local source_iid="$1"
  jq -r --argjson source_iid "$source_iid" \
    '.issues[] | select(.source_iid == $source_iid) | .source_labels | join(",")' "$GITLAB_MANIFEST"
}

xauthority_path() {
  if [ -s /run/user/1000/gdm/Xauthority ]; then
    printf '%s\n' /run/user/1000/gdm/Xauthority
  else
    printf '%s\n' /home/ga/.Xauthority
  fi
}

browser_window_id() {
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool search --onlyvisible --class 'Epiphany' 2>/dev/null | tail -1
}

wait_for_browser_window() {
  local timeout="${1:-60}"
  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if [ -n "$(browser_window_id)" ]; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 1
}

focus_browser() {
  local wid
  wid=$(browser_window_id)
  [ -n "$wid" ] || return 1
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" wmctrl -ia "$wid" 2>/dev/null || true
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" wmctrl -ir "$wid" -b add,maximized_vert,maximized_horz 2>/dev/null || true
}

browser_title() {
  local wid
  wid=$(browser_window_id)
  [ -n "$wid" ] || return 1
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool getwindowname "$wid" 2>/dev/null
}

navigate_browser() {
  local url="$1"
  local wid
  wid=$(browser_window_id)
  [ -n "$wid" ] || return 1
  focus_browser
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key --window "$wid" ctrl+l
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool type --window "$wid" --delay 1 "$url"
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key --window "$wid" Return
}

start_browser() {
  local url="$1"
  pkill -TERM -f '/usr/bin/epiphany-browser' 2>/dev/null || true
  sleep 2
  pkill -KILL -f '/usr/bin/epiphany-browser' 2>/dev/null || true

  local xauth
  xauth=$(xauthority_path)
  su - ga -c "setsid env DISPLAY=:1 XAUTHORITY='${xauth}' XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus epiphany-browser '${url}' >/tmp/epiphany_gitlab.log 2>&1 </dev/null &"
  wait_for_browser_window 60
  focus_browser
}

login_gitlab_browser() {
  local target_url="$1"
  local wid

  start_browser "${GITLAB_URL}/users/sign_in" || return 1
  sleep 8
  wid=$(browser_window_id)
  [ -n "$wid" ] || return 1

  # GitLab 18 sign-in form at 1920x1080. Explicit clicks avoid browser chrome tab-order drift.
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool mousemove --window "$wid" 960 410 click 1
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key --window "$wid" ctrl+a
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool type --window "$wid" --delay 35 "$GITLAB_ROOT_USER"
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool mousemove --window "$wid" 960 490 click 1
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key --window "$wid" ctrl+a
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool type --window "$wid" --delay 35 "$GITLAB_ROOT_PASSWORD"
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key --window "$wid" Return
  sleep 10

  navigate_browser "$target_url"
  sleep 10
  focus_browser

  if browser_title | grep -qi 'sign in'; then
    echo "ERROR: Browser remained on the GitLab sign-in page"
    return 1
  fi
  return 0
}

capture_browser_window() {
  local output="$1"
  local wid
  wid=$(browser_window_id)
  [ -n "$wid" ] || return 1
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xwd -silent -id "$wid" -out /tmp/gitlab_browser.xwd
  convert /tmp/gitlab_browser.xwd "$output"
  chmod 0666 "$output" 2>/dev/null || true
}
