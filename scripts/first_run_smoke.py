#!/usr/bin/env python3
"""First-run clean-install and source-checkout smoke harness.

The default lanes are hermetic enough for a local checkout: installer fixture
dry-run and source no-global ``uv run``. Release artifacts and real tool installs
are available through explicit lanes, with release artifacts requiring an
additional gate so local runs never publish or fetch mutable release state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_INSTALLER = REPO_ROOT / "packaging" / "install.sh"
DEFAULT_PYTHON = "3.12"
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_LANES = ("installer-fixture", "source-uv-run")
CHECKSUM_VERIFIED_MARKER = "Checksum verified"
_FIXTURE_VERSION = "0.1.0"
RELEASE_GATE_REASON = (
    "release lane requires --allow-release, --release-dist-dir, and --release-manifest"
)
_ENVIRONMENTAL_COMMAND_SKIP_REASON = (
    "smoke command could not resolve dependencies in this environment"
)
SOURCE_CHECKOUT_REASON_CODES = {
    "SOURCE_CHECKOUT_INVALID",
    "SOURCE_CHECKOUT_ASSETS_STALE",
}
SOURCE_LANE_PARENT_PYPROJECT = """[project]
name = "awf-first-run-smoke-root"
version = "0.0.0"
requires-python = ">=3.12"
"""
_TAIL_CHARS = 4000
_IGNORED_SOURCE_NAMES = frozenset(
    {
        ".coverage",
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "venv",
    }
)
_ENVIRONMENTAL_FAILURE_SIGNATURES = (
    "network connectivity is disabled",
    "offline mode",
    "failed to fetch",
    "failed to download",
    "error sending request",
    "could not connect",
    "could not resolve host",
    "failed to lookup address",
    "temporary failure in name resolution",
    "connection refused",
    "connection reset",
    "connection timed out",
    "operation timed out",
    "read timed out",
    "no such host",
    "network is unreachable",
    "proxyerror",
)

SmokeStatus = Literal["passed", "failed", "skipped"]
Runner = Callable[["CommandSpec", float], subprocess.CompletedProcess[str]]


class Lane(StrEnum):
    """Supported first-run smoke lanes."""

    INSTALLER_FIXTURE = "installer-fixture"
    INSTALLER_RELEASE = "installer-release"
    TOOL_INSTALL = "tool-install"
    SOURCE_TOOL_INSTALL = "source-tool-install"
    SOURCE_UV_RUN = "source-uv-run"


@dataclass(frozen=True)
class CommandSpec:
    """One subprocess command with its cwd and environment overrides."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path | None = None


@dataclass(frozen=True)
class SmokeResult:
    """Concise result for one smoke command or lane-level skip."""

    lane: Lane
    status: SmokeStatus
    command: tuple[str, ...] = ()
    reason: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    source_checkout: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SmokeConfig:
    """Resolved CLI options for a first-run smoke run."""

    lanes: tuple[Lane, ...]
    methods: tuple[str, ...]
    checkout_root: Path
    installer: Path
    release_dist_dir: Path | None
    release_manifest: Path | None
    allow_release: bool
    timeout_seconds: float
    python: str
    keep_temp: bool


def installer_fixture_command(
    smoke_root: Path,
    *,
    method: str,
    installer: Path = DEFAULT_INSTALLER,
) -> CommandSpec:
    """Create a local wheel manifest fixture and return an install.sh dry-run command."""
    from scripts.release_smoke import smoke_invocation

    smoke_root.mkdir(parents=True, exist_ok=True)
    dist_dir = smoke_root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    wheel = dist_dir / f"agent_workspace_fabric-{_FIXTURE_VERSION}-py3-none-any.whl"
    wheel.write_bytes(b"awf first-run smoke fixture wheel\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = {
        "artifacts": [
            {
                "kind": "wheel",
                "name": wheel.name,
                "sha256": digest,
                "url": f"file://{wheel.resolve()}",
            }
        ],
        "channel": "stable",
        "generated_at": "2026-05-29T00:00:00Z",
        "package": "agent-workspace-fabric",
        "schema_version": 1,
        "source": {
            "commit": None,
            "repository": "https://github.com/dimileeh/agent-workspace-fabric",
            "tag": f"v{_FIXTURE_VERSION}",
        },
        "version": _FIXTURE_VERSION,
    }
    manifest_path = smoke_root / "fixture-manifest.json"
    _write_json(manifest, manifest_path)
    argv, env = smoke_invocation(installer, manifest_path, channel="stable", method=method)
    return CommandSpec(argv=tuple(argv), env=env, cwd=REPO_ROOT)


def run_installer_fixture_lane(
    *,
    smoke_root: Path,
    methods: Sequence[str],
    installer: Path = DEFAULT_INSTALLER,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runner: Runner | None = None,
) -> tuple[SmokeResult, ...]:
    """Run the local fixture installer lane for each requested method."""
    run = runner or run_command
    results: list[SmokeResult] = []
    for method in methods:
        command = installer_fixture_command(
            smoke_root / method,
            method=method,
            installer=installer,
        )
        completed = run(command, timeout_seconds)
        results.append(
            _installer_result(
                Lane.INSTALLER_FIXTURE,
                command,
                completed,
                require_checksum_marker=True,
            )
        )
    return tuple(results)


def run_installer_release_lane(
    *,
    smoke_root: Path,
    methods: Sequence[str],
    allow_release: bool,
    release_dist_dir: Path | None,
    release_manifest: Path | None,
    installer: Path = DEFAULT_INSTALLER,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runner: Runner | None = None,
) -> tuple[SmokeResult, ...]:
    """Run release-artifact installer dry-runs, gated behind explicit local artifacts."""
    from scripts.release_smoke import build_smoke_manifest, smoke_invocation

    if not allow_release or release_dist_dir is None or release_manifest is None:
        return (
            SmokeResult(
                lane=Lane.INSTALLER_RELEASE,
                status="skipped",
                reason=RELEASE_GATE_REASON,
            ),
        )

    run = runner or run_command
    smoke_root.mkdir(parents=True, exist_ok=True)
    try:
        manifest = _load_json_object(release_manifest)
        smoke_manifest = build_smoke_manifest(manifest, release_dist_dir)
        channel = _manifest_channel(smoke_manifest)
        smoke_manifest_path = smoke_root / "release-manifest.smoke.json"
        _write_json(smoke_manifest, smoke_manifest_path)
    except (OSError, ValueError, RuntimeError) as exc:
        return (
            SmokeResult(
                lane=Lane.INSTALLER_RELEASE,
                status="failed",
                reason=str(exc),
            ),
        )

    results: list[SmokeResult] = []
    for method in methods:
        argv, env = smoke_invocation(installer, smoke_manifest_path, channel=channel, method=method)
        command = CommandSpec(argv=tuple(argv), env=env, cwd=REPO_ROOT)
        completed = run(command, timeout_seconds)
        results.append(
            _installer_result(
                Lane.INSTALLER_RELEASE,
                command,
                completed,
                require_checksum_marker=True,
            )
        )
    return tuple(results)


def isolated_tool_environment(
    smoke_root: Path,
    *,
    method: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an isolated uv/pipx environment rooted under ``smoke_root``."""
    env = _clean_base_env(base_env)
    home = smoke_root / "home"
    bin_dir = smoke_root / "bin"
    uv_cache = smoke_root / "uv-cache"
    for path in (
        home,
        bin_dir,
        uv_cache,
        smoke_root / "xdg-cache",
        smoke_root / "xdg-config",
        smoke_root / "xdg-data",
    ):
        path.mkdir(parents=True, exist_ok=True)

    env.update(
        {
            "HOME": str(home),
            "PATH": _prepend_path(bin_dir, env.get("PATH", "")),
            "UV_CACHE_DIR": str(uv_cache),
            "XDG_CACHE_HOME": str(smoke_root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(smoke_root / "xdg-config"),
            "XDG_DATA_HOME": str(smoke_root / "xdg-data"),
        }
    )
    if method == "uv":
        uv_tool_dir = smoke_root / "uv-tool-dir"
        uv_tool_dir.mkdir(parents=True, exist_ok=True)
        env.update({"UV_TOOL_DIR": str(uv_tool_dir), "UV_TOOL_BIN_DIR": str(bin_dir)})
    elif method == "pipx":
        pipx_home = smoke_root / "pipx-home"
        pipx_home.mkdir(parents=True, exist_ok=True)
        env.update({"PIPX_HOME": str(pipx_home), "PIPX_BIN_DIR": str(bin_dir)})
    else:
        raise ValueError(f"unsupported tool install method: {method}")
    return env


def source_environment(
    smoke_root: Path,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a temp-home environment for source no-global uv runs."""
    env = _clean_base_env(base_env)
    home = smoke_root / "home"
    uv_cache = smoke_root / "uv-cache"
    for path in (
        home,
        uv_cache,
        smoke_root / "xdg-cache",
        smoke_root / "xdg-config",
        smoke_root / "xdg-data",
    ):
        path.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "UV_CACHE_DIR": str(uv_cache),
            "XDG_CACHE_HOME": str(smoke_root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(smoke_root / "xdg-config"),
            "XDG_DATA_HOME": str(smoke_root / "xdg-data"),
        }
    )
    return env


def copy_source_checkout(source: Path, destination: Path) -> Path:
    """Copy a source checkout while excluding heavyweight developer state."""
    source = source.resolve()
    if destination.exists():
        raise FileExistsError(f"source checkout copy destination already exists: {destination}")
    shutil.copytree(source, destination, ignore=_ignore_source_names, symlinks=True)
    return destination


def source_uv_run_commands(
    *,
    checkout: Path,
    outside_cwd: Path,
    smoke_root: Path,
    python: str = DEFAULT_PYTHON,
    base_env: Mapping[str, str] | None = None,
) -> tuple[CommandSpec, ...]:
    """Return no-global uv-run commands that execute from outside the checkout."""
    env = source_environment(smoke_root, base_env=base_env)
    common = (
        "uv",
        "run",
        "--project",
        str(checkout),
        "--python",
        python,
        "awf",
    )
    source_checkout = str(checkout.resolve())
    return (
        CommandSpec(argv=(*common, "--help"), env=env, cwd=outside_cwd),
        CommandSpec(argv=(*common, "setup", "--help"), env=env, cwd=outside_cwd),
        CommandSpec(argv=(*common, "start", "--help"), env=env, cwd=outside_cwd),
        CommandSpec(
            argv=(
                *common,
                "setup",
                "--dry-run",
                "--source-checkout",
                source_checkout,
                "--format",
                "json",
            ),
            env=env,
            cwd=outside_cwd,
        ),
    )


def run_source_uv_run_lane(
    *,
    checkout_root: Path,
    smoke_root: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    python: str = DEFAULT_PYTHON,
    runner: Runner | None = None,
) -> tuple[SmokeResult, ...]:
    """Run the source checkout no-global uv-run lane from outside the checkout."""
    if shutil.which("uv") is None:
        return (_skip(Lane.SOURCE_UV_RUN, "uv is not available"),)

    run = runner or run_command
    try:
        checkout, outside = _prepare_source_lane_dirs(checkout_root, smoke_root)
    except (OSError, shutil.Error) as exc:
        return (_source_checkout_prepare_failure(Lane.SOURCE_UV_RUN, exc),)
    commands = source_uv_run_commands(
        checkout=checkout,
        outside_cwd=outside,
        smoke_root=smoke_root / "source-uv-run-env",
        python=python,
    )
    return _run_source_command_sequence(
        Lane.SOURCE_UV_RUN,
        commands,
        checkout=checkout,
        timeout_seconds=timeout_seconds,
        runner=run,
    )


def run_source_tool_install_lane(
    *,
    checkout_root: Path,
    smoke_root: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    python: str = DEFAULT_PYTHON,
    runner: Runner | None = None,
) -> tuple[SmokeResult, ...]:
    """Install the copied checkout as a uv tool, then run first-run commands outside it."""
    if shutil.which("uv") is None:
        return (_skip(Lane.SOURCE_TOOL_INSTALL, "uv is not available"),)

    run = runner or run_command
    try:
        checkout, outside = _prepare_source_lane_dirs(checkout_root, smoke_root)
    except (OSError, shutil.Error) as exc:
        return (_source_checkout_prepare_failure(Lane.SOURCE_TOOL_INSTALL, exc),)
    env = isolated_tool_environment(smoke_root, method="uv")
    install = CommandSpec(
        argv=("uv", "tool", "install", ".", "--force", "--python", python),
        env=env,
        cwd=checkout,
    )
    install_completed = run(install, timeout_seconds)
    install_result = _tool_install_result(Lane.SOURCE_TOOL_INSTALL, install, install_completed)
    if install_result.status != "passed":
        return (install_result,)

    awf_bin = Path(env["UV_TOOL_BIN_DIR"]) / "awf"
    commands = _installed_awf_commands(
        awf_bin=awf_bin,
        checkout=checkout,
        outside_cwd=outside,
        env=env,
    )
    command_results = _run_source_command_sequence(
        Lane.SOURCE_TOOL_INSTALL,
        commands,
        checkout=checkout,
        timeout_seconds=timeout_seconds,
        runner=run,
    )
    return (install_result, *command_results)


def run_tool_install_lane(
    *,
    checkout_root: Path,
    smoke_root: Path,
    methods: Sequence[str],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    python: str = DEFAULT_PYTHON,
    runner: Runner | None = None,
) -> tuple[SmokeResult, ...]:
    """Build a local wheel and install it through uv/pipx in isolated tool dirs."""
    if shutil.which("uv") is None:
        return (_skip(Lane.TOOL_INSTALL, "uv is not available to build the local wheel"),)

    run = runner or run_command
    dist_dir = smoke_root / "tool-dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    build = CommandSpec(
        argv=("uv", "build", "--wheel", "--out-dir", str(dist_dir), str(checkout_root)),
        env=source_environment(smoke_root / "build-env"),
        cwd=checkout_root,
    )
    build_completed = run(build, timeout_seconds)
    if build_completed.returncode != 0:
        combined = _combined_output(build_completed)
        if _is_environmental_failure(combined):
            return (
                SmokeResult(
                    lane=Lane.TOOL_INSTALL,
                    status="skipped",
                    command=build.argv,
                    reason="local wheel build could not resolve dependencies in this environment",
                    stdout_tail=_tail(build_completed.stdout),
                    stderr_tail=_tail(build_completed.stderr),
                ),
            )
        return (_tool_install_result(Lane.TOOL_INSTALL, build, build_completed),)

    wheels = sorted(dist_dir.glob("*.whl"))
    if not wheels:
        return (
            SmokeResult(
                lane=Lane.TOOL_INSTALL,
                status="failed",
                command=build.argv,
                reason=f"uv build produced no wheel in {dist_dir}",
                stdout_tail=_tail(build_completed.stdout),
                stderr_tail=_tail(build_completed.stderr),
            ),
        )

    results: list[SmokeResult] = []
    for method in methods:
        if method == "pipx" and shutil.which("pipx") is None:
            results.append(_skip(Lane.TOOL_INSTALL, "pipx is not available"))
            continue
        env = isolated_tool_environment(smoke_root / f"tool-{method}", method=method)
        install_argv = _tool_install_argv(method, wheels[0], python=python)
        install = CommandSpec(argv=install_argv, env=env, cwd=smoke_root)
        install_completed = run(install, timeout_seconds)
        install_result = _tool_install_result(Lane.TOOL_INSTALL, install, install_completed)
        results.append(install_result)
        if install_result.status != "passed":
            continue
        # uv and pipx expose their isolated script directory through method-specific env keys.
        awf_bin = Path(env["UV_TOOL_BIN_DIR" if method == "uv" else "PIPX_BIN_DIR"]) / "awf"
        outside = smoke_root / f"outside-{method}"
        outside.mkdir(parents=True, exist_ok=True)
        commands = _installed_awf_commands(
            awf_bin=awf_bin,
            checkout=checkout_root,
            outside_cwd=outside,
            env=env,
        )
        results.extend(
            _run_source_command_sequence(
                Lane.TOOL_INSTALL,
                commands,
                checkout=checkout_root,
                timeout_seconds=timeout_seconds,
                runner=run,
            )
        )
    return tuple(results)


def run_command(command: CommandSpec, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    """Run a smoke command with PYTHONPATH scrubbed by default."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update(command.env)
    try:
        return subprocess.run(
            command.argv,
            check=False,
            capture_output=True,
            text=True,
            cwd=str(command.cwd) if command.cwd is not None else None,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_output_text(exc.stdout)
        stderr = _timeout_output_text(exc.stderr)
        message = f"command timed out after {timeout_seconds:g} seconds"
        return subprocess.CompletedProcess(
            args=command.argv,
            returncode=124,
            stdout=stdout,
            stderr=f"{message}\n{stderr}" if stderr else message,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=command.argv,
            returncode=127,
            stdout="",
            stderr=f"command not found or not executable: {exc}",
        )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    config = _parse_args(argv)
    if config.keep_temp:
        smoke_root = Path(tempfile.mkdtemp(prefix="awf-first-run-smoke-"))
        results = run_harness(config, smoke_root=smoke_root)
        print(f"temp root: {smoke_root}")
        _print_results(results)
    else:
        with tempfile.TemporaryDirectory(prefix="awf-first-run-smoke-") as temp_dir:
            results = run_harness(config, smoke_root=Path(temp_dir))
            _print_results(results)
    has_failure = any(result.status == "failed" for result in results)
    has_pass = any(result.status == "passed" for result in results)
    return 0 if has_pass and not has_failure else 1


def run_harness(config: SmokeConfig, *, smoke_root: Path) -> tuple[SmokeResult, ...]:
    """Run configured lanes under one smoke root."""
    results: list[SmokeResult] = []
    for lane in config.lanes:
        lane_root = smoke_root / lane.value
        if lane is Lane.INSTALLER_FIXTURE:
            results.extend(
                run_installer_fixture_lane(
                    smoke_root=lane_root,
                    methods=config.methods,
                    installer=config.installer,
                    timeout_seconds=config.timeout_seconds,
                )
            )
        elif lane is Lane.INSTALLER_RELEASE:
            results.extend(
                run_installer_release_lane(
                    smoke_root=lane_root,
                    methods=config.methods,
                    allow_release=config.allow_release,
                    release_dist_dir=config.release_dist_dir,
                    release_manifest=config.release_manifest,
                    installer=config.installer,
                    timeout_seconds=config.timeout_seconds,
                )
            )
        elif lane is Lane.TOOL_INSTALL:
            results.extend(
                run_tool_install_lane(
                    checkout_root=config.checkout_root,
                    smoke_root=lane_root,
                    methods=config.methods,
                    timeout_seconds=config.timeout_seconds,
                    python=config.python,
                )
            )
        elif lane is Lane.SOURCE_TOOL_INSTALL:
            results.extend(
                run_source_tool_install_lane(
                    checkout_root=config.checkout_root,
                    smoke_root=lane_root,
                    timeout_seconds=config.timeout_seconds,
                    python=config.python,
                )
            )
        elif lane is Lane.SOURCE_UV_RUN:
            results.extend(
                run_source_uv_run_lane(
                    checkout_root=config.checkout_root,
                    smoke_root=lane_root,
                    timeout_seconds=config.timeout_seconds,
                    python=config.python,
                )
            )
    return tuple(results)


def _parse_args(argv: Sequence[str] | None) -> SmokeConfig:
    """Parse CLI flags into the smoke harness configuration."""
    parser = argparse.ArgumentParser(description="Run AWF first-run smoke lanes.")
    parser.add_argument(
        "--lane",
        action="append",
        choices=[lane.value for lane in Lane],
        help=f"lane to run (repeatable; default: {', '.join(DEFAULT_LANES)}).",
    )
    parser.add_argument(
        "--method",
        action="append",
        choices=["uv", "pipx"],
        help="install method for installer/tool lanes (repeatable; default: uv).",
    )
    parser.add_argument("--checkout-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--installer", type=Path, default=DEFAULT_INSTALLER)
    parser.add_argument("--release-dist-dir", type=Path)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--allow-release", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--python", default=DEFAULT_PYTHON)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args(argv)
    lanes = tuple(dict.fromkeys(Lane(raw) for raw in (args.lane or DEFAULT_LANES)))
    return SmokeConfig(
        lanes=lanes,
        methods=tuple(dict.fromkeys(args.method or ("uv",))),
        checkout_root=args.checkout_root.resolve(),
        installer=args.installer.resolve(),
        release_dist_dir=args.release_dist_dir.resolve() if args.release_dist_dir else None,
        release_manifest=args.release_manifest.resolve() if args.release_manifest else None,
        allow_release=args.allow_release,
        timeout_seconds=args.timeout_seconds,
        python=args.python,
        keep_temp=args.keep_temp,
    )


def _prepare_source_lane_dirs(checkout_root: Path, smoke_root: Path) -> tuple[Path, Path]:
    """Copy a source checkout and create the outside working directory."""
    smoke_root.mkdir(parents=True, exist_ok=True)
    # uv walks project ancestors while building a path dependency. Keep a valid
    # boundary here so unrelated /tmp/pyproject.toml files cannot poison lanes.
    (smoke_root / "pyproject.toml").write_text(SOURCE_LANE_PARENT_PYPROJECT, encoding="utf-8")
    checkout = copy_source_checkout(checkout_root, smoke_root / "source-checkout")
    outside = smoke_root / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    return checkout, outside


def _installed_awf_commands(
    *,
    awf_bin: Path,
    checkout: Path,
    outside_cwd: Path,
    env: Mapping[str, str],
) -> tuple[CommandSpec, ...]:
    """Build post-install AWF commands that prove source checkout selection."""
    source_checkout = str(checkout.resolve())
    return (
        CommandSpec(argv=(str(awf_bin), "--help"), env=env, cwd=outside_cwd),
        CommandSpec(argv=(str(awf_bin), "setup", "--help"), env=env, cwd=outside_cwd),
        CommandSpec(argv=(str(awf_bin), "start", "--help"), env=env, cwd=outside_cwd),
        CommandSpec(
            argv=(
                str(awf_bin),
                "setup",
                "--dry-run",
                "--source-checkout",
                source_checkout,
                "--format",
                "json",
            ),
            env=env,
            cwd=outside_cwd,
        ),
    )


def _run_source_command_sequence(
    lane: Lane,
    commands: Sequence[CommandSpec],
    *,
    checkout: Path,
    timeout_seconds: float,
    runner: Runner,
) -> tuple[SmokeResult, ...]:
    """Run source lane commands and stop only after hard command failures."""
    results: list[SmokeResult] = []
    for command in commands:
        completed = runner(command, timeout_seconds)
        result = _source_command_result(lane, command, completed, checkout)
        results.append(result)
        if result.status == "failed":
            break
    return tuple(results)


def _source_command_result(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
    checkout: Path,
) -> SmokeResult:
    """Classify a source lane command result, including setup proof commands."""
    if _is_setup_dry_run_json(command):
        return _source_setup_result(lane, command, completed, checkout)
    return _basic_result(lane, command, completed)


def _source_setup_result(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
    checkout: Path,
) -> SmokeResult:
    """Validate setup dry-run JSON proves the selected source checkout."""
    stdout_tail = _tail(completed.stdout)
    stderr_tail = _tail(completed.stderr)
    combined = _combined_output(completed)
    # Host-readiness blockers can make setup dry-run exit 1 even when the
    # selected source checkout is correct, so parse JSON before deciding whether
    # this source-checkout smoke proof failed.
    if completed.returncode not in {0, 1}:
        if _is_environmental_failure(combined):
            return _environmental_skip_result(lane, command, completed)
        return SmokeResult(
            lane=lane,
            status="failed",
            command=command.argv,
            reason=f"setup dry-run exited {completed.returncode}",
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        if completed.returncode != 0 and _is_environmental_failure(combined):
            return _environmental_skip_result(lane, command, completed)
        return SmokeResult(
            lane=lane,
            status="failed",
            command=command.argv,
            reason=f"setup dry-run did not emit parseable JSON: {exc}",
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    reason_codes = _payload_reason_codes(payload)
    source_failures = sorted(reason_codes & SOURCE_CHECKOUT_REASON_CODES)
    if source_failures:
        return SmokeResult(
            lane=lane,
            status="failed",
            command=command.argv,
            reason="source checkout failed validation: " + ", ".join(source_failures),
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    source_checkout = _payload_source_checkout(payload)
    if source_checkout is None:
        return SmokeResult(
            lane=lane,
            status="failed",
            command=command.argv,
            reason="setup dry-run did not emit details.source_checkout as an object",
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    selected = _source_checkout_root(source_checkout)
    if selected is None:
        return SmokeResult(
            lane=lane,
            status="failed",
            command=command.argv,
            reason="setup dry-run did not emit source_checkout.root as a string",
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    if selected != str(checkout.resolve()):
        return SmokeResult(
            lane=lane,
            status="failed",
            command=command.argv,
            reason=f"setup dry-run did not identify selected source checkout {checkout}",
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    return SmokeResult(
        lane=lane,
        status="passed",
        command=command.argv,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        source_checkout=source_checkout,
    )


def _installer_result(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
    *,
    require_checksum_marker: bool,
) -> SmokeResult:
    """Convert installer command output into a structured smoke result."""
    if completed.returncode != 0:
        return _failed_completed(lane, command, completed, f"command exited {completed.returncode}")
    if require_checksum_marker and CHECKSUM_VERIFIED_MARKER not in completed.stdout:
        return _failed_completed(lane, command, completed, "installer did not verify checksum")
    return SmokeResult(
        lane=lane,
        status="passed",
        command=command.argv,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
    )


def _tool_install_result(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
) -> SmokeResult:
    """Convert tool-install command output into a structured smoke result."""
    combined = _combined_output(completed)
    if completed.returncode == 0:
        return SmokeResult(
            lane=lane,
            status="passed",
            command=command.argv,
            stdout_tail=_tail(completed.stdout),
            stderr_tail=_tail(completed.stderr),
        )
    if _is_environmental_failure(combined):
        return SmokeResult(
            lane=lane,
            status="skipped",
            command=command.argv,
            reason="local tool install could not resolve dependencies in this environment",
            stdout_tail=_tail(completed.stdout),
            stderr_tail=_tail(completed.stderr),
        )
    return _failed_completed(lane, command, completed, f"command exited {completed.returncode}")


def _basic_result(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
) -> SmokeResult:
    """Convert a generic command completion into a smoke result."""
    if completed.returncode == 0:
        return SmokeResult(
            lane=lane,
            status="passed",
            command=command.argv,
            stdout_tail=_tail(completed.stdout),
            stderr_tail=_tail(completed.stderr),
        )
    if _is_environmental_failure(_combined_output(completed)):
        return _environmental_skip_result(lane, command, completed)
    return _failed_completed(lane, command, completed, f"command exited {completed.returncode}")


def _environmental_skip_result(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
) -> SmokeResult:
    """Return a skipped result for dependency/environmental command failures."""
    return SmokeResult(
        lane=lane,
        status="skipped",
        command=command.argv,
        reason=_ENVIRONMENTAL_COMMAND_SKIP_REASON,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
    )


def _failed_completed(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
    reason: str,
) -> SmokeResult:
    """Return a failed result with captured stdout and stderr tails."""
    return SmokeResult(
        lane=lane,
        status="failed",
        command=command.argv,
        reason=reason,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
    )


def _source_checkout_prepare_failure(lane: Lane, exc: Exception) -> SmokeResult:
    """Return the structured failure for source checkout preparation errors."""
    detail = str(exc)
    return SmokeResult(
        lane=lane,
        status="failed",
        reason=f"source checkout preparation failed: {detail}",
        stderr_tail=_tail(detail),
    )


def _skip(lane: Lane, reason: str) -> SmokeResult:
    """Return a skipped lane result with the supplied reason."""
    return SmokeResult(lane=lane, status="skipped", reason=reason)


def _tool_install_argv(method: str, wheel: Path, *, python: str) -> tuple[str, ...]:
    """Build the install argv for the requested tool installer."""
    if method == "uv":
        return ("uv", "tool", "install", "--force", "--python", python, str(wheel))
    if method == "pipx":
        return ("pipx", "install", "--force", "--python", python, str(wheel))
    raise ValueError(f"unsupported tool install method: {method}")


def _timeout_output_text(output: str | bytes | None) -> str:
    """Normalize timeout-captured subprocess output to text."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


def _is_setup_dry_run_json(command: CommandSpec) -> bool:
    """Return whether a command is the setup dry-run JSON proof command."""
    try:
        fmt_idx = command.argv.index("--format")
    except ValueError:
        return False
    return (
        "setup" in command.argv
        and "--dry-run" in command.argv
        and fmt_idx + 1 < len(command.argv)
        and command.argv[fmt_idx + 1] == "json"
    )


def _payload_reason_codes(payload: object) -> set[str]:
    """Extract top-level and issue reason codes from a setup JSON payload."""
    if not isinstance(payload, dict):
        return set()
    codes: set[str] = set()
    reason_code = payload.get("reason_code")
    if isinstance(reason_code, str):
        codes.add(reason_code)
    issues = payload.get("issues")
    if isinstance(issues, list):
        for issue in issues:
            if isinstance(issue, dict) and isinstance(issue.get("reason_code"), str):
                codes.add(issue["reason_code"])
    return codes


def _payload_source_checkout(payload: object) -> dict[str, object] | None:
    """Extract string-keyed source checkout metadata from setup JSON."""
    if not isinstance(payload, dict):
        return None
    details = payload.get("details")
    if not isinstance(details, dict):
        return None
    source_checkout = details.get("source_checkout")
    if not isinstance(source_checkout, dict):
        return None
    metadata: dict[str, object] = {}
    for key, value in source_checkout.items():
        if isinstance(key, str):
            metadata[key] = value
    return metadata


def _source_checkout_root(source_checkout: Mapping[str, object] | None) -> str | None:
    """Return the source checkout root metadata when it is a string."""
    if source_checkout is None:
        return None
    root = source_checkout.get("root")
    return root if isinstance(root, str) else None


def _is_environmental_failure(output: str) -> bool:
    """Return whether output matches known local dependency failure signatures."""
    lowered = output.lower()
    return any(signature in lowered for signature in _ENVIRONMENTAL_FAILURE_SIGNATURES)


def _combined_output(completed: subprocess.CompletedProcess[str]) -> str:
    """Join stderr and stdout for failure classification."""
    return "\n".join(part for part in (completed.stderr, completed.stdout) if part)


def _ignore_source_names(_directory: str, names: list[str]) -> set[str]:
    """Return source checkout entries that should not be copied."""
    return {name for name in names if name in _IGNORED_SOURCE_NAMES or name.endswith(".egg-info")}


def _clean_base_env(base_env: Mapping[str, str] | None) -> dict[str, str]:
    """Return a subprocess environment without Python path injection."""
    env = dict(os.environ if base_env is None else base_env)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _prepend_path(path: Path, current_path: str) -> str:
    """Prepend a path entry to an existing PATH string."""
    if current_path:
        return f"{path}{os.pathsep}{current_path}"
    return str(path)


def _manifest_channel(manifest: Mapping[str, object]) -> str:
    """Read and validate the release manifest channel."""
    channel = manifest.get("channel", "stable")
    if not isinstance(channel, str) or not channel:
        raise ValueError(f"manifest channel must be a non-empty string: {channel!r}")
    return channel


def _load_json_object(path: Path) -> dict[str, object]:
    """Load a JSON file and require an object payload."""
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"expected JSON object: {path}")
    return loaded


def _write_json(payload: Mapping[str, object], path: Path) -> None:
    """Write a formatted JSON object to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _tail(text: str) -> str:
    """Return the bounded diagnostic tail for command output."""
    return text[-_TAIL_CHARS:]


def _print_results(results: Sequence[SmokeResult]) -> None:
    """Print smoke results with output tails for failures."""
    for result in results:
        prefix = result.status.upper()
        command = " ".join(result.command) if result.command else result.reason
        print(f"{prefix} {result.lane.value}: {command}")
        if result.reason and result.command:
            print(f"  reason: {result.reason}")
        if result.status == "failed":
            if result.stdout_tail:
                print("  stdout tail:")
                print(_indent(result.stdout_tail.rstrip()))
            if result.stderr_tail:
                print("  stderr tail:")
                print(_indent(result.stderr_tail.rstrip()))


def _indent(text: str) -> str:
    """Indent multiline output for CLI diagnostics."""
    return "\n".join(f"    {line}" for line in text.splitlines())


if __name__ == "__main__":
    sys.exit(main())
