"""Live checks against a freshly booted, idle GitLab task VM.

Set GITLAB_ENV_SSH_PORT to the host-forwarded port printed by Gym. These checks
use direct password authentication and close the SSH client, independently of
the runner's key-to-password fallback. Run before interacting with the task.
"""

import json
import os

import paramiko
import pytest


@pytest.fixture(scope="module")
def guest():
    port = os.environ.get("GITLAB_ENV_SSH_PORT")
    if not port:
        pytest.skip("Set GITLAB_ENV_SSH_PORT for a freshly booted GitLab VM")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            "127.0.0.1", port=int(port), username="ga", password="password123",
            look_for_keys=False, allow_agent=False, timeout=10,
        )
        yield client
    finally:
        client.close()


def command(guest, script, timeout=120):
    _, stdout, stderr = guest.exec_command(script, timeout=timeout)
    output = stdout.read().decode()
    error = stderr.read().decode()
    assert stdout.channel.recv_exit_status() == 0, error or output
    return output


def test_first_boot_completes_without_failed_database_seed(guest):
    state = json.loads(command(guest, "sudo docker inspect gitlab-ce"))[0]
    assert state["State"]["Running"]
    assert not state["State"]["OOMKilled"]
    assert state["RestartCount"] == 0, "GitLab restarted during initial provisioning"
    logs = command(guest, "sudo docker logs gitlab-ce 2>&1")
    assert "Could not create the default administrator account" not in logs
    assert "Infra Phase failed" not in logs


def test_seed_created_administrator_and_writable_no_license_baseline(guest):
    # An HTTP-ready GitLab can have a partially seeded database: a rejected
    # initial_root_password leaves root absent even after the restart recovers.
    result = command(guest, """sudo docker exec gitlab-ce gitlab-rails runner '
      root = User.find_by(username: "root")
      user = User.find_by(username: "byteblaze")
      project = Project.find_by_full_path("byteblaze/dotfiles")
      puts({root_admin: !!root&.admin?, user_name: user&.name,
        branch: project.repository.root_ref,
        sha: project.repository.commit("main").id,
        license: !!project.repository.blob_at("main", "LICENSE"),
        writable: Gitlab::UserAccess.new(user, container: project).can_push_to_branch?("main")
      }.to_json)
    '""")
    state = json.loads(result.strip().splitlines()[-1])
    assert state["root_admin"], "GitLab recovered with an incomplete initial database seed"
    assert state["user_name"] == "Byte Blaze"
    assert state["branch"] == "main"
    assert state["sha"] == "218b5e72424aca8b580e52342dbb92bd4bd076c8"
    assert not state["license"]
    assert state["writable"]


def test_visible_chrome_is_on_authenticated_projects_dashboard(guest):
    result = command(guest, """python3 - <<'PY'
import sys
sys.path.insert(0, '/workspace/scripts')
from chrome_cdp import Cdp, connect_page
socket = connect_page()
try:
    print(Cdp(socket).evaluate('JSON.stringify({url: location.href, title: document.title, login: !!document.querySelector("#user_login"), projects: !!document.querySelector(".projects-list")})'))
finally:
    socket.close()
PY""")
    state = json.loads(result)
    assert state["url"] == "http://localhost:8929/dashboard/projects?sort=name_asc"
    assert "Projects" in state["title"]
    assert not state["login"]
    assert state["projects"]
    title = command(guest, "DISPLAY=:1 XAUTHORITY=/home/ga/.Xauthority xdotool getactivewindow getwindowname").strip()
    assert "Projects · GitLab" in title


@pytest.mark.skipif(
    os.environ.get("GITLAB_ENV_TASK") == "tag_davepgreene_merge_request",
    reason="Resets task 414, so requires the task 414 VM",
)
def test_task_414_reset_invalidates_protection_cache_and_restores_add_license(guest):
    # GitLab's protection service warms a Redis decision independently of the
    # database row. A direct destroy_all in pre_task leaves that decision live.
    warmed = command(guest, """sudo docker exec gitlab-ce gitlab-rails runner '
      user = User.find_by!(username: "byteblaze")
      project = Project.find_by_full_path("byteblaze/dotfiles")
      rule = ProtectedBranches::CreateService.new(project, user, {
        name: "main", push_access_levels_attributes: [{access_level: 0}],
        merge_access_levels_attributes: [{access_level: 40}]
      }).execute
      raise rule.errors.full_messages.join(", ") unless rule.persisted?
      puts({protected: ProtectedBranch.protected?(project, "main"),
        writable: Gitlab::UserAccess.new(user, container: project).can_push_to_branch?("main")
      }.to_json)
    '""")
    assert json.loads(warmed.strip().splitlines()[-1]) == {
        "protected": True, "writable": False,
    }

    try:
        command(guest, "sudo bash /workspace/tasks/add_mit_license/setup_task.sh", timeout=300)
        result = command(guest, """sudo docker exec gitlab-ce gitlab-rails runner '
          user = User.find_by!(username: "byteblaze")
          project = Project.find_by_full_path("byteblaze/dotfiles")
          puts({rules: project.protected_branches.where(name: "main").count,
            protected: ProtectedBranch.protected?(project, "main"),
            writable: Gitlab::UserAccess.new(user, container: project).can_push_to_branch?("main")
          }.to_json)
        '""")
        assert json.loads(result.strip().splitlines()[-1]) == {
            "rules": 0, "protected": False, "writable": True,
        }
        # Fetch as the authenticated browser user, retaining the dashboard.
        # Checking the repository tree alone misses a hidden UI affordance.
        result = command(guest, """python3 - <<'PY'
import sys
sys.path.insert(0, '/workspace/scripts')
from chrome_cdp import Cdp, connect_page
socket = connect_page()
try:
    print(Cdp(socket).evaluate('''(async () => {
      const response = await fetch('/byteblaze/dotfiles');
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      return JSON.stringify({status: response.status, add_license: !!doc.querySelector('.add-license-link')});
    })()'''))
finally:
    socket.close()
PY""")
        assert json.loads(result) == {"status": 200, "add_license": True}
    finally:
        command(guest, """sudo docker exec gitlab-ce gitlab-rails runner '
          project = Project.find_by_full_path("byteblaze/dotfiles")
          project.protected_branches.where(name: "main").destroy_all
          ProtectedBranches::CacheService.new(project).refresh
        '""")


@pytest.mark.skipif(
    os.environ.get("GITLAB_ENV_TASK") != "tag_davepgreene_merge_request",
    reason="Requires the task 415 start state",
)
def test_task_415_merge_request_and_assigned_list(guest):
    result = command(guest, """sudo docker exec gitlab-ce gitlab-rails runner '
      user = User.find_by(username: "byteblaze")
      project = Project.find_by_full_path("byteblaze/a11y-webring-club")
      mr = project.merge_requests.find_by!(iid: 40)
      puts({title: mr.title, state: mr.state, author: mr.author.username,
        assignees: mr.assignees.map(&:username), target: mr.target_branch,
        main_sha: project.repository.commit("main").id,
        source_sha: project.repository.commit(mr.source_branch).id,
        mentioned: mr.notes.where(system: false).any? { |note| note.note.include?("@davepgreene") },
        assigned: MergeRequest.opened.assigned_to(user).count
      }.to_json)
    '""")
    state = json.loads(result.strip().splitlines()[-1])
    assert state["title"] == "Add verification functions"
    assert state["state"] == "opened"
    assert state["author"] == "davepgreene"
    assert state["assignees"] == ["byteblaze"]
    assert state["target"] == "main"
    assert state["main_sha"] == "83d965d99adaf5adaee708fb093fa81063cd0db2"
    assert state["source_sha"] == "4817a445d1b74904bd695059aea63705370f9205"
    assert not state["mentioned"]
    assert state["assigned"] == 3
