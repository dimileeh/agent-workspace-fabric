"""Tests for the AWF release artifact drift gate."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_release_artifacts import check_release_artifacts
from scripts.generate_install_manifest import ManifestError, build_manifest, write_manifest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_release_artifacts.py"
REPOSITORY_URL = "https://github.com/dimileeh/aira-agent-workspace-fabric"
GENERATED_AT = "2026-05-29T00:00:00Z"


def _write_checksums(path: Path, files: list[Path]) -> None:
    """Write a release-style SHA-256 checksum file for fixture artifacts."""
    lines = []
    for file in files:
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        lines.append(f"{digest}  dist/{file.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_release(
    tmp_path: Path,
    *,
    version: str = "0.1.0",
    channel: str = "auto",
) -> tuple[Path, Path, Path]:
    """Create paired dist/checksum fixtures and a matching published manifest."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    wheel = dist_dir / f"agent_workspace_fabric-{version}-py3-none-any.whl"
    sdist = dist_dir / f"agent_workspace_fabric-{version}.tar.gz"
    wheel.write_bytes(b"wheel fixture\n")
    sdist.write_bytes(b"sdist fixture\n")

    checksums_file = tmp_path / "python-distribution-sha256.txt"
    _write_checksums(checksums_file, [wheel, sdist])

    manifest = build_manifest(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        version=version,
        tag=f"v{version}",
        repository_url=REPOSITORY_URL,
        channel=channel,
        generated_at=GENERATED_AT,
    )
    manifest_path = tmp_path / "awf-install-manifest.json"
    write_manifest(manifest, manifest_path)
    return dist_dir, checksums_file, manifest_path


def _run_check(
    *,
    dist_dir: Path,
    checksums_file: Path,
    manifest_path: Path,
    version: str | None = None,
    tag: str | None = None,
    repository_url: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the drift-gate CLI against fixtures."""
    command = [
        sys.executable,
        str(CHECK_SCRIPT),
        "--dist-dir",
        str(dist_dir),
        "--checksums-file",
        str(checksums_file),
        "--manifest",
        str(manifest_path),
    ]
    if version is not None:
        command.extend(["--version", version])
    if tag is not None:
        command.extend(["--tag", tag])
    if repository_url is not None:
        command.extend(["--repository-url", repository_url])
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _tamper(manifest_path: Path, mutate: object) -> None:
    """Load, mutate, and rewrite the published manifest JSON."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)  # type: ignore[operator]
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.mark.unit
def test_drift_gate_passes_for_consistent_artifacts(tmp_path: Path) -> None:
    """A freshly generated manifest over its own dist/checksums passes."""
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)

    result = _run_check(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        manifest_path=manifest_path,
        version="0.1.0",
        tag="v0.1.0",
        repository_url=REPOSITORY_URL,
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


@pytest.mark.unit
def test_drift_gate_passes_when_release_metadata_derived_from_manifest(tmp_path: Path) -> None:
    """Version/tag/repository default to the published manifest when flags are omitted."""
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)

    result = _run_check(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        manifest_path=manifest_path,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_drift_gate_fails_on_tampered_recorded_sha256(tmp_path: Path) -> None:
    """A doctored artifact sha256 fails and the diff names the artifact."""
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)

    def _mutate(manifest: dict[str, object]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, list)
        for artifact in artifacts:
            if artifact["kind"] == "wheel":
                artifact["sha256"] = "0" * 64

    _tamper(manifest_path, _mutate)

    result = _run_check(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        manifest_path=manifest_path,
        version="0.1.0",
        tag="v0.1.0",
    )

    assert result.returncode != 0
    assert "agent_workspace_fabric-0.1.0-py3-none-any.whl" in result.stderr
    assert "sha256" in result.stderr


@pytest.mark.unit
def test_drift_gate_fails_when_manifest_omits_a_dist_artifact(tmp_path: Path) -> None:
    """A manifest missing an artifact that exists in dist is drift."""
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)

    def _mutate(manifest: dict[str, object]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, list)
        manifest["artifacts"] = [a for a in artifacts if a["kind"] != "sdist"]

    _tamper(manifest_path, _mutate)

    result = _run_check(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        manifest_path=manifest_path,
        version="0.1.0",
        tag="v0.1.0",
    )

    assert result.returncode != 0
    assert "agent_workspace_fabric-0.1.0.tar.gz" in result.stderr


@pytest.mark.unit
def test_drift_gate_fails_when_manifest_has_extra_artifact(tmp_path: Path) -> None:
    """A manifest artifact absent from dist is drift."""
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)

    def _mutate(manifest: dict[str, object]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, list)
        artifacts.append(
            {
                "kind": "wheel",
                "name": "ghost-9.9.9-py3-none-any.whl",
                "platform": {"arch": "any", "os": "any", "python": ">=3.12"},
                "sha256": "0" * 64,
                "signatures": [],
                "url": f"{REPOSITORY_URL}/releases/download/v0.1.0/ghost-9.9.9-py3-none-any.whl",
            }
        )

    _tamper(manifest_path, _mutate)

    result = _run_check(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        manifest_path=manifest_path,
        version="0.1.0",
        tag="v0.1.0",
    )

    assert result.returncode != 0
    assert "ghost-9.9.9-py3-none-any.whl" in result.stderr


@pytest.mark.unit
def test_drift_gate_fails_on_duplicate_artifact_name(tmp_path: Path) -> None:
    """A duplicated wheel entry with a stale URL is drift, not silently collapsed.

    Indexing artifacts by name would otherwise keep only the last entry, letting an
    extra earlier wheel object with a stale/broken URL escape the comparison.
    """
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)

    def _mutate(manifest: dict[str, object]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, list)
        for artifact in artifacts:
            if artifact["kind"] == "wheel":
                stale = dict(artifact)
                stale["url"] = f"{REPOSITORY_URL}/releases/download/v0.0.1/{stale['name']}"
                artifacts.insert(0, stale)
                break

    _tamper(manifest_path, _mutate)

    result = _run_check(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        manifest_path=manifest_path,
        version="0.1.0",
        tag="v0.1.0",
    )

    assert result.returncode != 0
    assert "agent_workspace_fabric-0.1.0-py3-none-any.whl" in result.stderr
    assert "duplicate" in result.stderr.lower()


@pytest.mark.unit
def test_drift_gate_fails_when_checksum_file_digest_edited(tmp_path: Path) -> None:
    """An edited checksum digest fails independently of the manifest."""
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)
    lines = checksums_file.read_text(encoding="utf-8").splitlines()
    edited = []
    for line in lines:
        digest, rest = line.split(maxsplit=1)
        if rest.endswith(".whl"):
            edited.append(f"{'0' * 64}  {rest}")
        else:
            edited.append(line)
    checksums_file.write_text("\n".join(edited) + "\n", encoding="utf-8")

    result = _run_check(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        manifest_path=manifest_path,
        version="0.1.0",
        tag="v0.1.0",
    )

    assert result.returncode != 0
    assert "checksum" in result.stderr.lower()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("channel", "prerelease", "prerelease"),
        ("version", "0.2.0", "0.2.0"),
    ],
)
def test_drift_gate_fails_on_channel_or_version_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
    needle: str,
) -> None:
    """A channel/version that disagrees with the built dist is drift."""
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)

    def _mutate(manifest: dict[str, object]) -> None:
        manifest[field] = value

    _tamper(manifest_path, _mutate)

    result = _run_check(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        manifest_path=manifest_path,
    )

    assert result.returncode != 0
    assert needle in result.stderr


@pytest.mark.unit
def test_drift_gate_fails_on_source_tag_mismatch(tmp_path: Path) -> None:
    """A doctored source.tag fails the gate."""
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)

    def _mutate(manifest: dict[str, object]) -> None:
        source = manifest["source"]
        assert isinstance(source, dict)
        source["tag"] = "v9.9.9"

    _tamper(manifest_path, _mutate)

    result = _run_check(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        manifest_path=manifest_path,
        version="0.1.0",
        tag="v0.1.0",
    )

    assert result.returncode != 0
    assert "tag" in result.stderr


@pytest.mark.unit
def test_check_release_artifacts_raises_on_drift(tmp_path: Path) -> None:
    """The importable entrypoint raises ManifestError describing the drift."""
    dist_dir, checksums_file, manifest_path = _build_release(tmp_path)

    def _mutate(manifest: dict[str, object]) -> None:
        manifest["package"] = "evil-package"

    _tamper(manifest_path, _mutate)

    with pytest.raises(ManifestError) as excinfo:
        check_release_artifacts(
            dist_dir=dist_dir,
            checksums_file=checksums_file,
            manifest_path=manifest_path,
        )

    assert "package" in str(excinfo.value)
