#!/bin/bash

GITLAB_URL="http://gitlab.local"
GITLAB_ROOT_USER="root"
GITLAB_ROOT_PASSWORD="N7v!4Qz@8Lm#2Rx%"
GITLAB_BROWSER_USER="byteblaze"
GITLAB_BROWSER_PASSWORD="N7v!4Qz@8Lm#2Rx%"
GITLAB_ROOT_TOKEN="gitlab-seed-token123"
GITLAB_MANIFEST="/home/ga/gitlab/seed/seed_manifest.json"

gitlab_api() {
  local method="$1"
  local endpoint="$2"
  shift 2
  curl -fsS -X "$method" -H "PRIVATE-TOKEN: ${GITLAB_ROOT_TOKEN}" \
    "${GITLAB_URL}/api/v4/${endpoint}" "$@"
}

wait_for_gitlab_readiness() {
  local timeout="${1:-300}"
  local elapsed=0
  local code="000"
  while [ "$elapsed" -lt "$timeout" ]; do
    code=$(curl -sS -o /tmp/gitlab_task_readiness.json -w "%{http_code}" \
      "${GITLAB_URL}/-/readiness?all=1" 2>/dev/null || true)
    if [ "$code" = "200" ] && \
      jq -e '.status == "ok"' /tmp/gitlab_task_readiness.json >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  return 1
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

show_gitlab_diagnostics() {
  gitlab-ctl status 2>/dev/null || true
  tail -100 /var/log/gitlab/gitlab-rails/production.log 2>/dev/null || true
  tail -100 /var/log/gitlab/puma/current 2>/dev/null || true
  tail -100 /var/log/gitlab/sidekiq/current 2>/dev/null || true
  tail -100 /var/log/gitlab/nginx/gitlab_error.log 2>/dev/null || true
}

ensure_gitlab_ready() {
  local timeout="${1:-900}"
  if wait_for_gitlab_readiness 30 && wait_for_gitlab_api 30; then
    echo "Native GitLab readiness and root API token are valid"
    return 0
  fi

  echo "GitLab is not responding after cache restore; starting its native services..."
  if ! systemctl start gitlab-runsvdir.service; then
    echo "ERROR: Could not start gitlab-runsvdir.service"
    show_gitlab_diagnostics
    return 1
  fi
  if ! gitlab-ctl start; then
    echo "ERROR: gitlab-ctl start failed"
    show_gitlab_diagnostics
    return 1
  fi
  if wait_for_gitlab_readiness "$timeout" && wait_for_gitlab_api 120; then
    echo "Native GitLab readiness and root API token recovered"
    return 0
  fi

  echo "ERROR: Native GitLab did not become ready with a valid root API token"
  cat /tmp/gitlab_task_readiness.json 2>/dev/null || true
  show_gitlab_diagnostics
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
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool search --onlyvisible --class firefox 2>/dev/null | tail -1
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

wait_for_browser_title() {
  local pattern="$1"
  local timeout="${2:-60}"
  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if browser_title 2>/dev/null | grep -qiE "$pattern"; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 1
}

wait_for_browser_title_without() {
  local required_pattern="$1"
  local excluded_pattern="$2"
  local timeout="${3:-60}"
  local elapsed=0
  local title=""
  while [ "$elapsed" -lt "$timeout" ]; do
    title=$(browser_title 2>/dev/null || true)
    if printf '%s\n' "$title" | grep -qiE "$required_pattern" && \
      ! printf '%s\n' "$title" | grep -qiE "$excluded_pattern"; then
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
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool windowactivate --sync "$wid"
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key ctrl+l
  sleep 0.3
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool type --delay 35 "$url"
  sleep 0.3
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key Return
}

prepare_firefox_profile() {
  local profile="/home/ga/.mozilla/firefox/gitlab-benchmark"
  install -d -m 0700 -o ga -g ga "$profile"
  cat > "$profile/user.js" <<'PREFSEOF'
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("security.insecure_field_warning.contextual.enabled", false);
user_pref("signon.rememberSignons", false);
PREFSEOF
  chown ga:ga "$profile/user.js"
  install -d -m 0755 /etc/firefox/policies
  cat > /etc/firefox/policies/policies.json <<'POLICYEOF'
{"policies":{"OfferToSaveLogins":false,"PasswordManagerEnabled":false}}
POLICYEOF
}

start_browser() {
  local url="$1"
  pkill -TERM -x firefox 2>/dev/null || true
  sleep 2
  pkill -KILL -x firefox 2>/dev/null || true

  local xauth
  local profile="/home/ga/.mozilla/firefox/gitlab-benchmark"
  xauth=$(xauthority_path)
  prepare_firefox_profile
  su - ga -c "setsid env DISPLAY=:1 XAUTHORITY='${xauth}' XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus firefox --no-remote --profile '${profile}' --private-window '${url}' >/tmp/firefox_gitlab.log 2>&1 </dev/null &"
  wait_for_browser_window 60
  focus_browser
}

login_gitlab_browser() {
  local target_url="$1"
  local expected_title_pattern="${2:-(Issues|Work items).*GitLab|GitLab.*(Issues|Work items)}"
  local wid
  local username_x
  local username_y

  start_browser "${GITLAB_URL}/users/sign_in" || return 1
  if wait_for_browser_title 'Sign in.*GitLab|GitLab.*Sign in' 60; then
    wid=$(browser_window_id)
    [ -n "$wid" ] || return 1

    # The GitLab sign-in page does not reliably autofocus its username field.
    # Target it relative to the pinned Firefox window, then send paced input to
    # the active window. Direct --window key events can be dropped by Firefox.
    focus_browser
    DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool windowactivate --sync "$wid"
    sleep 2
    eval "$(DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool getwindowgeometry --shell "$wid")"
    username_x=$((WIDTH / 2))
    username_y=$((HEIGHT * 32 / 100))
    DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool mousemove --sync --window "$wid" "$username_x" "$username_y" click 1
    sleep 0.5
    DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key ctrl+a
    DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool type --delay 100 "$GITLAB_BROWSER_USER"
    sleep 0.5
    DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key Tab
    sleep 0.5
    DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key ctrl+a
    DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool type --delay 100 "$GITLAB_BROWSER_PASSWORD"
    sleep 0.5
    # Dismiss Firefox's HTTP password warning while retaining field focus.
    DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key Escape
    sleep 0.5
    DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key Return
    wait_for_browser_title_without 'GitLab' 'Sign in' 90 || return 1
  elif ! wait_for_browser_title_without 'GitLab' 'Sign in' 5; then
    return 1
  fi

  navigate_browser "$target_url"
  wait_for_browser_title "$expected_title_pattern" 90 || return 1
  # GitLab can show a first-visit Work Items tour over the seeded issue list.
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" xdotool key Escape
  sleep 0.5
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
