#!/bin/bash
set -euo pipefail

echo "=== Setting up native GitLab Community Edition ==="

GITLAB_DIR="/home/ga/gitlab"
GITLAB_URL="http://gitlab.local"
ROOT_TOKEN="gitlab-seed-token123"
ROOT_PASSWORD='N7v!4Qz@8Lm#2Rx%'

wait_for_gitlab_readiness() {
  local timeout="${1:-900}"
  local elapsed=0
  local code="000"
  while [ "$elapsed" -lt "$timeout" ]; do
    code=$(curl -sS -o /tmp/gitlab_readiness.json -w "%{http_code}" \
      "${GITLAB_URL}/-/readiness?all=1" 2>/dev/null || true)
    if [ "$code" = "200" ] && \
      jq -e '.status == "ok"' /tmp/gitlab_readiness.json >/dev/null 2>&1; then
      echo "GitLab readiness returned HTTP 200 after ${elapsed}s"
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
    if [ $((elapsed % 60)) -eq 0 ]; then
      echo "  waiting for native GitLab... ${elapsed}s (HTTP ${code})"
      gitlab-ctl status 2>/dev/null || true
    fi
  done
  echo "ERROR: GitLab did not become ready within ${timeout}s (HTTP ${code})"
  cat /tmp/gitlab_readiness.json 2>/dev/null || true
  return 1
}

show_gitlab_diagnostics() {
  gitlab-ctl status 2>/dev/null || true
  tail -100 /var/log/gitlab/gitlab-rails/production.log 2>/dev/null || true
  tail -100 /var/log/gitlab/puma/current 2>/dev/null || true
  tail -100 /var/log/gitlab/sidekiq/current 2>/dev/null || true
  tail -100 /var/log/gitlab/nginx/gitlab_error.log 2>/dev/null || true
}

if ! grep -qE '(^|[[:space:]])gitlab\.local([[:space:]]|$)' /etc/hosts; then
  echo "127.0.0.1 gitlab.local" >> /etc/hosts
fi

systemctl start gitlab-runsvdir.service
gitlab-ctl start
if ! wait_for_gitlab_readiness 900; then
  show_gitlab_diagnostics
  exit 1
fi

echo "Configuring the deterministic root account, API token, and repository-by-URL import..."
# A second Rails process exceeds this guest's memory while Puma and Sidekiq are
# warm. Quiesce only those two application processes for this one-time admin
# operation, then restore the complete service stack before seeding.
gitlab-ctl stop sidekiq
gitlab-ctl stop puma
admin_status=0
gitlab-rails runner \
  "user = User.find_by_username('root'); user.password = '${ROOT_PASSWORD}'; user.password_confirmation = '${ROOT_PASSWORD}'; user.save!; settings = ApplicationSetting.current; settings.update!(import_sources: (Array(settings.import_sources) | ['git'])); user.personal_access_tokens.where(name: 'gym-anything-seed').each(&:revoke!); token = user.personal_access_tokens.create(scopes: ['api'], name: 'gym-anything-seed', expires_at: 365.days.from_now); token.set_token('${ROOT_TOKEN}'); token.save!" \
  || admin_status=$?
gitlab-ctl start puma
gitlab-ctl start sidekiq
if [ "$admin_status" -ne 0 ]; then
  echo "ERROR: Native GitLab Rails setup failed with status ${admin_status}"
  show_gitlab_diagnostics
  exit "$admin_status"
fi

if ! wait_for_gitlab_readiness 900; then
  echo "ERROR: GitLab did not recover after the Rails setup operation"
  show_gitlab_diagnostics
  exit 1
fi

echo "Validating root API token..."
for attempt in $(seq 1 30); do
  code=$(curl -sS -o /tmp/gitlab_root_user.json -w "%{http_code}" \
    -H "PRIVATE-TOKEN: ${ROOT_TOKEN}" "${GITLAB_URL}/api/v4/user" 2>/dev/null || true)
  if [ "$code" = "200" ] && [ "$(jq -r '.username // empty' /tmp/gitlab_root_user.json)" = "root" ]; then
    echo "Root API token is valid"
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    echo "ERROR: Root API token was not accepted (HTTP ${code})"
    cat /tmp/gitlab_root_user.json 2>/dev/null || true
    exit 1
  fi
  sleep 3
done

existing_project=""
if existing_project=$(curl -fsS -H "PRIVATE-TOKEN: ${ROOT_TOKEN}" \
  "${GITLAB_URL}/api/v4/projects/root%2Fgitlab-cli" 2>/dev/null); then
  existing_project_id=$(jq -r '.id' <<<"$existing_project")
  echo "Removing existing seeded project id=${existing_project_id} before rebuilding the baseline..."
  curl -fsS -X DELETE -H "PRIVATE-TOKEN: ${ROOT_TOKEN}" \
    "${GITLAB_URL}/api/v4/projects/${existing_project_id}" >/dev/null
  for attempt in $(seq 1 60); do
    if ! curl -fsS -H "PRIVATE-TOKEN: ${ROOT_TOKEN}" \
      "${GITLAB_URL}/api/v4/projects/${existing_project_id}" >/dev/null 2>&1; then
      break
    fi
    if [ "$attempt" -eq 60 ]; then
      echo "ERROR: Existing seeded project was not deleted"
      exit 1
    fi
    sleep 2
  done
fi

mkdir -p "$GITLAB_DIR"
rm -rf "$GITLAB_DIR/seed"
mkdir -p "$GITLAB_DIR/seed"
chown -R ga:ga "$GITLAB_DIR"

echo "Importing the real GitLab CLI repository and real issue records..."
python3 /workspace/scripts/seed_gitlab.py \
  --base-url "$GITLAB_URL" \
  --token "$ROOT_TOKEN" \
  --output-dir "$GITLAB_DIR/seed"

chown -R ga:ga "$GITLAB_DIR/seed"
chmod 0644 "$GITLAB_DIR/seed/seed_manifest.json" "$GITLAB_DIR/seed/source_snapshot.json"

mkdir -p /home/ga/Desktop
cat > /home/ga/Desktop/GitLab.desktop <<'DESKTOPEOF'
[Desktop Entry]
Name=GitLab CE
Comment=Open the local GitLab Community Edition instance
Exec=firefox http://gitlab.local
Icon=applications-development
StartupNotify=true
Terminal=false
Type=Application
Categories=Development;
DESKTOPEOF
chown ga:ga /home/ga/Desktop/GitLab.desktop
chmod 0755 /home/ga/Desktop/GitLab.desktop

echo "Launching Firefox at the canonical GitLab URL..."
source /workspace/scripts/task_utils.sh
if ! start_browser "$GITLAB_URL"; then
  echo "ERROR: Firefox did not open"
  cat /tmp/firefox_gitlab.log 2>/dev/null || true
  exit 1
fi
if ! wait_for_browser_title 'GitLab' 90; then
  echo "ERROR: Firefox did not display a GitLab page"
  echo "Browser title: $(browser_title 2>/dev/null || echo unavailable)"
  cat /tmp/firefox_gitlab.log 2>/dev/null || true
  exit 1
fi

GITLAB_VERSION=$(dpkg-query -W -f='${Version}' gitlab-ce)
echo "GitLab version: ${GITLAB_VERSION}"
echo "GitLab URL: ${GITLAB_URL}"
echo "Login: root / ${ROOT_PASSWORD}"
echo "Seed manifest: ${GITLAB_DIR}/seed/seed_manifest.json"
echo "Source snapshot: ${GITLAB_DIR}/seed/source_snapshot.json"
echo "=== Native GitLab setup complete ==="
