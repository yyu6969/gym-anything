#!/bin/bash
set -euo pipefail

echo "=== Setting up native GitLab Community Edition ==="

GITLAB_DIR="/home/ga/gitlab"
GITLAB_URL="http://gitlab.local"
ROOT_TOKEN="gitlab-seed-token123"
ROOT_PASSWORD='N7v!4Qz@8Lm#2Rx%'
DATASET_DIR="/workspace/gitlab_shared"
SEED_DIR="${GITLAB_DIR}/seed"
SEED_WORK_DIR="/var/opt/gitlab/gym-anything-seed"
USER_PASSWORD="$ROOT_PASSWORD"

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

echo "Preparing the deterministic root account and shared baseline..."
# Puma and Sidekiq are quiesced while the Rails runner restores 3.2 GB of
# canonical Git data. This keeps memory bounded and prevents background jobs
# from observing a partially constructed baseline.
gitlab-ctl stop sidekiq
gitlab-ctl stop puma
admin_status=0
gitlab-rails runner \
  "user = User.find_by_username('root'); user.password = '${ROOT_PASSWORD}'; user.password_confirmation = '${ROOT_PASSWORD}'; user.save!; user.personal_access_tokens.where(name: 'gym-anything-seed').delete_all; token = user.personal_access_tokens.create(scopes: ['api', 'sudo'], name: 'gym-anything-seed', expires_at: 365.days.from_now); token.set_token('${ROOT_TOKEN}'); token.save!" \
  || admin_status=$?
if [ "$admin_status" -ne 0 ]; then
  echo "ERROR: Native GitLab Rails setup failed with status ${admin_status}"
  show_gitlab_diagnostics
  exit "$admin_status"
fi

mkdir -p "$GITLAB_DIR"
rm -rf "$SEED_DIR" "$SEED_WORK_DIR"
mkdir -p "$SEED_DIR" "$SEED_WORK_DIR"
chown -R ga:ga "$GITLAB_DIR"
chown git:git "$SEED_WORK_DIR"

if [ ! -r "$DATASET_DIR/manifest.json" ]; then
  echo "ERROR: Extracted GitLab dataset is not mounted at ${DATASET_DIR}"
  exit 1
fi

echo "Validating the extracted logical dataset and all 28 bundles..."
python3 /workspace/scripts/prepare_shared_baseline.py \
  --dataset "$DATASET_DIR" \
  --output "$SEED_WORK_DIR/dataset_preflight.json"

echo "Constructing the shared baseline through GitLab Rails and RepoRestorer..."
seed_status=0
GITALY_DISABLE_REQUEST_LIMITS=1 GITLAB_SEED_USER_PASSWORD="$USER_PASSWORD" \
  gitlab-rails runner /workspace/scripts/seed_gitlab.rb "$DATASET_DIR" "$SEED_WORK_DIR" \
  || seed_status=$?
if [ "$seed_status" -ne 0 ]; then
  echo "ERROR: Shared baseline construction failed with status ${seed_status}"
  show_gitlab_diagnostics
  exit "$seed_status"
fi

gitlab-ctl start puma
gitlab-ctl start sidekiq
if ! wait_for_gitlab_readiness 900; then
  echo "ERROR: GitLab did not recover after shared baseline construction"
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

echo "Validating the live shared baseline and declared absences..."
python3 /workspace/scripts/validate_shared_baseline.py \
  --dataset "$DATASET_DIR" \
  --seed-manifest "$SEED_WORK_DIR/seed_manifest.json" \
  --base-url "$GITLAB_URL" \
  --token "$ROOT_TOKEN" \
  --output "$SEED_WORK_DIR/baseline_validation.json"
install -o ga -g ga -m 0644 "$SEED_WORK_DIR"/*.json "$SEED_DIR"/

chown -R ga:ga "$SEED_DIR"
chmod 0644 "$SEED_DIR"/*.json

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
if ! login_gitlab_browser "${GITLAB_URL}/dashboard/projects" '(Projects|Your work).*GitLab|GitLab.*(Projects|Your work)'; then
  echo "ERROR: Firefox did not reach an authenticated GitLab dashboard"
  echo "Browser title: $(browser_title 2>/dev/null || echo unavailable)"
  cat /tmp/firefox_gitlab.log 2>/dev/null || true
  exit 1
fi

GITLAB_VERSION=$(dpkg-query -W -f='${Version}' gitlab-ce)
echo "GitLab version: ${GITLAB_VERSION}"
echo "GitLab URL: ${GITLAB_URL}"
echo "Browser login: byteblaze / ${USER_PASSWORD}"
echo "Administrative API login: root / ${ROOT_PASSWORD}"
echo "Dataset preflight: ${SEED_DIR}/dataset_preflight.json"
echo "Seed manifest: ${SEED_DIR}/seed_manifest.json"
echo "Baseline validation: ${SEED_DIR}/baseline_validation.json"
echo "Environment Ready: shared GitLab baseline validated"
echo "=== Native GitLab setup complete ==="
