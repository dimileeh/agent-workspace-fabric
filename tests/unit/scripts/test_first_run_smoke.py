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
    wheel = tmp_path / "dist" / f"agent_workspace_fabric-{smoke._FIXTURE_VERSION}-py3-none-any.whl"
    wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel_artifact = next(
        artifact for artifact in manifest["artifacts"] if artifact["kind"] == "wheel"
    )

    assert manifest["channel"] == "stable"
    assert manifest["version"] == smoke._FIXTURE_VERSION
    assert manifest["source"]["tag"] == f"v{smoke._FIXTURE_VERSION}"
    assert wheel_artifact["url"] == f"file://{wheel.resolve()}"
    assert wheel_artifact["name"] == wheel.name
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
@pytest.mark.parametrize(
    ("method", "expected_prefix"),
    (
        ("uv", ("uv", "tool", "install", "--force", "--python", "3.12")),
        ("pipx", ("pipx", "install", "--force", "--python", "3.12")),
    ),
)
def test_tool_install_command_pins_requested_python(
    tmp_path: Path,
    method: str,
    expected_prefix: tuple[str, ...],
) -> None:
    """Tool installs use the caller-selected Python for every install method."""
    wheel = tmp_path / "agent_workspace_fabric-0.1.0-py3-none-any.whl"

    argv = smoke._tool_install_argv(method, wheel, python="3.12")

    assert argv == (*expected_prefix, str(wheel))


@pytest.mark.unit
def test_parse_args_deduplicates_repeat_lanes_in_order() -> None:
    """Duplicate lane args are idempotent while preserving first-seen order."""
    config = smoke._parse_args(
        [
            "--lane",
            "source-uv-run",
            "--lane",
            "installer-fixture",
            "--lane",
            "source-uv-run",
        ]
    )

    assert config.lanes == (smoke.Lane.SOURCE_UV_RUN, smoke.Lane.INSTALLER_FIXTURE)


@pytest.mark.unit
def test_parse_args_deduplicates_repeat_methods_in_order() -> None:
    """Duplicate method args are idempotent while preserving first-seen order."""
    config = smoke._parse_args(["--method", "uv", "--method", "pipx", "--method", "uv"])

    assert config.methods == ("uv", "pipx")


@pytest.mark.unit
def test_main_exits_nonzero_when_all_results_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op smoke run must not look successful to exit-code-only automation."""

    def skipped_harness(
        _config: smoke.SmokeConfig,
        *,
        smoke_root: Path,
    ) -> tuple[smoke.SmokeResult, ...]:
        assert smoke_root.exists()
        return (
            smoke.SmokeResult(
                lane=smoke.Lane.INSTALLER_RELEASE,
                status="skipped",
                reason=smoke.RELEASE_GATE_REASON,
            ),
        )

    monkeypatch.setattr(smoke, "run_harness", skipped_harness)

    assert smoke.main(["--lane", "installer-release"]) == 1


@pytest.mark.unit
def test_main_exits_zero_when_any_result_passes_without_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful smoke run may include lane skips as long as something passed."""

    def mixed_harness(
        _config: smoke.SmokeConfig,
        *,
        smoke_root: Path,
    ) -> tuple[smoke.SmokeResult, ...]:
        assert smoke_root.exists()
        return (
            smoke.SmokeResult(
                lane=smoke.Lane.INSTALLER_FIXTURE,
                status="passed",
                command=("bash", "install.sh", "--dry-run"),
            ),
            smoke.SmokeResult(
                lane=smoke.Lane.INSTALLER_RELEASE,
                status="skipped",
                reason=smoke.RELEASE_GATE_REASON,
            ),
        )

    monkeypatch.setattr(smoke, "run_harness", mixed_harness)

    assert smoke.main(["--lane", "installer-fixture", "--lane", "installer-release"]) == 0


@pytest.mark.unit
def test_tool_install_lane_stops_after_first_post_install_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Installed command probes stop at the first failure for clear diagnostics."""
    monkeypatch.setattr(smoke.shutil, "which", lambda name: f"/usr/bin/{name}")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    post_install_commands: list[smoke.CommandSpec] = []

    def runner(
        command: smoke.CommandSpec,
        _timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        if command.argv[:3] == ("uv", "build", "--wheel"):
            dist_dir = Path(command.argv[4])
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "agent_workspace_fabric-0.1.0-py3-none-any.whl").write_bytes(b"wheel\n")
            return subprocess.CompletedProcess(
                args=command.argv,
                returncode=0,
                stdout="built\n",
                stderr="",
            )
        if command.argv[:3] == ("uv", "tool", "install"):
            return subprocess.CompletedProcess(
                args=command.argv,
                returncode=0,
                stdout="installed\n",
                stderr="",
            )
        post_install_commands.append(command)
        return subprocess.CompletedProcess(
            args=command.argv,
            returncode=2,
            stdout="",
            stderr="awf missing\n",
        )

    results = smoke.run_tool_install_lane(
        checkout_root=checkout,
        smoke_root=tmp_path / "smoke",
        methods=("uv",),
        timeout_seconds=5,
        runner=runner,
    )

    assert [result.status for result in results] == ["passed", "failed"]
    assert [command.argv[1:] for command in post_install_commands] == [("--help",)]


@pytest.mark.unit
def test_tool_install_lane_describes_environmental_build_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Offline wheel build failures use build-specific skip diagnostics."""
    monkeypatch.setattr(smoke.shutil, "which", lambda name: f"/usr/bin/{name}")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def runner(
        command: smoke.CommandSpec,
        _timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=command.argv,
            returncode=1,
            stdout="",
            stderr="error: failed to fetch dependency\nTemporary failure in name resolution\n",
        )

    results = smoke.run_tool_install_lane(
        checkout_root=checkout,
        smoke_root=tmp_path / "smoke",
        methods=("uv",),
        timeout_seconds=5,
        runner=runner,
    )

    assert len(results) == 1
    result = results[0]
    assert result.status == "skipped"
    assert result.command[:3] == ("uv", "build", "--wheel")
    assert result.reason == "local wheel build could not resolve dependencies in this environment"


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
def test_prepare_source_lane_dirs_writes_parent_project_sentinel(tmp_path: Path) -> None:
    """Source lanes stop uv ancestor discovery before a polluted temp parent."""
    source = _write_source_checkout(tmp_path / "source")
    smoke_root = tmp_path / "smoke"

    checkout, outside = smoke._prepare_source_lane_dirs(source, smoke_root)

    sentinel = smoke_root / "pyproject.toml"
    assert checkout == smoke_root / "source-checkout"
    assert outside == smoke_root / "outside"
    assert sentinel.read_text(encoding="utf-8") == (
        "[project]\n"
        'name = "awf-first-run-smoke-root"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12"\n'
    )


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
@pytest.mark.parametrize(
    ("lane", "argv"),
    (
        (smoke.Lane.SOURCE_UV_RUN, ("uv", "run", "awf", "--help")),
        (smoke.Lane.TOOL_INSTALL, ("/tmp/awf-smoke/bin/awf", "--help")),
    ),
)
def test_source_command_result_skips_environmental_dependency_failures(
    tmp_path: Path,
    lane: smoke.Lane,
    argv: tuple[str, ...],
) -> None:
    """Offline resolver failures skip source and installed-command probes."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    command = smoke.CommandSpec(argv=argv, env={}, cwd=tmp_path / "outside")
    completed = subprocess.CompletedProcess(
        args=command.argv,
        returncode=1,
        stdout="",
        stderr="error: failed to fetch dependency\nTemporary failure in name resolution\n",
    )

    result = smoke._source_command_result(lane, command, completed, checkout)

    assert result.status == "skipped"
    assert result.reason == "smoke command could not resolve dependencies in this environment"
    assert result.command == command.argv


@pytest.mark.unit
def test_source_command_sequence_runs_setup_proof_after_environmental_skip(
    tmp_path: Path,
) -> None:
    """An early environmental skip must not bypass the source-checkout proof."""
    checkout = tmp_path / "checkout"
    outside = tmp_path / "outside"
    checkout.mkdir()
    outside.mkdir()
    help_command = smoke.CommandSpec(
        argv=("uv", "run", "--project", str(checkout), "awf", "--help"),
        env={},
        cwd=outside,
    )
    setup_command = smoke.CommandSpec(
        argv=(
            "uv",
            "run",
            "--project",
            str(checkout),
            "awf",
            "setup",
            "--dry-run",
            "--source-checkout",
            str(checkout),
            "--format",
            "json",
        ),
        env={},
        cwd=outside,
    )
    calls: list[smoke.CommandSpec] = []

    def runner(
        command: smoke.CommandSpec,
        _timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command.argv == help_command.argv:
            return subprocess.CompletedProcess(
                args=command.argv,
                returncode=1,
                stdout="",
                stderr="error: failed to fetch dependency\n",
            )
        payload = {"details": {"source_checkout": {"root": str(checkout.resolve())}}}
        return subprocess.CompletedProcess(
            args=command.argv,
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    results = smoke._run_source_command_sequence(
        smoke.Lane.SOURCE_UV_RUN,
        (help_command, setup_command),
        checkout=checkout,
        timeout_seconds=5,
        runner=runner,
    )

    assert [command.argv for command in calls] == [help_command.argv, setup_command.argv]
    assert [result.status for result in results] == ["skipped", "passed"]
    assert results[-1].source_checkout == {"root": str(checkout.resolve())}


@pytest.mark.unit
def test_source_command_result_requires_adjacent_format_json_for_setup_proof(
    tmp_path: Path,
) -> None:
    """A stray json token must not route a non-JSON setup command through proof parsing."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    command = smoke.CommandSpec(
        argv=("awf", "setup", "--dry-run", "--format", "text", "--output", "json"),
        env={},
        cwd=tmp_path / "outside",
    )
    completed = subprocess.CompletedProcess(
        args=command.argv,
        returncode=0,
        stdout="plain dry run\n",
        stderr="",
    )

    result = smoke._source_command_result(
        smoke.Lane.SOURCE_UV_RUN,
        command,
        completed,
        checkout,
    )

    assert result.status == "passed"
    assert result.source_checkout is None


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


@pytest.mark.unit
def test_run_command_reports_oserror_as_failed_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Command spawn failures are reported instead of crashing the harness."""

    def missing_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("missing executable")

    monkeypatch.setattr(smoke.subprocess, "run", missing_run)
    command = smoke.CommandSpec(argv=("/tmp/awf-smoke/bin/awf", "--help"), env={}, cwd=tmp_path)

    completed = smoke.run_command(command, timeout_seconds=5)

    assert completed.args == command.argv
    assert completed.returncode == 127
    assert completed.stdout == ""
    assert completed.stderr == "command not found or not executable: missing executable"


@pytest.mark.unit
def test_source_setup_result_exposes_full_stdout_source_checkout(tmp_path: Path) -> None:
    """Setup metadata is parsed from full stdout even when stdout_tail is truncated."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    command = smoke.CommandSpec(
        argv=(
            "awf",
            "setup",
            "--dry-run",
            "--source-checkout",
            str(checkout),
            "--format",
            "json",
        ),
        env={},
        cwd=tmp_path / "outside",
    )
    payload = {
        "padding": "x" * (smoke._TAIL_CHARS + 100),
        "details": {"source_checkout": {"root": str(checkout.resolve())}},
    }
    completed = subprocess.CompletedProcess(
        args=command.argv,
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )

    result = smoke._source_setup_result(smoke.Lane.SOURCE_UV_RUN, command, completed, checkout)

    assert result.status == "passed"
    assert result.source_checkout == {"root": str(checkout.resolve())}
    assert len(result.stdout_tail) == smoke._TAIL_CHARS
    assert not result.stdout_tail.startswith("{")


@pytest.mark.unit
def test_source_setup_result_accepts_non_source_readiness_blocker_exit_one(
    tmp_path: Path,
) -> None:
    """Return code 1 is acceptable when JSON proves the selected checkout."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    command = smoke.CommandSpec(
        argv=(
            "awf",
            "setup",
            "--dry-run",
            "--source-checkout",
            str(checkout),
            "--format",
            "json",
        ),
        env={},
        cwd=tmp_path / "outside",
    )
    payload = {
        "issues": [{"reason_code": "DOCKER_UNAVAILABLE"}],
        "details": {"source_checkout": {"root": str(checkout.resolve())}},
    }
    completed = subprocess.CompletedProcess(
        args=command.argv,
        returncode=1,
        stdout=json.dumps(payload),
        stderr="",
    )

    result = smoke._source_setup_result(smoke.Lane.SOURCE_UV_RUN, command, completed, checkout)

    assert result.status == "passed"
    assert result.source_checkout == {"root": str(checkout.resolve())}


@pytest.mark.unit
@pytest.mark.parametrize("returncode", [1, 2])
def test_source_setup_result_skips_unparseable_environmental_failure(
    tmp_path: Path,
    returncode: int,
) -> None:
    """Setup dry-run dependency fetch failures skip when AWF JSON never runs."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    command = smoke.CommandSpec(
        argv=(
            "uv",
            "run",
            "--project",
            str(checkout),
            "awf",
            "setup",
            "--dry-run",
            "--source-checkout",
            str(checkout),
            "--format",
            "json",
        ),
        env={},
        cwd=tmp_path / "outside",
    )
    completed = subprocess.CompletedProcess(
        args=command.argv,
        returncode=returncode,
        stdout="",
        stderr="error: failed to fetch dependency\nTemporary failure in name resolution\n",
    )

    result = smoke._source_setup_result(smoke.Lane.SOURCE_UV_RUN, command, completed, checkout)

    assert result.status == "skipped"
    assert result.reason == "smoke command could not resolve dependencies in this environment"
    assert result.command == command.argv


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
