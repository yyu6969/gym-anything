#!/bin/bash
# pre_start: install the browser and Docker, then cache the pinned GitLab image.
set -euo pipefail

echo "=== Installing GitLab environment prerequisites ==="
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  docker.io \
  docker-compose-v2 \
  git \
  imagemagick \
  jq \
  python3 \
  python3-pip \
  scrot \
  unzip \
  wget \
  wmctrl \
  x11-utils \
  xdotool

systemctl enable docker
systemctl start docker
usermod -aG docker ga || true

# The demonstrated workflow uses Chrome. Pin the exact tested native package
# and verify it before installation so a later "current stable" release cannot
# change CDP startup or browser UI behavior.
CHROME_VERSION=154.0.8037.97-1
CHROME_SHA256=a4edbe95e9b01db6c9b97d7a1323121eda18362b5620df06abac1b59bee80053
CHROME_DEB=/tmp/google-chrome-stable_${CHROME_VERSION}_amd64.deb
CHROME_URL="https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}_amd64.deb"
installed_chrome_version=$(dpkg-query -W -f='${Version}' google-chrome-stable 2>/dev/null || true)
if [ "$installed_chrome_version" != "$CHROME_VERSION" ]; then
  wget --quiet --tries=4 --timeout=30 \
    "$CHROME_URL" \
    -O "$CHROME_DEB"
  echo "$CHROME_SHA256  $CHROME_DEB" | sha256sum --check --strict
  apt-get install -y --allow-downgrades "$CHROME_DEB"
fi
test "$(dpkg-query -W -f='${Version}' google-chrome-stable)" = "$CHROME_VERSION"

python3 -m pip install --no-cache-dir websocket-client==1.9.2

if [ -f /workspace/config/.dockerhub_credentials ]; then
  # shellcheck disable=SC1091
  source /workspace/config/.dockerhub_credentials
  echo "${DOCKERHUB_TOKEN:-}" | docker login -u "${DOCKERHUB_USERNAME:-}" --password-stdin >/dev/null 2>&1 || true
fi

echo "Pulling pinned GitLab CE image..."
GITLAB_IMAGE='gitlab/gitlab-ce:15.11.13-ce.0@sha256:798b18325a90851922c916fcded34d1ba7decf810a311765b43e43f452bf564c'
docker pull "$GITLAB_IMAGE"
docker image inspect "$GITLAB_IMAGE" >/dev/null

apt-get clean
rm -f "$CHROME_DEB"
rm -rf /var/lib/apt/lists/*

echo "=== GitLab prerequisites installed ==="
docker --version
docker compose version
google-chrome --version
