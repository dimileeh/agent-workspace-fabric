"""Release workflow tests for manifest and checksum artifact publication."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _publish_workflow() -> dict[str, Any]:
    """Load the publish workflow YAML as a mapping."""
    loaded = yaml.safe_load(PUBLISH_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _job(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a named workflow job mapping."""
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get(name)
    assert isinstance(job, dict)
    return job


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Return mapping steps from a workflow job."""
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _run_steps(job: dict[str, Any]) -> str:
    """Concatenate shell command bodies from workflow steps."""
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _step_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a workflow step by display name."""
    for step in _steps(job):
        if step.get("name") == name:
            return step
    raise AssertionError(f"missing workflow step named {name!r}")


def _uploaded_paths(job: dict[str, Any]) -> set[str]:
    """Collect artifact upload paths declared by a workflow job."""
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
    """The publish workflow creates and uploads both checksum and manifest files."""
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


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the workflow jobs mapping."""
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    return jobs


@pytest.mark.unit
def test_publish_workflow_build_verifies_release_artifact_drift() -> None:
    """The build job runs the drift gate over the generated manifest and checksums."""
    build_job = _job(_publish_workflow(), "build")
    commands = _run_steps(build_job)

    assert "scripts/check_release_artifacts.py" in commands
    assert "--manifest artifacts/release/awf-install-manifest.json" in commands
    assert "--checksums-file artifacts/release/python-distribution-sha256.txt" in commands
    assert "--dist-dir dist" in commands

    # The drift check must run after the manifest is generated.
    step_names = [str(step.get("name")) for step in _steps(build_job)]
    assert "Verify release artifact drift" in step_names


@pytest.mark.unit
def test_publish_workflow_has_installer_smoke_job_consuming_release_artifacts() -> None:
    """A dedicated installer-smoke job consumes the uploaded artifacts and runs install.sh."""
    workflow = _publish_workflow()
    jobs = _jobs(workflow)
    assert {"build", "installer-smoke", "publish"}.issubset(jobs)

    smoke_job = _job(workflow, "installer-smoke")
    needs = smoke_job.get("needs")
    needs_set = {needs} if isinstance(needs, str) else set(needs or [])
    assert "build" in needs_set

    # Downloads the two release artifacts built by the build job.
    downloaded = {
        step.get("with", {}).get("name")
        for step in _steps(smoke_job)
        if step.get("uses") == "actions/download-artifact@v4"
    }
    assert "python-distributions" in downloaded
    assert "python-distribution-checksums" in downloaded

    # Checks out the repo (for install.sh + scripts) and runs the smoke generator.
    assert any(
        str(step.get("uses", "")).startswith("actions/checkout") for step in _steps(smoke_job)
    )
    commands = _run_steps(smoke_job)
    assert "scripts/release_smoke.py" in commands
    assert "--run" in commands
    # The smoke must be driven via the script, never a raw install.sh invocation.
    # Ignore comment lines (the script's purpose is documented in a comment that
    # mentions install.sh); only executable command lines are asserted against.
    executable_commands = "\n".join(
        line for line in commands.splitlines() if not line.lstrip().startswith("#")
    )
    assert "packaging/install.sh" not in executable_commands


@pytest.mark.unit
def test_publish_workflow_installer_smoke_guarded_on_generated_manifest() -> None:
    """installer-smoke must skip when the build job did not produce a manifest.

    On non-release refs ``generate_install_manifest.py`` SKIPs and removes the
    manifest while the checksum file survives, so the upload still succeeds.
    ``release_smoke.py`` hard-fails without the manifest, so the job has to be
    gated on a build-job output rather than run unconditionally.
    """
    workflow = _publish_workflow()
    build_job = _job(workflow, "build")

    # The build job exposes a manifest_generated output wired from the build step.
    outputs = build_job.get("outputs", {})
    assert isinstance(outputs, dict)
    assert "manifest_generated" in outputs

    build_step = _step_named(build_job, "Build wheel and sdist")
    assert build_step.get("id") == "build"
    assert str(outputs["manifest_generated"]) == ("${{ steps.build.outputs.manifest_generated }}")

    build_commands = _run_steps(build_job)
    assert 'echo "manifest_generated=true" >> "${GITHUB_OUTPUT}"' in build_commands
    assert 'echo "manifest_generated=false" >> "${GITHUB_OUTPUT}"' in build_commands

    # The installer-smoke job only runs when a manifest was generated.
    smoke_job = _job(workflow, "installer-smoke")
    condition = str(smoke_job.get("if", ""))
    assert "needs.build.outputs.manifest_generated == 'true'" in condition


@pytest.mark.unit
def test_publish_workflow_publish_job_keeps_manual_trusted_publishing_gate() -> None:
    """The publish job stays gated on manual dispatch with a non-none target (AC4)."""
    workflow = _publish_workflow()
    publish_job = _job(workflow, "publish")

    condition = str(publish_job.get("if", ""))
    assert "workflow_dispatch" in condition
    assert "inputs.publish_target != 'none'" in condition

    publish_steps = _steps(publish_job)
    assert any(
        step.get("uses") == "pypa/gh-action-pypi-publish@release/v1" for step in publish_steps
    )


@pytest.mark.unit
def test_publish_workflow_publish_job_gated_on_installer_smoke() -> None:
    """Publishing must depend on installer-smoke so a failed smoke check blocks release."""
    workflow = _publish_workflow()
    publish_job = _job(workflow, "publish")

    needs = publish_job.get("needs")
    needs_set = {needs} if isinstance(needs, str) else set(needs or [])
    assert "build" in needs_set
    assert "installer-smoke" in needs_set


@pytest.mark.unit
def test_publish_workflow_does_not_treat_v_prefixed_branch_as_release_tag() -> None:
    """Manual dispatch from a v-prefixed branch must still use the version tag."""
    build_job = _job(_publish_workflow(), "build")
    checkout = _step_named(build_job, "Checkout source")
    checkout_config = checkout.get("with", {})
    assert isinstance(checkout_config, dict)
    assert checkout_config.get("fetch-tags") is True

    commands = _run_steps(build_job)
    assert 'if [[ "${GITHUB_REF_TYPE:-}" == "tag" && "${GITHUB_REF_NAME:-}" == v* ]]' in commands
    assert 'TAG="${GITHUB_REF_NAME}"' in commands
    assert 'TAG="v${VERSION}"' in commands
    assert 'TAG="${GITHUB_REF_NAME:-v${VERSION}}"' not in commands
