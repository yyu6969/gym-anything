#!/usr/bin/env python3
"""Seed local GitLab with a real public repository and real issue records."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SOURCE_PROJECT_ID = 34675721
SOURCE_PROJECT_PATH = "gitlab-org/cli"
SOURCE_PROJECT_URL = "https://gitlab.com/gitlab-org/cli"
SOURCE_GIT_URL = "https://gitlab.com/gitlab-org/cli.git"
SOURCE_ISSUE_IIDS = [8551, 8565, 7554, 7685, 979, 939, 903]


def request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "gym-anything-gitlab-env/0.1"}
    data = None
    if token:
        headers["PRIVATE-TOKEN"] = token
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            return json.loads(body.decode("utf-8")) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail[:1000]}") from exc


def fetch_real_source_records() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    source_api = "https://gitlab.com/api/v4"
    project = request_json("GET", f"{source_api}/projects/{SOURCE_PROJECT_ID}")
    issues = [
        request_json("GET", f"{source_api}/projects/{SOURCE_PROJECT_ID}/issues/{iid}")
        for iid in SOURCE_ISSUE_IIDS
    ]
    branch = request_json(
        "GET",
        f"{source_api}/projects/{SOURCE_PROJECT_ID}/repository/branches/"
        + urllib.parse.quote(project["default_branch"], safe=""),
    )
    return project, issues, branch


def wait_for_import(base_url: str, token: str, project_id: int, timeout: int = 1800) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    while time.monotonic() < deadline:
        project = request_json("GET", f"{base_url}/api/v4/projects/{project_id}", token=token)
        last_status = project.get("import_status") or "none"
        if last_status == "failed":
            raise RuntimeError(f"Repository import failed: {project.get('import_error', 'unknown error')}")
        if last_status in {"finished", "none"} and project.get("default_branch"):
            try:
                request_json(
                    "GET",
                    f"{base_url}/api/v4/projects/{project_id}/repository/tree?per_page=1",
                    token=token,
                )
                return project
            except RuntimeError:
                pass
        print(f"Repository import status: {last_status}", flush=True)
        time.sleep(10)
    raise TimeoutError(f"Repository import did not finish within {timeout}s (last status: {last_status})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching real source data from {SOURCE_PROJECT_URL}", flush=True)
    source_project, source_issues, source_branch = fetch_real_source_records()

    project = request_json(
        "POST",
        f"{base_url}/api/v4/projects",
        token=args.token,
        payload={
            "name": source_project["name"],
            "path": "gitlab-cli",
            "description": source_project["description"],
            "visibility": "internal",
            "import_url": SOURCE_GIT_URL,
            "issues_access_level": "enabled",
            "merge_requests_access_level": "enabled",
            "wiki_access_level": "enabled",
        },
        timeout=180,
    )
    project_id = int(project["id"])
    print(f"Created local project {project['path_with_namespace']} (id={project_id})", flush=True)
    project = wait_for_import(base_url, args.token, project_id)

    seeded_issues: list[dict[str, Any]] = []
    for source in source_issues:
        local = request_json(
            "POST",
            f"{base_url}/api/v4/projects/{project_id}/issues",
            token=args.token,
            payload={
                "title": source["title"],
                "description": source.get("description") or "",
                "labels": ",".join(source.get("labels") or []),
                "created_at": source["created_at"],
                "issue_type": source.get("issue_type") or "issue",
            },
        )
        seeded_issues.append(
            {
                "source_iid": source["iid"],
                "source_url": source["web_url"],
                "source_labels": source.get("labels") or [],
                "local_id": local["id"],
                "local_iid": local["iid"],
                "local_url": local["web_url"],
                "title": local["title"],
            }
        )
        print(
            f"Seeded source issue #{source['iid']} as local issue #{local['iid']}: {local['title']}",
            flush=True,
        )

    snapshot = {
        "retrieved_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_project": source_project,
        "source_default_branch": source_branch,
        "source_issues": source_issues,
    }
    manifest = {
        "source": {
            "project_id": SOURCE_PROJECT_ID,
            "path": SOURCE_PROJECT_PATH,
            "web_url": SOURCE_PROJECT_URL,
            "git_url": SOURCE_GIT_URL,
            "default_branch": source_project["default_branch"],
            "commit_sha": source_branch["commit"]["id"],
        },
        "local": {
            "project_id": project_id,
            "path_with_namespace": project["path_with_namespace"],
            "web_url": project["web_url"],
            "default_branch": project["default_branch"],
        },
        "issues": seeded_issues,
    }

    (args.output_dir / "source_snapshot.json").write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (args.output_dir / "seed_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    target = next(item for item in seeded_issues if item["source_iid"] == 8551)
    print(f"Target source issue: {target['source_url']}", flush=True)
    print(f"Target local issue: {target['local_url']}", flush=True)
    print(f"Source repository commit: {manifest['source']['commit_sha']}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
