#!/bin/bash
set -euo pipefail

echo "=== Installing GitLab environment dependencies ==="

export DEBIAN_FRONTEND=noninteractive
COMPOSE_VERSION="v2.39.4"
COMPOSE_SHA256="7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4"
COMPOSE_URL="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64"
GITLAB_IMAGE="gitlab/gitlab-ce:18.11.12-ce.0"
IMAGE_PULL_DIR="/var/lib/gym-anything-gitlab"
IMAGE_PULL_SCRIPT="/usr/local/sbin/pull-gitlab-image.sh"

APT_HOOKS=(
  /etc/apt/apt.conf.d/20packagekit
  /etc/apt/apt.conf.d/50appstream
  /etc/apt/apt.conf.d/50command-not-found
  /etc/apt/apt.conf.d/99update-notifier
)
restore_apt_hooks() {
  for hook in "${APT_HOOKS[@]}"; do
    if [ -f "${hook}.gym-anything-disabled" ]; then
      mv "${hook}.gym-anything-disabled" "$hook"
    fi
  done
}
trap restore_apt_hooks EXIT
for hook in "${APT_HOOKS[@]}"; do
  if [ -f "$hook" ]; then
    mv "$hook" "${hook}.gym-anything-disabled"
  fi
done
apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  dbus-x11 \
  docker.io \
  epiphany-browser \
  git \
  imagemagick \
  jq \
  netcat-openbsd \
  python3 \
  python3-requests \
  wmctrl \
  x11-apps \
  x11-utils \
  xdotool
restore_apt_hooks
trap - EXIT

install -d -m 0755 /usr/local/lib/docker/cli-plugins
curl -fsSL --retry 5 --retry-delay 3 "$COMPOSE_URL" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
echo "${COMPOSE_SHA256}  /usr/local/lib/docker/cli-plugins/docker-compose" | sha256sum -c -
chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose

systemctl enable docker
systemctl start docker
usermod -aG docker ga 2>/dev/null || true

echo "Waiting for Docker daemon..."
for attempt in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    echo "Docker is ready after attempt ${attempt}"
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    echo "ERROR: Docker daemon did not become ready"
    exit 1
  fi
  sleep 2
done

mkdir -p "$IMAGE_PULL_DIR"
cat > "$IMAGE_PULL_SCRIPT" <<'PULLEOF'
#!/bin/bash
set -uo pipefail

GITLAB_IMAGE="gitlab/gitlab-ce:18.11.12-ce.0"
IMAGE_PULL_DIR="/var/lib/gym-anything-gitlab"
STATUS_FILE="${IMAGE_PULL_DIR}/image-pull.status"

# Give the runtime's guest-side Docker Hub login a chance to run after the
# pre_start hook returns. Anonymous pulls still work when no credentials exist.
sleep 10

if docker pull "$GITLAB_IMAGE"; then
  printf 'success\n' > "$STATUS_FILE"
  exit 0
else
  status=$?
  printf 'failed:%s\n' "$status" > "$STATUS_FILE"
  exit "$status"
fi
PULLEOF
chmod 0755 "$IMAGE_PULL_SCRIPT"

if docker image inspect "$GITLAB_IMAGE" >/dev/null 2>&1; then
  printf 'success\n' > "$IMAGE_PULL_DIR/image-pull.status"
  echo "Pinned GitLab CE image is already available"
else
  rm -f "$IMAGE_PULL_DIR/image-pull.status"
  echo "Starting pinned GitLab CE image pull in the background..."
  setsid nohup "$IMAGE_PULL_SCRIPT" \
    > "$IMAGE_PULL_DIR/image-pull.log" 2>&1 </dev/null &
  echo "$!" > "$IMAGE_PULL_DIR/image-pull.pid"
  echo "GitLab image pull PID: $!"
fi

apt-get clean
rm -rf /var/lib/apt/lists/*

echo "Docker: $(docker --version)"
echo "Docker Compose: $(docker compose version)"
echo "Epiphany: $(epiphany-browser --version 2>/dev/null || true)"
echo "GitLab image pull status: ${IMAGE_PULL_DIR}/image-pull.status"
echo "=== GitLab dependency installation complete ==="
