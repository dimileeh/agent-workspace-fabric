"""Tests for the AWF release installer smoke generator/runner."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.generate_install_manifest import build_manifest, write_manifest
from scripts.release_smoke import _print_invocation, build_smoke_manifest, smoke_invocation

REPO_ROOT = Path(__file__).resolve().parents[3]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "release_smoke.py"
INSTALLER = REPO_ROOT / "packaging" / "install.sh"
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
    """Create dist fixtures and a matching published manifest."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    wheel = dist_dir / f"agent_workspace_fabric-{version}-py3-none-any.whl"
    sdist = dist_dir / f"agent_workspace_fabric-{version}.tar.gz"
    wheel.write_bytes(b"PK fake awf wheel " + version.encode() + b"\n")
    sdist.write_bytes(b"sdist fixture " + version.encode() + b"\n")

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


@pytest.mark.unit
def test_build_smoke_manifest_rewrites_urls_to_local_dist(tmp_path: Path) -> None:
    """Every artifact url points at the local dist file; identity fields are kept."""
    dist_dir, _checksums_file, manifest_path = _build_release(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    smoke = build_smoke_manifest(manifest, dist_dir)

    assert smoke["version"] == manifest["version"]
    assert smoke["package"] == manifest["package"]
    assert smoke["channel"] == manifest["channel"]
    assert smoke["source"] == manifest["source"]

    for original, rewritten in zip(manifest["artifacts"], smoke["artifacts"], strict=True):
        assert rewritten["name"] == original["name"]
        assert rewritten["kind"] == original["kind"]
        assert rewritten["sha256"] == original["sha256"]
        expected = f"file://{(dist_dir / rewritten['name']).resolve()}"
        assert rewritten["url"] == expected

    # The input manifest is not mutated (deep copy).
    assert manifest["artifacts"][0]["url"].startswith("https://")


@pytest.mark.unit
@pytest.mark.parametrize("method", ["uv", "pipx"])
def test_smoke_invocation_builds_dry_run_command(tmp_path: Path, method: str) -> None:
    """The invocation is a dry-run with method + manifest-derived channel and env."""
    _dist_dir, _checksums_file, manifest_path = _build_release(tmp_path)

    argv, env = smoke_invocation(INSTALLER, manifest_path, channel="stable", method=method)

    assert argv == [
        "bash",
        str(INSTALLER),
        "--dry-run",
        "--method",
        method,
        "--channel",
        "stable",
    ]
    assert env == {"AWF_INSTALL_MANIFEST": str(manifest_path)}


@pytest.mark.unit
def test_release_smoke_uses_prerelease_channel_from_manifest(tmp_path: Path) -> None:
    """A prerelease manifest drives a --channel prerelease invocation."""
    dist_dir, _checksums_file, manifest_path = _build_release(
        tmp_path, version="0.2.0rc1", channel="auto"
    )
    smoke_out = tmp_path / "awf-install-manifest.smoke.json"

    result = _run_smoke(
        dist_dir=dist_dir,
        manifest_path=manifest_path,
        smoke_out=smoke_out,
        methods=["uv"],
        run=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--channel prerelease" in result.stdout


@pytest.mark.unit
def test_print_invocation_shell_quotes_spaces_and_metacharacters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The printed command shell-quotes env values and argv so it stays copy-pasteable."""
    invocation = ["bash", "/path with space/install.sh", "--channel", "stable"]
    env = {"AWF_INSTALL_MANIFEST": "/tmp/dir with space/manifest.json"}

    _print_invocation(invocation, env)

    printed = capsys.readouterr().out.strip()
    assert printed == (
        "$ AWF_INSTALL_MANIFEST='/tmp/dir with space/manifest.json' "
        "bash '/path with space/install.sh' --channel stable"
    )


def _run_smoke(
    *,
    dist_dir: Path,
    manifest_path: Path,
    smoke_out: Path,
    methods: list[str],
    run: bool,
) -> subprocess.CompletedProcess[str]:
    """Run the release smoke CLI."""
    command = [
        sys.executable,
        str(SMOKE_SCRIPT),
        "--dist-dir",
        str(dist_dir),
        "--manifest",
        str(manifest_path),
        "--smoke-manifest-out",
        str(smoke_out),
    ]
    for method in methods:
        command.extend(["--method", method])
    if run:
        command.append("--run")
    return subprocess.run(command, check=False, capture_output=True, text=True)


@pytest.mark.unit
def test_release_smoke_main_writes_manifest_and_prints_commands(tmp_path: Path) -> None:
    """Without --run, the CLI writes the smoke manifest and prints the commands."""
    dist_dir, _checksums_file, manifest_path = _build_release(tmp_path)
    smoke_out = tmp_path / "awf-install-manifest.smoke.json"

    result = _run_smoke(
        dist_dir=dist_dir,
        manifest_path=manifest_path,
        smoke_out=smoke_out,
        methods=["uv"],
        run=False,
    )

    assert result.returncode == 0, result.stderr
    assert smoke_out.is_file()
    smoke = json.loads(smoke_out.read_text(encoding="utf-8"))
    for artifact in smoke["artifacts"]:
        assert artifact["url"].startswith("file://")
    assert "install.sh" in result.stdout
    assert "--dry-run" in result.stdout


@pytest.mark.unit
def test_release_smoke_main_run_verifies_checksum_before_install(tmp_path: Path) -> None:
    """--run drives install.sh dry-run: checksum verified, no install mutation."""
    dist_dir, _checksums_file, manifest_path = _build_release(tmp_path)
    smoke_out = tmp_path / "awf-install-manifest.smoke.json"

    result = _run_smoke(
        dist_dir=dist_dir,
        manifest_path=manifest_path,
        smoke_out=smoke_out,
        methods=["uv"],
        run=True,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "Checksum verified" in result.stdout
    assert "Dry run complete" in result.stdout
    # Dry run must not have mutated anything.
    assert "Installed agent-workspace-fabric" not in result.stdout


@pytest.mark.unit
def test_release_smoke_run_aborts_before_install_on_checksum_mismatch(tmp_path: Path) -> None:
    """A corrupted wheel fails install.sh with CHECKSUM_MISMATCH before any install."""
    dist_dir, _checksums_file, manifest_path = _build_release(tmp_path)
    smoke_out = tmp_path / "awf-install-manifest.smoke.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    smoke = build_smoke_manifest(manifest, dist_dir)
    smoke_out.write_text(json.dumps(smoke, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Corrupt the real wheel bytes so its sha256 no longer matches the manifest.
    wheel = next(dist_dir.glob("*.whl"))
    wheel.write_bytes(b"corrupted wheel bytes\n")

    argv, env = smoke_invocation(INSTALLER, smoke_out, channel=smoke["channel"], method="uv")
    result = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        env={**_clean_env(), **env},
    )

    assert result.returncode != 0
    assert "CHECKSUM_MISMATCH" in result.stderr
    assert "Checksum verified" not in result.stdout


def _clean_env() -> dict[str, str]:
    """Return the current process environment for subprocess inheritance."""
    import os

    return dict(os.environ)
