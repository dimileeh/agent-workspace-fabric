"""Release workflow tests for manifest and checksum artifact publication."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _publish_workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _job(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get(name)
    assert isinstance(job, dict)
    return job


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _run_steps(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _uploaded_paths(job: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for step in _steps(job):
        if step.get("uses") != "actions/upload-artifact@v4":
            continue
        with_config = step.get("with", {})
        assert isinstance(with_config, dict)
        raw_path = with_config.get("path")
        if isinstance(raw_path, str):
            paths.update(line.strip() for line in raw_path.splitlines() if line.strip())
        elif isinstance(raw_path, list):
            paths.update(str(line).strip() for line in raw_path if str(line).strip())
    return paths


@pytest.mark.unit
def test_publish_workflow_generates_manifest_without_removing_checksum_artifact() -> None:
    build_job = _job(_publish_workflow(), "build")
    commands = _run_steps(build_job)

    assert "sha256sum dist/* | tee artifacts/release/python-distribution-sha256.txt" in commands
    assert "scripts/generate_install_manifest.py" in commands
    assert "--checksums-file artifacts/release/python-distribution-sha256.txt" in commands
    assert "--output artifacts/release/awf-install-manifest.json" in commands
    assert "--channel auto" in commands

    uploaded_paths = _uploaded_paths(build_job)
    assert "artifacts/release/python-distribution-sha256.txt" in uploaded_paths
    assert "artifacts/release/awf-install-manifest.json" in uploaded_paths
