#!/bin/bash
set -euo pipefail

echo "=== Setting up GitLab Community Edition ==="

GITLAB_DIR="/home/ga/gitlab"
COMPOSE_SOURCE="/workspace/config/docker-compose.yml"
GITLAB_URL="http://gitlab.local"
ROOT_TOKEN="gitlab-seed-token123"
GITLAB_IMAGE="gitlab/gitlab-ce:18.11.12-ce.0"
IMAGE_PULL_DIR="/var/lib/gym-anything-gitlab"
SETUP_STATUS_FILE="${IMAGE_PULL_DIR}/setup.status"
SETUP_PID_FILE="${IMAGE_PULL_DIR}/setup.pid"
SETUP_LOG="/home/ga/gitlab_setup_background.log"

wait_for_docker() {
  local elapsed=0
  local timeout=120
  while [ "$elapsed" -lt "$timeout" ]; do
    if docker info >/dev/null 2>&1; then
      echo "Docker daemon is ready"
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  echo "ERROR: Docker daemon did not become ready within ${timeout}s"
  return 1
}

wait_for_gitlab_image() {
  local elapsed=0
  local timeout=1800
  local status="pending"
  while [ "$elapsed" -lt "$timeout" ]; do
    if docker image inspect "$GITLAB_IMAGE" >/dev/null 2>&1; then
      echo "Pinned GitLab CE image is available after ${elapsed}s"
      return 0
    fi

    if [ -f "$IMAGE_PULL_DIR/image-pull.status" ]; then
      status=$(cat "$IMAGE_PULL_DIR/image-pull.status")
      if [[ "$status" == failed:* ]]; then
        echo "ERROR: GitLab image pull failed (${status})"
        tail -200 "$IMAGE_PULL_DIR/image-pull.log" 2>/dev/null || true
        return 1
      fi
    fi

    sleep 10
    elapsed=$((elapsed + 10))
    if [ $((elapsed % 60)) -eq 0 ]; then
      echo "  waiting for GitLab image... ${elapsed}s"
      tail -5 "$IMAGE_PULL_DIR/image-pull.log" 2>/dev/null || true
    fi
  done

  echo "ERROR: GitLab image was not available within ${timeout}s"
  tail -200 "$IMAGE_PULL_DIR/image-pull.log" 2>/dev/null || true
  return 1
}

wait_for_gitlab() {
  local elapsed=0
  local timeout=5400
  local code="000"
  while [ "$elapsed" -lt "$timeout" ]; do
    # GitLab deliberately returns 404 for monitoring endpoints requested from
    # an address outside its allowlist. Probe from loopback inside the
    # container, as documented for local health checks.
    code=$(docker exec gitlab curl -sS -o /dev/null -w "%{http_code}" \
      "http://127.0.0.1/-/readiness" 2>/dev/null || true)
    if [ "$code" = "200" ] && \
      docker exec gitlab curl -fsS "http://127.0.0.1/-/readiness" \
        > /tmp/gitlab_readiness.json 2>/dev/null && \
      jq -e '.status == "ok"' /tmp/gitlab_readiness.json >/dev/null 2>&1; then
      echo "GitLab readiness endpoint returned HTTP 200 after ${elapsed}s"
      return 0
    fi
    sleep 10
    elapsed=$((elapsed + 10))
    if [ $((elapsed % 60)) -eq 0 ]; then
      echo "  waiting for GitLab... ${elapsed}s (HTTP ${code})"
      docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' gitlab 2>/dev/null || true
    fi
  done
  echo "ERROR: GitLab did not become ready within ${timeout}s"
  docker logs --tail 200 gitlab || true
  return 1
}

if [ ! -f "$COMPOSE_SOURCE" ]; then
  echo "ERROR: Missing Docker Compose file: ${COMPOSE_SOURCE}"
  exit 1
fi

if [ "${1:-}" != "--background-worker" ]; then
  wait_for_docker
  mkdir -p "$IMAGE_PULL_DIR"

  if [ -f "$SETUP_STATUS_FILE" ] && [ "$(cat "$SETUP_STATUS_FILE")" = "success" ]; then
    echo "GitLab background setup is already complete"
    exit 0
  fi

  if [ -f "$SETUP_PID_FILE" ]; then
    setup_pid=$(cat "$SETUP_PID_FILE")
    if [[ "$setup_pid" =~ ^[0-9]+$ ]] && kill -0 "$setup_pid" 2>/dev/null; then
      echo "GitLab background setup is already running (PID ${setup_pid})"
      exit 0
    fi
  fi

  rm -f "$SETUP_STATUS_FILE"
  chown ga:ga "$GITLAB_DIR" 2>/dev/null || true
  echo "Starting GitLab setup in a detached worker..."
  setsid nohup bash "$0" --background-worker > "$SETUP_LOG" 2>&1 </dev/null &
  echo "$!" > "$SETUP_PID_FILE"
  echo "GitLab setup worker PID: $!"
  echo "GitLab setup log: ${SETUP_LOG}"
  echo "=== GitLab post-start launcher complete ==="
  exit 0
fi

mark_setup_failed() {
  local status=$?
  printf 'failed:%s\n' "$status" > "$SETUP_STATUS_FILE"
  echo "ERROR: GitLab background setup failed with status ${status}"
}
trap mark_setup_failed ERR

echo "=== GitLab background setup worker started ==="
wait_for_docker
wait_for_gitlab_image

if ! grep -qE '(^|[[:space:]])gitlab\.local([[:space:]]|$)' /etc/hosts; then
  echo "127.0.0.1 gitlab.local" >> /etc/hosts
fi

mkdir -p "$GITLAB_DIR"
cp "$COMPOSE_SOURCE" "$GITLAB_DIR/docker-compose.yml"

cd "$GITLAB_DIR"
docker compose down --remove-orphans >/tmp/gitlab_compose_down.log 2>&1 || true

# Hooks can be re-run during development; rebuild only this environment's explicit data roots.
rm -rf "$GITLAB_DIR/config" "$GITLAB_DIR/logs" "$GITLAB_DIR/data"
mkdir -p "$GITLAB_DIR/config" "$GITLAB_DIR/logs" "$GITLAB_DIR/data" "$GITLAB_DIR/seed"
chown -R ga:ga "$GITLAB_DIR"

echo "Starting pinned GitLab CE container..."
docker compose up -d
docker compose ps

wait_for_gitlab

echo "Creating deterministic root API token by the documented Rails-runner method..."
docker exec gitlab gitlab-rails runner \
  "user = User.find_by_username('root'); user.personal_access_tokens.where(name: 'gym-anything-seed').each(&:revoke!); token = user.personal_access_tokens.create(scopes: ['api'], name: 'gym-anything-seed', expires_at: 365.days.from_now); token.set_token('${ROOT_TOKEN}'); token.save!"

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

echo "Importing the real GitLab CLI repository and real issue records..."
python3 /workspace/scripts/seed_gitlab.py \
  --base-url "$GITLAB_URL" \
  --token "$ROOT_TOKEN" \
  --output-dir "$GITLAB_DIR/seed"

chown -R ga:ga "$GITLAB_DIR/seed"
chmod 0644 "$GITLAB_DIR/seed/seed_manifest.json" "$GITLAB_DIR/seed/source_snapshot.json"

echo "Configuring GNOME Web for a deterministic benchmark session..."
su - ga -c "gsettings set org.gnome.Epiphany.web remember-passwords false" 2>/dev/null || true
su - ga -c "gsettings set org.gnome.Epiphany.web enable-popups false" 2>/dev/null || true
su - ga -c "gsettings set org.gnome.Epiphany.web ask-on-download false" 2>/dev/null || true

mkdir -p /home/ga/Desktop
cat > /home/ga/Desktop/GitLab.desktop << 'DESKTOPEOF'
[Desktop Entry]
Name=GitLab CE
Comment=Open the local GitLab Community Edition instance
Exec=epiphany-browser http://gitlab.local/root/gitlab-cli/-/issues
Icon=applications-development
StartupNotify=true
Terminal=false
Type=Application
Categories=Development;
DESKTOPEOF
chown ga:ga /home/ga/Desktop/GitLab.desktop
chmod 0755 /home/ga/Desktop/GitLab.desktop

echo "GitLab version: $(docker exec gitlab gitlab-rake gitlab:env:info 2>/dev/null | awk -F: '/GitLab version/{gsub(/^[[:space:]]+/, "", $2); print $2; exit}')"
echo "GitLab URL: ${GITLAB_URL}"
echo "Login: root / N7v!4Qz@8Lm#2Rx%"
echo "Seed manifest: ${GITLAB_DIR}/seed/seed_manifest.json"
echo "Source snapshot: ${GITLAB_DIR}/seed/source_snapshot.json"
printf 'success\n' > "$SETUP_STATUS_FILE"
trap - ERR
echo "=== GitLab setup complete ==="
