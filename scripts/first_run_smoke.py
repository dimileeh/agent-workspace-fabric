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
RELEASE_GATE_REASON = (
    "release lane requires --allow-release, --release-dist-dir, and --release-manifest"
)
SOURCE_CHECKOUT_REASON_CODES = {
    "SOURCE_CHECKOUT_INVALID",
    "SOURCE_CHECKOUT_ASSETS_STALE",
}
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
    wheel = dist_dir / "agent_workspace_fabric-0.1.0-py3-none-any.whl"
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
            "repository": "https://github.com/dimileeh/aira-agent-workspace-fabric",
            "tag": "v0.1.0",
        },
        "version": "0.1.0",
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
        "--extra",
        "dev",
        "awf",
    )
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
                str(checkout),
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
    checkout, outside = _prepare_source_lane_dirs(checkout_root, smoke_root)
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
    checkout, outside = _prepare_source_lane_dirs(checkout_root, smoke_root)
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    config = _parse_args(argv)
    if config.keep_temp:
        smoke_root = Path(tempfile.mkdtemp(prefix="awf-first-run-smoke-"))
        results = run_harness(config, smoke_root=smoke_root)
        print(f"temp root: {smoke_root}")
    else:
        with tempfile.TemporaryDirectory(prefix="awf-first-run-smoke-") as temp_dir:
            results = run_harness(config, smoke_root=Path(temp_dir))
    _print_results(results)
    return 1 if any(result.status == "failed" for result in results) else 0


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
    return SmokeConfig(
        lanes=tuple(Lane(raw) for raw in (args.lane or DEFAULT_LANES)),
        methods=tuple(args.method or ("uv",)),
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
    smoke_root.mkdir(parents=True, exist_ok=True)
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
                str(checkout),
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
    results: list[SmokeResult] = []
    for command in commands:
        completed = runner(command, timeout_seconds)
        result = _source_command_result(lane, command, completed, checkout)
        results.append(result)
        if result.status != "passed":
            break
    return tuple(results)


def _source_command_result(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
    checkout: Path,
) -> SmokeResult:
    if _is_setup_dry_run_json(command):
        return _source_setup_result(lane, command, completed, checkout)
    return _basic_result(lane, command, completed)


def _source_setup_result(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
    checkout: Path,
) -> SmokeResult:
    stdout_tail = _tail(completed.stdout)
    stderr_tail = _tail(completed.stderr)
    # Host-readiness blockers can make setup dry-run exit 1 even when the
    # selected source checkout is correct, so parse JSON before deciding whether
    # this source-checkout smoke proof failed.
    if completed.returncode not in {0, 1}:
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
    selected = _source_checkout_root(source_checkout)
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
    combined = "\n".join(part for part in (completed.stderr, completed.stdout) if part)
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
    if completed.returncode == 0:
        return SmokeResult(
            lane=lane,
            status="passed",
            command=command.argv,
            stdout_tail=_tail(completed.stdout),
            stderr_tail=_tail(completed.stderr),
        )
    return _failed_completed(lane, command, completed, f"command exited {completed.returncode}")


def _failed_completed(
    lane: Lane,
    command: CommandSpec,
    completed: subprocess.CompletedProcess[str],
    reason: str,
) -> SmokeResult:
    return SmokeResult(
        lane=lane,
        status="failed",
        command=command.argv,
        reason=reason,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
    )


def _skip(lane: Lane, reason: str) -> SmokeResult:
    return SmokeResult(lane=lane, status="skipped", reason=reason)


def _tool_install_argv(method: str, wheel: Path, *, python: str) -> tuple[str, ...]:
    if method == "uv":
        return ("uv", "tool", "install", "--force", "--python", python, str(wheel))
    if method == "pipx":
        return ("pipx", "install", "--force", "--python", python, str(wheel))
    raise ValueError(f"unsupported tool install method: {method}")


def _timeout_output_text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


def _is_setup_dry_run_json(command: CommandSpec) -> bool:
    return (
        "setup" in command.argv
        and "--dry-run" in command.argv
        and "--format" in command.argv
        and "json" in command.argv
    )


def _payload_reason_codes(payload: object) -> set[str]:
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
    if source_checkout is None:
        return None
    root = source_checkout.get("root")
    return root if isinstance(root, str) else None


def _is_environmental_failure(output: str) -> bool:
    lowered = output.lower()
    return any(signature in lowered for signature in _ENVIRONMENTAL_FAILURE_SIGNATURES)


def _ignore_source_names(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _IGNORED_SOURCE_NAMES or name.endswith(".egg-info")}


def _clean_base_env(base_env: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _prepend_path(path: Path, current_path: str) -> str:
    if current_path:
        return f"{path}{os.pathsep}{current_path}"
    return str(path)


def _manifest_channel(manifest: Mapping[str, object]) -> str:
    channel = manifest.get("channel", "stable")
    if not isinstance(channel, str) or not channel:
        raise ValueError(f"manifest channel must be a non-empty string: {channel!r}")
    return channel


def _load_json_object(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"expected JSON object: {path}")
    return loaded


def _write_json(payload: Mapping[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _tail(text: str) -> str:
    return text[-_TAIL_CHARS:]


def _print_results(results: Sequence[SmokeResult]) -> None:
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
    return "\n".join(f"    {line}" for line in text.splitlines())


if __name__ == "__main__":
    sys.exit(main())
