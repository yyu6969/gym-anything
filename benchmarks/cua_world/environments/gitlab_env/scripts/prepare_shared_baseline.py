#!/usr/bin/env python3
"""Validate the extracted logical dataset before GitLab mutates any state."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

EXPECTED_COUNTS = {
    "branches": 44,
    "comments": 85,
    "excluded": 168,
    "files": 5,
    "groups": 0,
    "issues": 10,
    "labels": 29,
    "merge_requests": 8,
    "milestones": 1,
    "namespaces": 22,
    "project_members": 13,
    "projects": 39,
    "reference_dependencies": 45,
    "repositories": 28,
    "tasks": 172,
    "users": 70,
}

REQUIRED_DOCUMENTS = (
    "branches.json",
    "excluded_initial_entities.json",
    "files.json",
    "groups.json",
    "issues.json",
    "labels.json",
    "manifest.json",
    "merge_requests.json",
    "milestones.json",
    "notes.json",
    "project_members.json",
    "projects.json",
    "reference_dependencies.json",
    "repository_metadata.json",
    "task_index.json",
    "users.json",
    "validation_report.json",
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_heads(path: Path) -> dict[str, str]:
    result = subprocess.run(
        ["git", "bundle", "list-heads", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    heads: dict[str, str] = {}
    for line in result.stdout.splitlines():
        oid, name = line.split(" ", 1)
        heads[name] = oid
    return heads


def validate_dataset(dataset: Path) -> dict[str, Any]:
    failures: list[str] = []
    for name in REQUIRED_DOCUMENTS:
        if not (dataset / name).is_file():
            failures.append(f"missing required document: {name}")

    if failures:
        raise RuntimeError("; ".join(failures))

    manifest = load_json(dataset / "manifest.json")
    if manifest.get("counts") != EXPECTED_COUNTS:
        failures.append(
            f"manifest counts differ: expected {EXPECTED_COUNTS}, got {manifest.get('counts')}"
        )
    validation = load_json(dataset / "validation_report.json")
    if validation.get("status") != "passed":
        failures.append("logical extraction validation_report.json is not passed")
    if manifest.get("validation", {}).get("status") != "passed":
        failures.append("manifest validation status is not passed")

    exclusions = load_json(dataset / "excluded_initial_entities.json")
    categories = exclusions.get("categories", {})
    excluded_count = sum(len(value) for value in categories.values())
    if excluded_count != exclusions.get("total_count") or excluded_count != 168:
        failures.append(
            f"excluded entity count differs: categories={excluded_count}, "
            f"declared={exclusions.get('total_count')}"
        )

    repository_doc = load_json(dataset / "repository_metadata.json")
    repositories = repository_doc.get("records", [])
    if len(repositories) != EXPECTED_COUNTS["repositories"]:
        failures.append(f"expected 28 repository records, got {len(repositories)}")

    verified_repositories: list[dict[str, Any]] = []
    for record in repositories:
        project = record["project"]
        artifact = dataset / record["artifact"]
        if not artifact.is_file():
            failures.append(f"{project}: missing artifact {record['artifact']}")
            continue
        if artifact.stat().st_size != record["size_bytes"]:
            failures.append(
                f"{project}: size {artifact.stat().st_size} != {record['size_bytes']}"
            )
        actual_sha = sha256(artifact)
        if actual_sha != record["sha256"]:
            failures.append(f"{project}: SHA-256 mismatch")
        try:
            heads = bundle_heads(artifact)
        except subprocess.CalledProcessError as exc:
            failures.append(
                f"{project}: git bundle list-heads failed: {exc.stderr.strip()}"
            )
            continue
        expected_heads = {item["name"]: item["oid"] for item in record["refs"]}
        expected_heads["HEAD"] = heads.get("HEAD", "")
        missing = {
            name: oid
            for name, oid in expected_heads.items()
            if name != "HEAD" and heads.get(name) != oid
        }
        if missing:
            failures.append(
                f"{project}: {len(missing)} bundle refs differ or are missing"
            )
        if len(heads) != record["bundle_head_count"]:
            failures.append(
                f"{project}: bundle head count {len(heads)} != {record['bundle_head_count']}"
            )
        verified_repositories.append(
            {
                "project": project,
                "artifact": record["artifact"],
                "sha256": actual_sha,
                "bundle_head_count": len(heads),
            }
        )

    if failures:
        raise RuntimeError("dataset preflight failed:\n- " + "\n- ".join(failures))

    return {
        "schema_version": 1,
        "status": "passed",
        "dataset": manifest["dataset"],
        "manifest_counts": manifest["counts"],
        "excluded_initial_entities": excluded_count,
        "repositories_verified": len(verified_repositories),
        "repository_artifact_bytes": sum(item["size_bytes"] for item in repositories),
        "repositories": verified_repositories,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    report = validate_dataset(args.dataset.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"Dataset preflight passed: {report['repositories_verified']} bundles, "
        f"{report['excluded_initial_entities']} exclusions",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
