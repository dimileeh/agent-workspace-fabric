"""Unit tests for the first-run clean-install/source-lane smoke harness."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from awf.host_setup.source_assets import SOURCE_CHECKOUT_MARKERS
from scripts import first_run_smoke as smoke


@pytest.mark.unit
@pytest.mark.parametrize("method", ["uv", "pipx"])
def test_installer_fixture_command_is_local_dry_run(tmp_path: Path, method: str) -> None:
    """The fixture installer lane uses local file URLs and never plans a real install."""
    installer = smoke.DEFAULT_INSTALLER

    command = smoke.installer_fixture_command(tmp_path, method=method, installer=installer)

    assert command.argv == (
        "bash",
        str(installer),
        "--dry-run",
        "--method",
        method,
        "--channel",
        "stable",
    )
    assert command.env == {"AWF_INSTALL_MANIFEST": str(tmp_path / "fixture-manifest.json")}

    manifest = json.loads((tmp_path / "fixture-manifest.json").read_text(encoding="utf-8"))
    wheel = tmp_path / "dist" / "agent_workspace_fabric-0.1.0-py3-none-any.whl"
    wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel_artifact = next(
        artifact for artifact in manifest["artifacts"] if artifact["kind"] == "wheel"
    )

    assert manifest["channel"] == "stable"
    assert wheel_artifact["url"] == f"file://{wheel.resolve()}"
    assert wheel_artifact["sha256"] == wheel_digest
    assert "tool install" not in " ".join(command.argv)


@pytest.mark.unit
def test_release_lane_skips_without_explicit_gate(tmp_path: Path) -> None:
    """Release smoke never hits artifacts/network unless the release gate is explicit."""
    calls: list[smoke.CommandSpec] = []

    results = smoke.run_installer_release_lane(
        smoke_root=tmp_path,
        methods=("uv",),
        allow_release=False,
        release_dist_dir=None,
        release_manifest=None,
        installer=smoke.DEFAULT_INSTALLER,
        timeout_seconds=5,
        runner=lambda command, timeout_seconds: _record_run(calls, command, timeout_seconds),
    )

    assert calls == []
    assert results == (
        smoke.SmokeResult(
            lane=smoke.Lane.INSTALLER_RELEASE,
            status="skipped",
            reason="release lane requires --allow-release, --release-dist-dir, and --release-manifest",
        ),
    )


@pytest.mark.unit
def test_release_lane_uses_local_release_manifest_when_gated(tmp_path: Path) -> None:
    """A gated release lane rewrites artifacts to local file URLs before dry-run."""
    dist_dir = tmp_path / "dist"
    manifest_path = _write_release_fixture(dist_dir)
    calls: list[smoke.CommandSpec] = []

    results = smoke.run_installer_release_lane(
        smoke_root=tmp_path / "smoke",
        methods=("uv",),
        allow_release=True,
        release_dist_dir=dist_dir,
        release_manifest=manifest_path,
        installer=smoke.DEFAULT_INSTALLER,
        timeout_seconds=5,
        runner=lambda command, timeout_seconds: _record_run(calls, command, timeout_seconds),
    )

    assert [result.status for result in results] == ["passed"]
    assert len(calls) == 1
    command = calls[0]
    smoke_manifest = Path(command.env["AWF_INSTALL_MANIFEST"])
    rewritten = json.loads(smoke_manifest.read_text(encoding="utf-8"))

    assert command.argv == (
        "bash",
        str(smoke.DEFAULT_INSTALLER),
        "--dry-run",
        "--method",
        "uv",
        "--channel",
        "stable",
    )
    assert rewritten["artifacts"][0]["url"].startswith("file://")
    assert str(dist_dir.resolve()) in rewritten["artifacts"][0]["url"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "home_key", "bin_key"),
    (("uv", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR"), ("pipx", "PIPX_HOME", "PIPX_BIN_DIR")),
)
def test_tool_install_environment_isolated(
    tmp_path: Path,
    method: str,
    home_key: str,
    bin_key: str,
) -> None:
    """Tool install lanes pin HOME, tool home, tool bin, cache, and PATH under temp."""
    env = smoke.isolated_tool_environment(
        tmp_path,
        method=method,
        base_env={"HOME": "/home/operator", "PATH": "/usr/bin", "PYTHONPATH": "/repo/src"},
    )

    assert env["HOME"] == str(tmp_path / "home")
    assert env[home_key].startswith(str(tmp_path))
    assert env[bin_key] == str(tmp_path / "bin")
    assert env["UV_CACHE_DIR"] == str(tmp_path / "uv-cache")
    assert env["PATH"].split(":")[0] == str(tmp_path / "bin")
    assert "/home/operator" not in env[home_key]
    assert "PYTHONPATH" not in env


@pytest.mark.unit
def test_copy_source_checkout_preserves_markers_and_excludes_dev_state(tmp_path: Path) -> None:
    """The copied checkout keeps source markers but drops git, caches, and build state."""
    source = _write_source_checkout(tmp_path / "source")
    for ignored in (".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build"):
        ignored_path = source / ignored
        ignored_path.mkdir(parents=True)
        (ignored_path / "sentinel").write_text("ignored\n", encoding="utf-8")

    copied = smoke.copy_source_checkout(source, tmp_path / "copied")

    assert (copied / "pyproject.toml").is_file()
    assert (copied / "src" / "awf" / "__init__.py").is_file()
    assert (copied / "docker" / "control-plane.Dockerfile").is_file()
    assert (copied / "docker" / "compose" / "local-service.yml").is_file()
    for ignored in (".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build"):
        assert not (copied / ignored).exists(), ignored


@pytest.mark.unit
def test_source_uv_run_commands_use_project_and_outside_cwd(tmp_path: Path) -> None:
    """No-global source lane runs uv from outside the checkout with scrubbed env."""
    checkout = tmp_path / "checkout"
    outside = tmp_path / "outside"
    root = tmp_path / "smoke"
    outside.mkdir()

    commands = smoke.source_uv_run_commands(
        checkout=checkout,
        outside_cwd=outside,
        smoke_root=root,
        python="3.12",
        base_env={"PATH": "/usr/bin", "PYTHONPATH": "/repo/src"},
    )

    assert commands[0].argv == (
        "uv",
        "run",
        "--project",
        str(checkout),
        "--python",
        "3.12",
        "--extra",
        "dev",
        "awf",
        "--help",
    )
    assert commands[-1].argv[-6:] == (
        "setup",
        "--dry-run",
        "--source-checkout",
        str(checkout),
        "--format",
        "json",
    )
    for command in commands:
        assert command.cwd == outside
        assert command.env["HOME"] == str(root / "home")
        assert "PYTHONPATH" not in command.env


@pytest.mark.unit
def test_run_command_reports_timeout_as_failed_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Timeouts are reported as smoke command failures instead of crashing."""

    def timeout_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=("awf", "--help"),
            timeout=2.5,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr(smoke.subprocess, "run", timeout_run)
    command = smoke.CommandSpec(argv=("awf", "--help"), env={}, cwd=tmp_path)

    completed = smoke.run_command(command, timeout_seconds=2.5)

    assert completed.args == command.argv
    assert completed.returncode == 124
    assert completed.stdout == "partial stdout"
    assert completed.stderr == "command timed out after 2.5 seconds\npartial stderr"


def _record_run(
    calls: list[smoke.CommandSpec],
    command: smoke.CommandSpec,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    calls.append(command)
    return subprocess.CompletedProcess(
        args=command.argv,
        returncode=0,
        stdout="Checksum verified\nDry run complete\n",
        stderr=f"timeout={timeout_seconds}\n",
    )


def _write_release_fixture(dist_dir: Path) -> Path:
    dist_dir.mkdir(parents=True)
    wheel = dist_dir / "agent_workspace_fabric-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"fixture wheel\n")
    manifest = {
        "artifacts": [
            {
                "kind": "wheel",
                "name": wheel.name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "url": "https://example.invalid/release/wheel.whl",
            }
        ],
        "channel": "stable",
        "generated_at": "2026-05-29T00:00:00Z",
        "package": "agent-workspace-fabric",
        "schema_version": 1,
        "source": {
            "commit": None,
            "repository": "https://github.com/dimileeh/aira-agent-workspace-fabric",
            "tag": "v0.1.0",
        },
        "version": "0.1.0",
    }
    manifest_path = dist_dir / "awf-install-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


def _write_source_checkout(root: Path) -> Path:
    for marker in SOURCE_CHECKOUT_MARKERS:
        target = root / marker.path
        if marker.kind == "dir":
            target.mkdir(parents=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n", encoding="utf-8")
    return root
