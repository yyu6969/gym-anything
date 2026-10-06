#!/bin/bash
# post_start: start GitLab, seed the account/project records, and import the
# exact real repository revision shown in the source video.
set -euo pipefail

echo "=== Starting and seeding GitLab CE ==="
GITLAB_HOME=/home/ga/gitlab
GITLAB_URL=http://localhost:8929
BASELINE_COMMIT=218b5e72424aca8b580e52342dbb92bd4bd076c8
MASTER_COMMIT=e19ab89a021a57e1f8d354cea6bb87df09c7c504
SEED_REPO=/home/ga/gitlab_seed/dotfiles

mkdir -p "$GITLAB_HOME"/{config,logs,data}
cp /workspace/config/docker-compose.yml "$GITLAB_HOME/docker-compose.yml"
chown -R ga:ga "$GITLAB_HOME"

# Install browser policy before service initialization so Chrome's native
# password/default-browser prompts are suppressed on every subsequent launch.
mkdir -p /etc/opt/chrome/policies/managed
cp /workspace/config/chrome-policy.json /etc/opt/chrome/policies/managed/gitlab-environment.json

cd "$GITLAB_HOME"
docker compose up -d

echo "Waiting for GitLab HTTP readiness (first boot can take several minutes)..."
ready=false
for attempt in $(seq 1 180); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$GITLAB_URL/users/sign_in" 2>/dev/null || true)
  if [ "$code" = "200" ]; then
    echo "GitLab HTTP endpoint ready after $((attempt * 5))s"
    ready=true
    break
  fi
  if [ $((attempt % 12)) -eq 0 ]; then
    echo "  still waiting: $((attempt * 5))s (HTTP ${code:-000})"
  fi
  sleep 5
done

if [ "$ready" != "true" ]; then
  echo "ERROR: GitLab did not become ready"
  docker logs gitlab-ce --tail 120
  exit 1
fi

echo "Waiting for GitLab Rails to accept runner commands..."
rails_ready=false
for attempt in $(seq 1 60); do
  if docker exec gitlab-ce gitlab-rails runner 'puts "RAILS_READY"' 2>/dev/null | grep -q RAILS_READY; then
    rails_ready=true
    break
  fi
  sleep 5
done
if [ "$rails_ready" != "true" ]; then
  echo "ERROR: GitLab Rails runner never became ready"
  exit 1
fi

docker cp /workspace/scripts/seed_gitlab.rb gitlab-ce:/tmp/seed_gitlab.rb
docker exec gitlab-ce gitlab-rails runner /tmp/seed_gitlab.rb | tee /tmp/gitlab_seed_metadata.log
grep -q 'SEED_OK' /tmp/gitlab_seed_metadata.log

mkdir -p /home/ga/gitlab_seed
if [ ! -d "$SEED_REPO/.git" ]; then
  git clone /workspace/assets/dotfiles.bundle "$SEED_REPO"
fi
git -C "$SEED_REPO" checkout -f "$BASELINE_COMMIT"
git -C "$SEED_REPO" branch -f main "$BASELINE_COMMIT"
git -C "$SEED_REPO" config user.name 'Byte Blaze'
git -C "$SEED_REPO" config user.email 'byteblaze@example.test'
git -C "$SEED_REPO" remote remove gitlab 2>/dev/null || true
git -C "$SEED_REPO" remote add gitlab 'http://byteblaze:V9xQ4mL2pR7s%21@localhost:8929/byteblaze/dotfiles.git'
git -C "$SEED_REPO" push --force gitlab main:main
git -C "$SEED_REPO" push --force gitlab "$MASTER_COMMIT":refs/heads/master

docker exec gitlab-ce gitlab-rails runner \
  "p=Project.find_by_full_path('byteblaze/dotfiles'); raise 'missing repository' unless p && p.repository_exists?; raise 'wrong default branch' unless p.repository.root_ref == 'main'; puts 'PROJECT_OK branch=main'" \
  | tee /tmp/gitlab_seed_repository.log
grep -q 'PROJECT_OK' /tmp/gitlab_seed_repository.log

actual_sha=$(git ls-remote 'http://byteblaze:V9xQ4mL2pR7s%21@localhost:8929/byteblaze/dotfiles.git' refs/heads/main | awk '{print $1}')
if [ "$actual_sha" != "$BASELINE_COMMIT" ]; then
  echo "ERROR: Imported repository SHA $actual_sha does not match $BASELINE_COMMIT"
  exit 1
fi

chown -R ga:ga /home/ga/gitlab_seed

echo "=== GitLab setup complete ==="
echo "GitLab URL: $GITLAB_URL"
echo "Task user: Byte Blaze (byteblaze)"
echo "Target project: byteblaze/dotfiles"
echo "Baseline commit: $actual_sha"
