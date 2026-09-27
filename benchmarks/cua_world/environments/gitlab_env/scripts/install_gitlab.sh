#!/bin/bash
set -euo pipefail

echo "=== Installing GitLab environment dependencies ==="

export DEBIAN_FRONTEND=noninteractive
GITLAB_VERSION="18.11.12-ce.0"
GITLAB_URL="http://gitlab.local"
GITLAB_ROOT_PASSWORD='N7v!4Qz@8Lm#2Rx%'
GITLAB_REPOSITORY_SCRIPT="/tmp/gitlab-ce-repository.sh"

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
  firefox \
  git \
  imagemagick \
  jq \
  locales \
  netcat-openbsd \
  openssh-server \
  perl \
  python3 \
  python3-requests \
  wmctrl \
  x11-apps \
  x11-utils \
  xdotool

echo "Configuring the official GitLab CE package repository..."
curl -fsSL --retry 5 --retry-delay 3 \
  https://packages.gitlab.com/install/repositories/gitlab/gitlab-ce/script.deb.sh \
  -o "$GITLAB_REPOSITORY_SCRIPT"
bash "$GITLAB_REPOSITORY_SCRIPT"
apt-get update

if ! apt-cache madison gitlab-ce | awk '{print $3}' | grep -Fx "$GITLAB_VERSION" >/dev/null; then
  echo "ERROR: GitLab CE package ${GITLAB_VERSION} is not available for this guest"
  exit 1
fi

if ! grep -qE '(^|[[:space:]])gitlab\.local([[:space:]]|$)' /etc/hosts; then
  echo "127.0.0.1 gitlab.local" >> /etc/hosts
fi

install -d -m 0755 /etc/gitlab
cat > /etc/gitlab/gitlab.rb <<'GITLABEOF'
external_url 'http://gitlab.local'
gitlab_rails['gitlab_shell_ssh_port'] = 22
gitlab_rails['env'] = {
  'MALLOC_CONF' => 'dirty_decay_ms:1000,muzzy_decay_ms:1000'
}
letsencrypt['enable'] = false
nginx['listen_addresses'] = ['127.0.0.1']
puma['worker_processes'] = 0
puma['exporter_enabled'] = false
sidekiq['concurrency'] = 10
sidekiq['metrics_enabled'] = false
alertmanager['enable'] = false
gitlab_exporter['enable'] = false
gitlab_kas['enable'] = false
node_exporter['enable'] = false
postgres_exporter['enable'] = false
prometheus_monitoring['enable'] = false
prometheus['enable'] = false
redis_exporter['enable'] = false
GITLABEOF

echo "Installing pinned GitLab CE ${GITLAB_VERSION} directly in the guest..."
EXTERNAL_URL="$GITLAB_URL" GITLAB_ROOT_PASSWORD="$GITLAB_ROOT_PASSWORD" \
  apt-get install -y "gitlab-ce=${GITLAB_VERSION}"
apt-mark hold gitlab-ce

installed_version=$(dpkg-query -W -f='${Version}' gitlab-ce)
if [ "$installed_version" != "$GITLAB_VERSION" ]; then
  echo "ERROR: Installed GitLab CE ${installed_version}; expected ${GITLAB_VERSION}"
  exit 1
fi

echo "Applying native GitLab configuration..."
gitlab-ctl reconfigure
systemctl enable gitlab-runsvdir.service
gitlab-ctl start

restore_apt_hooks
trap - EXIT

apt-get clean
rm -rf /var/lib/apt/lists/*
rm -f "$GITLAB_REPOSITORY_SCRIPT"

echo "GitLab CE package: ${installed_version}"
echo "Firefox: $(firefox --version 2>/dev/null || true)"
echo "=== Native GitLab installation complete ==="
