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
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "generate_install_manifest.py"
REPOSITORY_URL = "https://github.com/dimileeh/aira-agent-workspace-fabric"
GITHUB_ACTIONS_REF_ENV_KEYS = (
    "GITHUB_ACTIONS",
    "GITHUB_EVENT_NAME",
    "GITHUB_REF_NAME",
    "GITHUB_REF_TYPE",
    "GITHUB_SHA",
)


def _write_distribution_fixtures(
    tmp_path: Path,
    *,
    version: str = "0.1.0",
) -> tuple[Path, Path, list[Path]]:
    """Create paired wheel, sdist, and checksum fixtures for manifest tests."""
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
    """Write a release-style SHA-256 checksum file for fixture artifacts."""
    lines = []
    for file in files:
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        lines.append(f"{digest}  dist/{file.name}")
    content = "\n".join(lines) + "\n"
    path.write_text(content, encoding="utf-8")
    return content


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in a test repository."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_git_commit(repo: Path, filename: str, content: str) -> str:
    """Write a file, commit it, and return the resulting commit SHA."""
    (repo / filename).write_text(content, encoding="utf-8")
    _run_git(repo, "add", filename)
    _run_git(repo, "commit", "-m", f"commit {filename}")
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_git_repo_with_release_tag(tmp_path: Path) -> tuple[Path, str]:
    """Create a git repository whose release tag points at HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.name", "AWF Test")
    _run_git(repo, "config", "user.email", "awf@example.com")
    commit = _write_git_commit(repo, "release.txt", "release\n")
    _run_git(repo, "tag", "v0.1.0", commit)
    return repo, commit


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
    commit: str | None = None,
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the manifest generator CLI against temporary release fixtures."""
    output = tmp_path / "awf-install-manifest.json"
    env = os.environ.copy()
    for key in GITHUB_ACTIONS_REF_ENV_KEYS:
        env.pop(key, None)
    if env_overrides is not None:
        env.update(env_overrides)
    command = [
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
    ]
    if commit is not None:
        command.extend(["--commit", commit])
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        cwd=cwd,
        env=env,
        text=True,
    )


def _load_manifest(path: Path) -> dict[str, object]:
    """Load generated manifest JSON from disk."""
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_manifest_generator_emits_deterministic_manifest_from_dist_and_checksums(
    tmp_path: Path,
) -> None:
    """The generator emits stable JSON without mutating checksum inputs."""
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
def test_write_manifest_emits_objects_with_sorted_keys(tmp_path: Path) -> None:
    """write_manifest sorts every object's keys.

    The jq-free awk wheel parser in packaging/install.sh keys off "kind" being
    emitted before an artifact's "name"/"sha256"/"url" fields. Sorted-key output
    guarantees that ordering; if a future change dropped sort_keys=True (even
    though build_manifest happens to assemble keys alphabetically today), the
    installer would silently mis-parse. Feed write_manifest an intentionally
    unsorted dict so this invariant is pinned independent of build order.
    """
    output = tmp_path / "awf-install-manifest.json"
    write_manifest(
        {
            "version": "0.1.0",
            "channel": "stable",
            "artifacts": [
                {"url": "u", "sha256": "s", "name": "n", "kind": "wheel"},
            ],
            "source": {"tag": "v0.1.0", "repository": "r", "commit": None},
        },
        output,
    )
    text = output.read_text(encoding="utf-8")

    # The awk parser relies on "kind" preceding the artifact's other fields.
    assert text.index('"kind"') < text.index('"name"')
    assert text.index('"kind"') < text.index('"sha256"')
    assert text.index('"kind"') < text.index('"url"')

    unsorted_objects: list[list[str]] = []

    def _record_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        keys = [key for key, _ in pairs]
        if keys != sorted(keys):
            unsorted_objects.append(keys)
        return dict(pairs)

    json.loads(text, object_pairs_hook=_record_object)
    assert unsorted_objects == [], f"objects emitted with unsorted keys: {unsorted_objects}"


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
    """Explicit generated_at values must use UTC second-precision Zulu form."""
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
    """Artifact URLs are pinned to immutable GitHub Release tag downloads."""
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
def test_run_generator_ignores_ambient_github_actions_branch_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default subprocess tests do not inherit CI branch refs from pytest."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF_NAME", "development")
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)

    result = _run_generator(tmp_path, dist_dir=dist_dir, checksums_file=checksums_file)

    assert result.returncode == 0, result.stderr
    assert "SKIP:" not in result.stdout
    assert (tmp_path / "awf-install-manifest.json").is_file()


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
def test_manifest_generator_skips_non_dispatch_github_actions_branch_ref_without_writing_manifest(
    tmp_path: Path,
    env_overrides: dict[str, str],
) -> None:
    """Non-dispatch Actions branch refs skip manifest output and remove stale files."""
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
def test_manifest_generator_skips_workflow_dispatch_branch_ref_without_verified_tag(
    tmp_path: Path,
) -> None:
    """Manual branch dispatches skip when tag provenance cannot be verified."""
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)
    commit = "0123456789abcdef0123456789abcdef01234567"
    output = tmp_path / "awf-install-manifest.json"
    output.write_text("stale manifest\n", encoding="utf-8")

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        cwd=tmp_path,
        env_overrides={
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF_NAME": "development",
            "GITHUB_REF_TYPE": "branch",
            "GITHUB_SHA": commit,
        },
    )

    assert result.returncode == 0, result.stderr
    assert "SKIP:" in result.stdout
    assert "could not verify release tag v0.1.0" in result.stdout
    assert not output.exists()


@pytest.mark.unit
def test_manifest_generator_allows_workflow_dispatch_branch_ref_with_matching_tag(
    tmp_path: Path,
) -> None:
    """Manual branch dispatches emit manifests when the release tag matches HEAD."""
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)
    repo, commit = _init_git_repo_with_release_tag(tmp_path)

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        cwd=repo,
        env_overrides={
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF_NAME": "development",
            "GITHUB_REF_TYPE": "branch",
            "GITHUB_SHA": commit,
        },
    )

    assert result.returncode == 0, result.stderr
    assert "SKIP:" not in result.stdout
    manifest = _load_manifest(tmp_path / "awf-install-manifest.json")
    source = manifest["source"]
    assert isinstance(source, dict)
    assert source["commit"] == commit
    assert source["tag"] == "v0.1.0"


@pytest.mark.unit
def test_manifest_generator_skips_workflow_dispatch_branch_ref_with_mismatched_tag(
    tmp_path: Path,
) -> None:
    """Manual branch dispatches skip when the release tag points elsewhere."""
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)
    repo, tag_commit = _init_git_repo_with_release_tag(tmp_path)
    branch_commit = _write_git_commit(repo, "branch.txt", "branch\n")
    output = tmp_path / "awf-install-manifest.json"
    output.write_text("stale manifest\n", encoding="utf-8")

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        cwd=repo,
        env_overrides={
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF_NAME": "development",
            "GITHUB_REF_TYPE": "branch",
            "GITHUB_SHA": branch_commit,
        },
    )

    assert result.returncode == 0, result.stderr
    assert "SKIP:" in result.stdout
    assert f"release tag v0.1.0 resolves to {tag_commit}" in result.stdout
    assert f"GITHUB_SHA is {branch_commit}" in result.stdout
    assert not output.exists()


@pytest.mark.unit
def test_manifest_generator_skips_github_actions_without_ref_metadata(
    tmp_path: Path,
) -> None:
    """Actions runs without ref metadata skip rather than generating unproven manifests."""
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)
    output = tmp_path / "awf-install-manifest.json"
    output.write_text("stale manifest\n", encoding="utf-8")

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        env_overrides={"GITHUB_ACTIONS": "true"},
    )

    assert result.returncode == 0, result.stderr
    assert "SKIP:" in result.stdout
    assert "<unknown>" in result.stdout
    assert "not a release tag" in result.stdout
    assert not output.exists()


@pytest.mark.unit
def test_manifest_generator_allows_github_actions_tag_ref(tmp_path: Path) -> None:
    """Actions tag dispatch for the release tag generates the manifest."""
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
def test_manifest_generator_records_github_sha_when_actions_tag_ref_omits_commit(
    tmp_path: Path,
) -> None:
    """Actions tag builds use GITHUB_SHA as commit provenance by default."""
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)
    commit = "0123456789abcdef0123456789abcdef01234567"

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        env_overrides={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF_NAME": "v0.1.0",
            "GITHUB_REF_TYPE": "tag",
            "GITHUB_SHA": commit,
        },
    )

    assert result.returncode == 0, result.stderr
    manifest = _load_manifest(tmp_path / "awf-install-manifest.json")
    source = manifest["source"]
    assert isinstance(source, dict)
    assert source["commit"] == commit


@pytest.mark.unit
def test_manifest_generator_prefers_explicit_commit_over_github_sha(
    tmp_path: Path,
) -> None:
    """Explicit --commit keeps priority over the Actions environment."""
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)
    commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        commit=commit,
        env_overrides={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF_NAME": "v0.1.0",
            "GITHUB_REF_TYPE": "tag",
            "GITHUB_SHA": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        },
    )

    assert result.returncode == 0, result.stderr
    manifest = _load_manifest(tmp_path / "awf-install-manifest.json")
    source = manifest["source"]
    assert isinstance(source, dict)
    assert source["commit"] == commit


@pytest.mark.unit
def test_manifest_generator_ignores_github_sha_outside_actions(tmp_path: Path) -> None:
    """Local generation does not infer commit provenance from stray env vars."""
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        env_overrides={
            "GITHUB_SHA": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        },
    )

    assert result.returncode == 0, result.stderr
    manifest = _load_manifest(tmp_path / "awf-install-manifest.json")
    source = manifest["source"]
    assert isinstance(source, dict)
    assert source["commit"] is None


@pytest.mark.unit
def test_manifest_normalizes_git_suffix_from_repository_clone_url(tmp_path: Path) -> None:
    """Repository clone URLs lose their .git suffix before URL assembly."""
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
def test_manifest_normalizes_repository_url_path_separators(tmp_path: Path) -> None:
    """Repository URLs with extra path separators are canonicalized before URL assembly."""
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        repository_url="https://github.com//dimileeh///aira-agent-workspace-fabric//",
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
        assert "github.com//" not in artifact["url"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "repository_url",
    [
        f"{REPOSITORY_URL};download=1",
        f"{REPOSITORY_URL}?download=1",
        f"{REPOSITORY_URL}#install",
    ],
)
def test_manifest_rejects_repository_urls_with_suffix_components(
    tmp_path: Path,
    repository_url: str,
) -> None:
    """Repository URLs with params, queries, or fragments are rejected."""
    dist_dir, checksums_file, _files = _write_distribution_fixtures(tmp_path)

    result = _run_generator(
        tmp_path,
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        repository_url=repository_url,
    )

    assert result.returncode == 2
    assert "repository URL must not include params, query, or fragment" in result.stderr
    assert not (tmp_path / "awf-install-manifest.json").exists()


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
    """Checksum files must exactly cover every distribution artifact once."""
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
    """Checksum validation rejects artifacts whose bytes do not match."""
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
    """Distribution artifacts must match the requested package and version."""
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
    """Checksum verification streams artifacts instead of reading them at once."""
    artifact = tmp_path / "agent_workspace_fabric-0.1.0-py3-none-any.whl"
    content = b"wheel fixture\n" * 10_000
    artifact.write_bytes(content)
    expected_digest = hashlib.sha256(content).hexdigest()
    read_sizes: list[int] = []
    original_open = Path.open
    original_read_bytes = Path.read_bytes

    class TrackingReader:
        """File wrapper that records read sizes used by checksum validation."""

        def __init__(self, handle: object) -> None:
            """Wrap the opened artifact handle."""
            self._handle = handle

        def __enter__(self) -> TrackingReader:
            """Return the wrapper as the context manager value."""
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            """Close the wrapped file handle when the context exits."""
            self._handle.close()

        def read(self, size: int = -1) -> bytes:
            """Record bounded read sizes before delegating to the artifact."""
            read_sizes.append(size)
            if size <= 0:
                raise AssertionError("checksum validation must use bounded reads")
            return self._handle.read(size)

    def tracking_open(self: Path, *args: object, **kwargs: object) -> object:
        """Return the tracking wrapper only for the artifact under test."""
        handle = original_open(self, *args, **kwargs)
        if self == artifact:
            return TrackingReader(handle)
        return handle

    def forbidden_read_bytes(self: Path) -> bytes:
        """Fail if checksum validation tries to read the full artifact."""
        if self == artifact:
            raise AssertionError("checksum validation must not load the full artifact")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    _validate_checksum_content([artifact], {artifact.name: expected_digest})

    assert read_sizes


@pytest.mark.unit
def test_manifest_rejects_unexpected_distribution_artifacts(tmp_path: Path) -> None:
    """Only wheels and source distributions are accepted as release artifacts."""
    dist_dir, checksums_file, files = _write_distribution_fixtures(tmp_path)
    unexpected = dist_dir / "agent_workspace_fabric-0.1.0.zip"
    unexpected.write_bytes(b"unexpected\n")
    _write_checksums(checksums_file, [*files, unexpected])

    result = _run_generator(tmp_path, dist_dir=dist_dir, checksums_file=checksums_file)

    assert result.returncode == 2
    assert "unexpected distribution artifact" in result.stderr


@pytest.mark.unit
def test_manifest_records_platform_metadata_for_wheel_and_sdist(tmp_path: Path) -> None:
    """Wheel and sdist entries include their expected platform metadata."""
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
    """SOURCE_DATE_EPOCH produces deterministic UTC generated_at values."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1790553600")

    assert _default_generated_at() == "2026-09-28T00:00:00Z"


@pytest.mark.unit
def test_default_generated_at_rejects_out_of_range_source_date_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid SOURCE_DATE_EPOCH values produce a manifest input error."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1" * 100)

    with pytest.raises(ManifestError, match="Invalid SOURCE_DATE_EPOCH timestamp"):
        _default_generated_at()


@pytest.mark.unit
@pytest.mark.parametrize("version", ["0.1.0", "1.2.3", "2.0.0.post1"])
def test_channel_auto_selects_stable_for_final_versions(version: str) -> None:
    """The auto channel resolves final versions to stable."""
    assert resolve_channel(version, "auto") == "stable"
    assert resolve_channel(version, "stable") == "stable"


@pytest.mark.unit
@pytest.mark.parametrize("version", ["0.2.0a1", "0.2.0b1", "0.2.0rc1", "0.2.0.dev1"])
def test_channel_auto_selects_prerelease_for_prerelease_versions(version: str) -> None:
    """The auto channel resolves prerelease versions to prerelease."""
    assert resolve_channel(version, "auto") == "prerelease"
    assert resolve_channel(version, "prerelease") == "prerelease"

    with pytest.raises(ValueError, match="stable channel requires a final version"):
        resolve_channel(version, "stable")


@pytest.mark.unit
@pytest.mark.parametrize("version", ["0.1.0", "1.2.3"])
def test_channel_prerelease_rejects_final_versions(version: str) -> None:
    """The prerelease channel rejects final versions."""
    with pytest.raises(ValueError, match="prerelease channel requires a prerelease version"):
        resolve_channel(version, "prerelease")
