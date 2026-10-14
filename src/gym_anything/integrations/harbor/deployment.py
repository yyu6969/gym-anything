"""Fail-fast checks for the Sandweave container deployment, not a runner."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import socket


def prepare_sandweave_worker(config: dict, artifact_root: Path) -> dict:
    artifact_root.mkdir(parents=True, exist_ok=True)
    report = {"status": "checking", "required_version": config["sandweave_version"]}
    path = artifact_root / "bootstrap.json"
    try:
        version = importlib.metadata.version("sandweave")
        if version != config["sandweave_version"]:
            raise RuntimeError(f"Sandweave deployment requires {config['sandweave_version']}, found {version}")
        if not shutil.which("apptainer"):
            raise RuntimeError("Sandweave worker requires Apptainer in the controller image")
        report["sandweave_version"] = version
        for name in ("SANDWEAVE_HOME", "SANDWEAVE_LOCAL_DIR", "APPTAINER_TMPDIR", "APPTAINER_CACHEDIR"):
            value = os.environ.get(name)
            if not value:
                raise RuntimeError(f"Sandweave deployment must configure {name}")
            directory = Path(value)
            directory.mkdir(parents=True, exist_ok=True)
            if directory.stat().st_uid != os.getuid():
                raise RuntimeError(f"Sandweave storage must be owned by the controller user: {directory}")
            report[name] = str(directory)
        from sandweave.releases import check_host
        # This runs the SDK's actual Apptainer namespace/seccomp probe before
        # downloading a guest filesystem. It cannot repair host security policy.
        report["platform"] = check_host(Path(os.environ["SANDWEAVE_HOME"]))
        report["status"] = "passed"
        return report
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        path.write_text(json.dumps(report, indent=2) + "\n")


def collect_sandweave_logs(artifact_root: Path) -> None:
    """Keep this controller's worker log, without exporting connection tokens."""
    cache = os.environ.get("SANDWEAVE_HOME")
    if not cache:
        return
    destination = artifact_root / "worker-logs"
    for metadata in (Path(cache) / "connections").glob("*/worker.json"):
        record = json.loads(metadata.read_text())
        log = metadata.with_name("worker.log")
        if record.get("hostname") == socket.gethostname() and log.is_file():
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(log, destination / (metadata.parent.name + ".log"))
