#!/bin/bash
set -euo pipefail

echo "=== Setting up triage_windows_build_failure task ==="

source /workspace/scripts/task_utils.sh

TARGET_SOURCE_IID=8551
REQUIRED_COMMENT='Confirmed as the Windows build release blocker for the next patch. Please add non-Windows build tags and regression coverage.'

if ! wait_for_gitlab_setup 10800; then
  echo "ERROR: GitLab background setup did not complete"
  exit 1
fi

if ! wait_for_gitlab_api 300; then
  echo "ERROR: GitLab API is not reachable"
  exit 1
fi

if [ ! -f "$GITLAB_MANIFEST" ]; then
  echo "ERROR: Missing real-data seed manifest: ${GITLAB_MANIFEST}"
  exit 1
fi

PROJECT_ID=$(gitlab_project_id)
TARGET_IID=$(gitlab_local_issue_iid "$TARGET_SOURCE_IID")
SOURCE_LABELS=$(gitlab_source_labels_csv "$TARGET_SOURCE_IID")

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "null" ] || [ -z "$TARGET_IID" ] || [ "$TARGET_IID" = "null" ]; then
  echo "ERROR: Could not resolve target from seed manifest"
  exit 1
fi

echo "Resetting local issue #${TARGET_IID} from real source issue #${TARGET_SOURCE_IID}"
RESET_PAYLOAD=$(jq -n \
  --arg labels "$SOURCE_LABELS" \
  '{labels: $labels, assignee_ids: [], due_date: null, state_event: "reopen"}')
RESET_RESULT=$(gitlab_api PUT "projects/${PROJECT_ID}/issues/${TARGET_IID}" \
  --data-binary "$RESET_PAYLOAD" -H 'Content-Type: application/json')

echo "$RESET_RESULT" | jq -e \
  --argjson target "$TARGET_IID" \
  --arg labels "$SOURCE_LABELS" \
  '.iid == $target and (.state == "opened") and (.due_date == null) and ((.assignees | length) == 0) and ((.labels | sort) == ($labels | split(",") | sort))' \
  >/dev/null

echo "Removing prior copies of the task-specific comment, if present"
NOTES=$(gitlab_api GET "projects/${PROJECT_ID}/issues/${TARGET_IID}/notes?per_page=100")
while IFS= read -r note_id; do
  [ -n "$note_id" ] || continue
  gitlab_api DELETE "projects/${PROJECT_ID}/issues/${TARGET_IID}/notes/${note_id}" >/dev/null
  echo "Deleted prior task note id=${note_id}"
done < <(echo "$NOTES" | jq -r --arg body "$REQUIRED_COMMENT" '.[] | select(.body == $body) | .id')

VERIFY_NOTES=$(gitlab_api GET "projects/${PROJECT_ID}/issues/${TARGET_IID}/notes?per_page=100")
if echo "$VERIFY_NOTES" | jq -e --arg body "$REQUIRED_COMMENT" '.[] | select(.body == $body)' >/dev/null; then
  echo "ERROR: Task comment reset failed"
  exit 1
fi

PROJECT_PATH=$(jq -r '.local.path_with_namespace' "$GITLAB_MANIFEST")
ISSUES_URL="${GITLAB_URL}/${PROJECT_PATH}/-/issues"

echo "Opening authenticated browser at ${ISSUES_URL}"
if ! login_gitlab_browser "$ISSUES_URL"; then
  echo "ERROR: Could not prepare the authenticated GitLab Issues view"
  DISPLAY=:1 XAUTHORITY="$(xauthority_path)" wmctrl -l 2>/dev/null || true
  cat /tmp/epiphany_gitlab.log 2>/dev/null || true
  exit 1
fi

sleep 3
capture_browser_window /tmp/gitlab_task_start.png

echo "Project id: ${PROJECT_ID}"
echo "Target local issue iid: ${TARGET_IID}"
echo "Target source URL: https://gitlab.com/gitlab-org/cli/-/work_items/${TARGET_SOURCE_IID}"
echo "Browser title: $(browser_title)"
echo "Task start screenshot: /tmp/gitlab_task_start.png"
echo "=== Task setup complete ==="
