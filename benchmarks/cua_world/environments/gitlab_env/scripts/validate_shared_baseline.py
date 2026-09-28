#!/usr/bin/env python3
"""Validate the reconstructed shared GitLab baseline and all declared exclusions."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def load_records(dataset: Path, name: str) -> list[dict[str, Any]]:
    return json.loads((dataset / name).read_text(encoding="utf-8"))["records"]


def encoded(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def second(value: str | None) -> str | None:
    return value[:19] if value else None


class GitLabAPI:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/") + "/api/v4"
        self.token = token

    def request(
        self, path: str, *, sudo: str | None = None, allow_404: bool = False
    ) -> tuple[Any, dict[str, str]]:
        headers = {"PRIVATE-TOKEN": self.token, "Accept": "application/json"}
        if sudo:
            headers["Sudo"] = sudo
        request = urllib.request.Request(
            self.base_url + "/" + path.lstrip("/"), headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()
                return (json.loads(body) if body else None), dict(response.headers)
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None, {}
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GET {path} failed with HTTP {exc.code}: {detail[:500]}"
            ) from exc

    def pages(self, path: str, *, sudo: str | None = None) -> list[dict[str, Any]]:
        separator = "&" if "?" in path else "?"
        page = 1
        values: list[dict[str, Any]] = []
        while True:
            data, headers = self.request(
                f"{path}{separator}per_page=100&page={page}", sudo=sudo
            )
            values.extend(data)
            next_page = headers.get("X-Next-Page", "")
            if not next_page:
                return values
            page = int(next_page)


class Validator:
    def __init__(self, dataset: Path, seed_manifest: Path, api: GitLabAPI) -> None:
        self.dataset = dataset
        self.seed_manifest = json.loads(seed_manifest.read_text(encoding="utf-8"))
        self.api = api
        self.checks: list[dict[str, Any]] = []
        self.projects: dict[str, dict[str, Any] | None] = {}
        self.issues: dict[str, list[dict[str, Any]]] = {}
        self.merge_requests: dict[str, list[dict[str, Any]]] = {}
        self.notes: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
        self.members: dict[str, list[dict[str, Any]]] = {}

    def check(self, condition: bool, category: str, key: str, detail: str = "") -> None:
        self.checks.append(
            {
                "status": "passed" if condition else "failed",
                "category": category,
                "semantic_key": key,
                "detail": detail,
            }
        )

    def project(self, path: str) -> dict[str, Any] | None:
        if path not in self.projects:
            value, _ = self.api.request(f"projects/{encoded(path)}", allow_404=True)
            self.projects[path] = value
        return self.projects[path]

    def project_issues(self, path: str) -> list[dict[str, Any]]:
        if path not in self.issues:
            project = self.project(path)
            self.issues[path] = (
                self.api.pages(f"projects/{project['id']}/issues?scope=all&state=all")
                if project
                else []
            )
        return self.issues[path]

    def project_mrs(self, path: str) -> list[dict[str, Any]]:
        if path not in self.merge_requests:
            project = self.project(path)
            self.merge_requests[path] = (
                self.api.pages(
                    f"projects/{project['id']}/merge_requests?scope=all&state=all"
                )
                if project
                else []
            )
        return self.merge_requests[path]

    def parent_notes(
        self, path: str, parent_type: str, iid: int
    ) -> list[dict[str, Any]]:
        key = (path, parent_type, iid)
        if key not in self.notes:
            project = self.project(path)
            endpoint = "issues" if parent_type == "issue" else "merge_requests"
            self.notes[key] = self.api.pages(
                f"projects/{project['id']}/{endpoint}/{iid}/notes?sort=asc&order_by=created_at"
            )
        return self.notes[key]

    def project_members(self, path: str) -> list[dict[str, Any]]:
        if path not in self.members:
            project = self.project(path)
            self.members[path] = (
                self.api.pages(f"projects/{project['id']}/members/all")
                if project
                else []
            )
        return self.members[path]

    def git_refs(self, disk_path: str) -> dict[str, str]:
        git_dir = Path("/var/opt/gitlab/git-data/repositories") / f"{disk_path}.git"
        result = subprocess.run(
            [
                "git",
                f"--git-dir={git_dir}",
                "for-each-ref",
                "--format=%(refname) %(objectname)",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return dict(line.split(" ", 1) for line in result.stdout.splitlines())

    def validate_users_and_projects(self) -> None:
        user_records = [
            item
            for item in load_records(self.dataset, "users.json")
            if item["identity_type"] == "gitlab_account"
        ]
        unique = {item["canonical_username"].lower(): item for item in user_records}
        for record in unique.values():
            username = record["canonical_username"]
            users = self.api.pages(f"users?username={encoded(username)}")
            exact = [
                item for item in users if item["username"].lower() == username.lower()
            ]
            self.check(len(exact) == 1, "user", username, f"matches={len(exact)}")

        project_records = load_records(self.dataset, "projects.json")
        for record in project_records:
            path = record["path_with_namespace"]
            actual = self.project(path)
            valid = bool(actual) and all(
                [
                    actual["path_with_namespace"].lower() == path.lower(),
                    actual["visibility"] == record["visibility"],
                    actual.get("description") == record["description"],
                    actual.get("archived") == record["archived"],
                    actual.get("default_branch") == record["default_branch"]
                    or path not in self.seed_manifest["repository_disk_paths"],
                ]
            )
            self.check(valid, "project", path, "missing or metadata differs")

        visible_projects = self.api.pages("projects?simple=true&order_by=id")
        self.check(
            len(visible_projects) == len(project_records),
            "count",
            "projects",
            f"actual={len(visible_projects)}",
        )

        for record in load_records(self.dataset, "groups.json"):
            owner = record["owner_username"]
            users = self.api.pages(f"users?username={encoded(owner)}")
            exact = [
                item for item in users if item["username"].lower() == owner.lower()
            ]
            self.check(
                len(exact) == 1,
                "namespace_dependency",
                record["semantic_key"],
                f"matches={len(exact)}",
            )

        for record in load_records(self.dataset, "project_members.json"):
            actual = next(
                (
                    item
                    for item in self.project_members(record["project"])
                    if item["username"].lower() == record["username"].lower()
                ),
                None,
            )
            self.check(
                bool(actual) and actual["access_level"] == record["access_level"],
                "project_membership",
                record["semantic_key"],
                f"actual={actual}",
            )

    def validate_repositories(self) -> None:
        disk_paths = self.seed_manifest["repository_disk_paths"]
        for record in load_records(self.dataset, "repository_metadata.json"):
            project = record["project"]
            actual = self.git_refs(disk_paths[project])
            expected = {item["name"]: item["oid"] for item in record["refs"]}
            changed_or_missing = {
                name: [oid, actual.get(name)]
                for name, oid in expected.items()
                if actual.get(name) != oid
            }
            extra = sorted(set(actual) - set(expected))
            unexpected_extra = [
                name
                for name in extra
                if not name.startswith(("refs/keep-around/", "refs/merge-requests/"))
            ]
            self.check(
                not changed_or_missing and not unexpected_extra,
                "repository_refs",
                project,
                f"expected={len(expected)} actual={len(actual)} "
                f"changed_or_missing={json.dumps(changed_or_missing, sort_keys=True)} "
                f"unexpected_extra={unexpected_extra}",
            )
        for record in load_records(self.dataset, "branches.json"):
            actual = self.git_refs(disk_paths[record["project"]])
            self.check(
                actual.get(f"refs/heads/{record['name']}") == record["commit_oid"],
                "branch",
                record["semantic_key"],
            )
        for record in load_records(self.dataset, "files.json"):
            git_dir = (
                Path("/var/opt/gitlab/git-data/repositories")
                / f"{disk_paths[record['project']]}.git"
            )
            result = subprocess.run(
                [
                    "git",
                    f"--git-dir={git_dir}",
                    "show",
                    f"{record['branch']}:{record['path']}",
                ],
                check=True,
                capture_output=True,
            )
            digest = hashlib.sha256(result.stdout).hexdigest()
            self.check(
                digest == record["content_sha256"],
                "repository_file",
                record["semantic_key"],
            )

    def validate_labels_milestones_issues_mrs_notes(self) -> None:
        expected_label_count = 0
        for record in load_records(self.dataset, "labels.json"):
            project = self.project(record["project"])
            labels = self.api.pages(
                f"projects/{project['id']}/labels?include_ancestor_groups=false"
            )
            actual = next(
                (item for item in labels if item["name"] == record["name"]), None
            )
            self.check(
                bool(actual)
                and actual["color"].lower() == record["color"].lower()
                and actual.get("description") == record["description"],
                "label",
                record["semantic_key"],
            )
            expected_label_count += 1

        milestone_records = load_records(self.dataset, "milestones.json")
        for record in milestone_records:
            project = self.project(record["project"])
            milestones = self.api.pages(
                f"projects/{project['id']}/milestones?state=all"
            )
            actual = next(
                (item for item in milestones if item["title"] == record["title"]), None
            )
            self.check(
                bool(actual)
                and actual["iid"] == record["iid"]
                and actual["state"] == record["state"]
                and actual.get("start_date") == record["start_date"]
                and actual.get("due_date") == record["due_date"],
                "milestone",
                record["semantic_key"],
            )

        actual_label_count = 0
        actual_milestone_count = 0
        for project_record in load_records(self.dataset, "projects.json"):
            project = self.project(project_record["path_with_namespace"])
            actual_label_count += len(
                self.api.pages(
                    f"projects/{project['id']}/labels?include_ancestor_groups=false"
                )
            )
            actual_milestone_count += len(
                self.api.pages(f"projects/{project['id']}/milestones?state=all")
            )
        self.check(
            actual_label_count == expected_label_count,
            "count",
            "labels",
            f"actual={actual_label_count}",
        )
        self.check(
            actual_milestone_count == len(milestone_records),
            "count",
            "milestones",
            f"actual={actual_milestone_count}",
        )

        issue_records = load_records(self.dataset, "issues.json")
        for record in issue_records:
            actual = next(
                (
                    item
                    for item in self.project_issues(record["project"])
                    if item["iid"] == record["iid"]
                ),
                None,
            )
            actual_assignees = (
                sorted(item["username"] for item in actual.get("assignees", []))
                if actual
                else []
            )
            valid = bool(actual) and all(
                [
                    actual["title"] == record["title"],
                    actual.get("description") == record["description"],
                    actual["state"] == record["state"],
                    actual["author"]["username"].lower()
                    == record["author_username"].lower(),
                    sorted(actual["labels"]) == sorted(record["labels"]),
                    actual_assignees == sorted(record["assignees"]),
                    actual.get("due_date") == record["due_date"],
                    (actual.get("milestone") or {}).get("title")
                    == record["milestone_title"],
                ]
            )
            self.check(valid, "issue", record["semantic_key"], "initial state differs")

        mr_records = load_records(self.dataset, "merge_requests.json")
        for record in mr_records:
            actual = next(
                (
                    item
                    for item in self.project_mrs(record["project"])
                    if item["iid"] == record["iid"]
                ),
                None,
            )
            valid = bool(actual) and all(
                [
                    actual["title"] == record["title"],
                    actual.get("description") == record["description"],
                    actual["state"] == record["state"],
                    actual["source_branch"] == record["source_branch"],
                    actual["target_branch"] == record["target_branch"],
                    actual["author"]["username"].lower()
                    == record["author_username"].lower(),
                    sorted(item["username"] for item in actual.get("assignees", []))
                    == sorted(record["assignees"]),
                    sorted(item["username"] for item in actual.get("reviewers", []))
                    == sorted(record["reviewers"]),
                    sorted(actual["labels"]) == sorted(record["labels"]),
                ]
            )
            self.check(
                valid, "merge_request", record["semantic_key"], "initial state differs"
            )

        expected_notes = load_records(self.dataset, "notes.json")
        grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = (
            collections.defaultdict(list)
        )
        for record in expected_notes:
            iid = int(
                record["parent"].split(
                    "#" if record["parent_type"] == "issue" else "!"
                )[-1]
            )
            grouped[(record["project"], record["parent_type"], iid)].append(record)
        actual_total = 0
        for (path, parent_type, iid), expected in grouped.items():
            actual = self.parent_notes(path, parent_type, iid)
            actual_total += len(actual)
            expected_counter = collections.Counter(
                (
                    item["body"],
                    item["author_username"].lower(),
                    item["system"],
                    second(item["created_at"]),
                )
                for item in expected
            )
            actual_counter = collections.Counter(
                (
                    item["body"],
                    item["author"]["username"].lower(),
                    item["system"],
                    second(item["created_at"]),
                )
                for item in actual
            )
            self.check(
                expected_counter == actual_counter,
                "notes",
                f"{path}:{parent_type}:{iid}",
                f"expected={len(expected)} actual={len(actual)}",
            )
        self.check(actual_total == 85, "count", "notes", f"actual={actual_total}")
        self.check(
            sum(
                len(self.project_issues(path))
                for path in self.projects
                if self.projects[path]
            )
            == 10,
            "count",
            "issues",
        )
        self.check(
            sum(
                len(self.project_mrs(path))
                for path in self.projects
                if self.projects[path]
            )
            == 8,
            "count",
            "merge_requests",
        )

    def validate_exclusions(self) -> None:
        exclusions = json.loads(
            (self.dataset / "excluded_initial_entities.json").read_text(
                encoding="utf-8"
            )
        )
        starred = {
            item["path_with_namespace"].lower()
            for item in self.api.pages(
                "projects?starred=true&order_by=id", sudo="byteblaze"
            )
        }
        byteblaze_users = self.api.pages("users?username=byteblaze")
        following = {
            item["username"].lower()
            for item in self.api.pages(f"users/{byteblaze_users[0]['id']}/following")
        }
        checked = 0
        for category, values in exclusions["categories"].items():
            for record in values:
                checked += 1
                entity_type = record["entity_type"]
                absent = False
                if entity_type in {"project"}:
                    absent = self.project(record["path_with_namespace"]) is None
                elif entity_type == "group":
                    group, _ = self.api.request(
                        f"groups/{encoded(record['full_path'])}", allow_404=True
                    )
                    absent = group is None
                elif entity_type == "file":
                    disk_path = self.seed_manifest["repository_disk_paths"][
                        record["project"]
                    ]
                    git_dir = (
                        Path("/var/opt/gitlab/git-data/repositories")
                        / f"{disk_path}.git"
                    )
                    result = subprocess.run(
                        [
                            "git",
                            f"--git-dir={git_dir}",
                            "cat-file",
                            "-e",
                            f"{record['branch']}:{record['path']}",
                        ],
                        capture_output=True,
                        check=False,
                    )
                    absent = result.returncode != 0
                elif entity_type == "issue":
                    needle = record["title_or_description"].lower()
                    absent = not any(
                        needle
                        in (
                            item["title"] + "\n" + (item.get("description") or "")
                        ).lower()
                        for item in self.project_issues(record["project"])
                    )
                elif entity_type == "merge_request":
                    absent = not any(
                        item["source_branch"] == record["source_branch"]
                        and item["target_branch"] == record["target_branch"]
                        for item in self.project_mrs(record["project"])
                    )
                elif entity_type == "milestone":
                    project = self.project(record["project"])
                    actual = self.api.pages(
                        f"projects/{project['id']}/milestones?state=all"
                    )
                    absent = not any(
                        item["title"] == record["title_expression"] for item in actual
                    )
                elif entity_type == "comment":
                    notes = self.parent_notes(
                        record["project"], record["parent_type"], record["parent_iid"]
                    )
                    absent = not any(item["body"] == record["text"] for item in notes)
                elif entity_type in {
                    "fork_relationship",
                    "project_membership_in_created_project",
                }:
                    absent = (
                        self.project(record.get("target_project") or record["project"])
                        is None
                    )
                elif entity_type == "group_membership_in_created_group":
                    group, _ = self.api.request(
                        f"groups/{encoded(record['group'])}", allow_404=True
                    )
                    absent = group is None
                elif entity_type == "project_membership":
                    absent = not any(
                        item["username"].lower() == record["username"].lower()
                        for item in self.project_members(record["project"])
                    )
                elif entity_type in {"issue_assignee_in_created_issue"}:
                    needle = record["issue"].split("#", 1)[1].lower()
                    absent = not any(
                        needle
                        in (
                            item["title"] + "\n" + (item.get("description") or "")
                        ).lower()
                        for item in self.project_issues(
                            record["issue"].split("#", 1)[0]
                        )
                    )
                elif entity_type == "issue_assignee_relationship":
                    path, iid_text = record["issue"].rsplit("#", 1)
                    issue = next(
                        (
                            item
                            for item in self.project_issues(path)
                            if item["iid"] == int(iid_text)
                        ),
                        None,
                    )
                    absent = bool(issue) and not any(
                        item["username"].lower() == record["username"].lower()
                        for item in issue["assignees"]
                    )
                elif entity_type == "merge_request_reviewer":
                    expression = record["merge_request"].split("!", 1)[1]
                    source_branch, target_branch = expression.split("->", 1)
                    path = record["merge_request"].split("!", 1)[0]
                    absent = not any(
                        item["source_branch"] == source_branch
                        and item["target_branch"] == target_branch
                        for item in self.project_mrs(path)
                    )
                elif entity_type == "project_star":
                    absent = record["project"].lower() not in starred
                elif entity_type == "user_follow":
                    absent = record["followed"].lower() not in following
                else:
                    raise RuntimeError(
                        f"unsupported exclusion entity type: {entity_type}"
                    )
                self.check(
                    absent, "excluded_initial_entity", record["semantic_key"], category
                )
        self.check(
            checked == exclusions["total_count"] == 168,
            "count",
            "exclusions",
            f"checked={checked}",
        )

    def validate_listener_scope(self) -> None:
        result = subprocess.run(
            ["ss", "-ltnp"], check=True, capture_output=True, text=True
        )
        bad = []
        for line in result.stdout.splitlines():
            columns = line.split()
            if len(columns) < 4:
                continue
            local_address = columns[3]
            wildcard = local_address.startswith(("0.0.0.0:", "[::]:", "*:"))
            gitlab_process = any(
                name in line.lower()
                for name in ("nginx", "puma", "workhorse", "gitlab")
            )
            if wildcard and gitlab_process:
                bad.append(line)
        self.check(not bad, "network", "loopback_only_gitlab", "\n".join(bad))

    def run(self) -> dict[str, Any]:
        self.validate_users_and_projects()
        self.validate_repositories()
        self.validate_labels_milestones_issues_mrs_notes()
        self.validate_exclusions()
        self.validate_listener_scope()
        failed = [item for item in self.checks if item["status"] == "failed"]
        return {
            "schema_version": 1,
            "status": "passed" if not failed else "failed",
            "summary": {
                "passed": len(self.checks) - len(failed),
                "failed": len(failed),
                "total": len(self.checks),
            },
            "checks": self.checks,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--seed-manifest", required=True, type=Path)
    parser.add_argument("--base-url", default="http://gitlab.local")
    parser.add_argument("--token", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    validator = Validator(
        args.dataset.resolve(), args.seed_manifest, GitLabAPI(args.base_url, args.token)
    )
    report = validator.run()
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], sort_keys=True), flush=True)
    if report["status"] != "passed":
        for item in report["checks"]:
            if item["status"] == "failed":
                print(
                    f"FAILED {item['category']} {item['semantic_key']}: {item['detail']}",
                    file=sys.stderr,
                )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
