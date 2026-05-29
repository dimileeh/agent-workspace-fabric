"""Tests for the AWF release install manifest generator."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.generate_install_manifest import (
    ManifestError,
    _default_generated_at,
    _validate_checksum_content,
    resolve_channel,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "generate_install_manifest.py"
REPOSITORY_URL = "https://github.com/dimileeh/aira-agent-workspace-fabric"


def _write_distribution_fixtures(
    tmp_path: Path,
    *,
    version: str = "0.1.0",
) -> tuple[Path, Path, list[Path]]:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    wheel = dist_dir / f"agent_workspace_fabric-{version}-py3-none-any.whl"
    sdist = dist_dir / f"agent_workspace_fabric-{version}.tar.gz"
    wheel.write_bytes(b"wheel fixture\n")
    sdist.write_bytes(b"sdist fixture\n")

    checksums_file = tmp_path / "python-distribution-sha256.txt"
    _write_checksums(checksums_file, [wheel, sdist])
    return dist_dir, checksums_file, [wheel, sdist]


def _write_checksums(path: Path, files: list[Path]) -> str:
    lines = []
    for file in files:
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        lines.append(f"{digest}  dist/{file.name}")
    content = "\n".join(lines) + "\n"
    path.write_text(content, encoding="utf-8")
    return content


def _run_generator(
    tmp_path: Path,
    *,
    dist_dir: Path,
    checksums_file: Path,
    version: str = "0.1.0",
    channel: str = "stable",
    tag: str = "v0.1.0",
    generated_at: str = "2026-05-29T00:00:00Z",
    repository_url: str = REPOSITORY_URL,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    output = tmp_path / "awf-install-manifest.json"
    env = None
    if env_overrides is not None:
        env = os.environ.copy()
        env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dist-dir",
            str(dist_dir),
            "--checksums-file",
            str(checksums_file),
            "--output",
            str(output),
            "--version",
            version,
            "--tag",
            tag,
            "--repository-url",
            repository_url,
            "--channel",
            channel,
            "--generated-at",
            generated_at,
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def _load_manifest(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_manifest_generator_emits_deterministic_manifest_from_dist_and_checksums(
    tmp_path: Path,
) -> None:
    dist_dir, checksums_file, files = _write_distribution_fixtures(tmp_path)
    original_checksums = checksums_file.read_text(encoding="utf-8")

    result = _run_generator(tmp_path, dist_dir=dist_dir, checksums_file=checksums_file)

    assert result.returncode == 0, result.stderr
    assert checksums_file.read_text(encoding="utf-8") == original_checksums
    output = tmp_path / "awf-install-manifest.json"
    text = output.read_text(encoding="utf-8")
    assert text.endswith("\n")
    manifest = _load_manifest(output)

    assert manifest["schema_version"] == 1
    assert manifest["package"] == "agent-workspace-fabric"
    assert manifest["version"] == "0.1.0"
    assert manifest["channel"] == "stable"
    assert manifest["generated_at"] == "2026-05-29T00:00:00Z"
    assert manifest["source"] == {
        "commit": None,
        "repository": REPOSITORY_URL,
        "tag": "v0.1.0",
    }

    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    assert [artifact["name"] for artifact in artifacts] == sorted(file.name for file in files)
    assert [artifact["kind"] for artifact in artifacts] == ["wheel", "sdist"]
    assert json.dumps(manifest, indent=2, sort_keys=True) + "\n" == text


@pytest.mark.unit
@pytest.mark.parametrize(
    "generated_at",
    [
        "2026-05-29",
        "2026-05-29T00:00:00+00:00",
        "not-a-timestamp",
    ],
)
def test_manifest_rejects_malformed_explicit_generated_at(
    tmp_path: Path,
    generated_at: str,
) -> None:
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        generated_at=generated_at,
    )

    assert result.returncode == 2
    assert "generated_at must be UTC RFC3339 format" in result.stderr
    assert not (tmp_path / "awf-install-manifest.json").exists()


@pytest.mark.unit
def test_manifest_artifact_urls_are_pinned_to_release_tag(tmp_path: Path) -> None:
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)

    result = _run_generator(tmp_path, dist_dir=dist_dir, checksums_file=checksums_file)

    assert result.returncode == 0, result.stderr
    manifest = _load_manifest(tmp_path / "awf-install-manifest.json")
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)

    for artifact in artifacts:
        url = artifact["url"]
        name = artifact["name"]
        assert url == f"{REPOSITORY_URL}/releases/download/v0.1.0/{name}"
        assert "/latest/" not in url
        assert "/raw/main/" not in url
        assert "/raw/development/" not in url
        assert "pypi.org" not in url


@pytest.mark.unit
@pytest.mark.parametrize(
    "env_overrides",
    [
        {"GITHUB_ACTIONS": "true", "GITHUB_REF_NAME": "development"},
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF_NAME": "vnext",
            "GITHUB_REF_TYPE": "branch",
        },
    ],
)
def test_manifest_generator_skips_github_actions_branch_ref_without_writing_manifest(
    tmp_path: Path,
    env_overrides: dict[str, str],
) -> None:
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)
    output = tmp_path / "awf-install-manifest.json"
    output.write_text("stale manifest\n", encoding="utf-8")

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        env_overrides=env_overrides,
    )

    assert result.returncode == 0, result.stderr
    assert "SKIP:" in result.stdout
    assert "not a release tag" in result.stdout
    assert not output.exists()


@pytest.mark.unit
def test_manifest_generator_allows_github_actions_tag_ref(tmp_path: Path) -> None:
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        env_overrides={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF_NAME": "v0.1.0",
            "GITHUB_REF_TYPE": "tag",
        },
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "awf-install-manifest.json").is_file()


@pytest.mark.unit
def test_manifest_normalizes_git_suffix_from_repository_clone_url(tmp_path: Path) -> None:
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        repository_url=f"{REPOSITORY_URL}.git",
    )

    assert result.returncode == 0, result.stderr
    manifest = _load_manifest(tmp_path / "awf-install-manifest.json")
    source = manifest["source"]
    assert isinstance(source, dict)
    assert source["repository"] == REPOSITORY_URL

    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    for artifact in artifacts:
        assert artifact["url"].startswith(f"{REPOSITORY_URL}/releases/download/")
        assert ".git/releases/download/" not in artifact["url"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("checksum_text", "expected_error"),
    [
        ("", "missing checksum"),
        ("not-a-sha  dist/agent_workspace_fabric-0.1.0-py3-none-any.whl\n", "malformed"),
        (
            (
                f"{'a' * 64}  dist/agent_workspace_fabric-0.1.0-py3-none-any.whl\n"
                f"{'b' * 64}  dist/agent_workspace_fabric-0.1.0-py3-none-any.whl\n"
            ),
            "duplicate checksum",
        ),
        (
            (
                f"{'a' * 64}  dist/agent_workspace_fabric-0.1.0-py3-none-any.whl\n"
                f"{'b' * 64}  dist/agent_workspace_fabric-0.1.0.tar.gz\n"
                f"{'c' * 64}  dist/stale.whl\n"
            ),
            "checksum references absent",
        ),
    ],
)
def test_manifest_requires_checksums_for_every_distribution_artifact(
    tmp_path: Path,
    checksum_text: str,
    expected_error: str,
) -> None:
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)
    checksums_file.write_text(checksum_text, encoding="utf-8")
    original_checksums = checksums_file.read_text(encoding="utf-8")

    result = _run_generator(tmp_path, dist_dir=dist_dir, checksums_file=checksums_file)

    assert result.returncode == 2
    assert expected_error in result.stderr
    assert checksums_file.read_text(encoding="utf-8") == original_checksums
    assert not (tmp_path / "awf-install-manifest.json").exists()


@pytest.mark.unit
def test_manifest_rejects_checksum_that_does_not_match_distribution_content(
    tmp_path: Path,
) -> None:
    dist_dir, checksums_file, files = _write_distribution_fixtures(tmp_path)
    wrong_digest = "0" * 64
    sdist_digest = hashlib.sha256(files[1].read_bytes()).hexdigest()
    checksums_file.write_text(
        (f"{wrong_digest}  dist/{files[0].name}\n{sdist_digest}  dist/{files[1].name}\n"),
        encoding="utf-8",
    )

    result = _run_generator(tmp_path, dist_dir=dist_dir, checksums_file=checksums_file)

    assert result.returncode == 2
    assert "checksum does not match distribution artifact" in result.stderr
    assert not (tmp_path / "awf-install-manifest.json").exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    "stale_artifact_name",
    [
        "agent_workspace_fabric-0.0.9-py3-none-any.whl",
        "agent_workspace_fabric-0.0.9.tar.gz",
        "other_package-0.1.0-py3-none-any.whl",
        "other_package-0.1.0.tar.gz",
    ],
)
def test_manifest_rejects_stale_distribution_artifact_package_or_version(
    tmp_path: Path,
    stale_artifact_name: str,
) -> None:
    dist_dir, checksums_file, files = _write_distribution_fixtures(tmp_path)
    stale_artifact = dist_dir / stale_artifact_name
    stale_artifact.write_bytes(b"stale artifact\n")
    _write_checksums(checksums_file, [*files, stale_artifact])

    result = _run_generator(tmp_path, dist_dir=dist_dir, checksums_file=checksums_file)

    assert result.returncode == 2
    assert "does not match requested package/version" in result.stderr
    assert stale_artifact_name in result.stderr
    assert not (tmp_path / "awf-install-manifest.json").exists()


@pytest.mark.unit
def test_manifest_hashes_distribution_artifacts_with_bounded_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "agent_workspace_fabric-0.1.0-py3-none-any.whl"
    content = b"wheel fixture\n" * 10_000
    artifact.write_bytes(content)
    expected_digest = hashlib.sha256(content).hexdigest()
    read_sizes: list[int] = []
    original_open = Path.open
    original_read_bytes = Path.read_bytes

    class TrackingReader:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def __enter__(self) -> TrackingReader:
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            self._handle.close()

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            if size <= 0:
                raise AssertionError("checksum validation must use bounded reads")
            return self._handle.read(size)

    def tracking_open(self: Path, *args: object, **kwargs: object) -> object:
        handle = original_open(self, *args, **kwargs)
        if self == artifact:
            return TrackingReader(handle)
        return handle

    def forbidden_read_bytes(self: Path) -> bytes:
        if self == artifact:
            raise AssertionError("checksum validation must not load the full artifact")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    _validate_checksum_content([artifact], {artifact.name: expected_digest})

    assert read_sizes


@pytest.mark.unit
def test_manifest_rejects_unexpected_distribution_artifacts(tmp_path: Path) -> None:
    dist_dir, checksums_file, files = _write_distribution_fixtures(tmp_path)
    unexpected = dist_dir / "agent_workspace_fabric-0.1.0.zip"
    unexpected.write_bytes(b"unexpected\n")
    _write_checksums(checksums_file, [*files, unexpected])

    result = _run_generator(tmp_path, dist_dir=dist_dir, checksums_file=checksums_file)

    assert result.returncode == 2
    assert "unexpected distribution artifact" in result.stderr


@pytest.mark.unit
def test_manifest_records_platform_metadata_for_wheel_and_sdist(tmp_path: Path) -> None:
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)

    result = _run_generator(tmp_path, dist_dir=dist_dir, checksums_file=checksums_file)

    assert result.returncode == 0, result.stderr
    artifacts = _load_manifest(tmp_path / "awf-install-manifest.json")["artifacts"]
    assert isinstance(artifacts, list)

    by_kind = {artifact["kind"]: artifact for artifact in artifacts}
    assert by_kind["wheel"]["platform"] == {
        "arch": "any",
        "os": "any",
        "python": ">=3.12",
    }
    assert by_kind["sdist"]["platform"] == {
        "arch": "source",
        "os": "source",
        "python": ">=3.12",
    }
    assert by_kind["wheel"]["signatures"] == []
    assert by_kind["sdist"]["signatures"] == []


@pytest.mark.unit
def test_default_generated_at_uses_source_date_epoch_as_zulu_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1790553600")

    assert _default_generated_at() == "2026-09-28T00:00:00Z"


@pytest.mark.unit
def test_default_generated_at_rejects_out_of_range_source_date_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1" * 100)

    with pytest.raises(ManifestError, match="Invalid SOURCE_DATE_EPOCH timestamp"):
        _default_generated_at()


@pytest.mark.unit
@pytest.mark.parametrize("version", ["0.1.0", "1.2.3", "2.0.0.post1"])
def test_channel_auto_selects_stable_for_final_versions(version: str) -> None:
    assert resolve_channel(version, "auto") == "stable"
    assert resolve_channel(version, "stable") == "stable"


@pytest.mark.unit
@pytest.mark.parametrize("version", ["0.2.0a1", "0.2.0b1", "0.2.0rc1", "0.2.0.dev1"])
def test_channel_auto_selects_prerelease_for_prerelease_versions(version: str) -> None:
    assert resolve_channel(version, "auto") == "prerelease"
    assert resolve_channel(version, "prerelease") == "prerelease"

    with pytest.raises(ValueError, match="stable channel requires a final version"):
        resolve_channel(version, "stable")
