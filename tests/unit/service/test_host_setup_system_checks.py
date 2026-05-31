"""Fixture-driven tests for host setup system checks and readiness payload."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from awf.host_setup import system_checks
from awf.host_setup.config import HostSetupConfig
from awf.host_setup.rendering import (
    SETUP_PROVIDER_UNKNOWN,
    SETUP_READINESS_FAILED,
    render_first_run_json,
    render_first_run_pretty,
)
from awf.host_setup.source_assets import SOURCE_CHECKOUT_INVALID, SourceCheckoutError
from awf.host_setup.system_checks import (
    INTERACTIVE_INPUT_REQUIRED,
    KNOWN_SETUP_PROVIDERS,
    CommandResult,
    SetupCheckError,
    SetupCheckLevel,
    SetupCheckResult,
    build_setup_readiness_payload,
    check_compose,
    check_disk,
    check_docker,
    check_gh,
    check_git,
    check_local_capacity,
    check_ports,
    check_python_runtime,
    check_shell_path,
    normalize_provider,
    normalize_providers,
    require_interactive,
    run_system_checks,
)


def _command_runner(
    mapping: dict[tuple[str, ...], CommandResult | None],
) -> system_checks.CommandRunner:
    """Return a fake command runner mapping arg tuples to canned results."""

    def run(args: Sequence[str]) -> CommandResult | None:
        return mapping.get(tuple(args))

    return run


# --- Docker ---------------------------------------------------------------


@pytest.mark.unit
def test_check_docker_ok_when_cli_and_daemon_reachable() -> None:
    """Verify a present docker CLI with a reachable daemon reports OK."""
    result = check_docker(
        which=lambda _cmd: "/usr/bin/docker",
        run=_command_runner(
            {("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(0)}
        ),
    )
    assert result.level is SetupCheckLevel.OK
    assert result.name == "docker"


@pytest.mark.unit
def test_check_docker_blocked_when_binary_missing() -> None:
    """Verify a missing docker CLI blocks with an install fix."""
    result = check_docker(which=lambda _cmd: None, run=_command_runner({}))
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.fix is not None
    assert "install" in result.fix.lower()
    assert result.data["available"] is False


@pytest.mark.unit
def test_check_docker_blocked_when_daemon_unreachable() -> None:
    """Verify a present CLI with an unreachable daemon blocks with a start fix."""
    result = check_docker(
        which=lambda _cmd: "/usr/bin/docker",
        run=_command_runner(
            {("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(1)}
        ),
    )
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.fix is not None
    assert "start" in result.fix.lower()
    assert result.data["daemon"] is False


@pytest.mark.unit
def test_check_docker_blocked_when_probe_cannot_run() -> None:
    """Verify a probe that cannot launch (None) is treated as daemon-unreachable."""
    result = check_docker(which=lambda _cmd: "/usr/bin/docker", run=_command_runner({}))
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["daemon"] is False


# --- Compose --------------------------------------------------------------


@pytest.mark.unit
def test_check_compose_ok_via_plugin() -> None:
    """Verify the docker compose plugin satisfies the compose check."""
    result = check_compose(
        run=_command_runner({("docker", "compose", "version"): CommandResult(0)})
    )
    assert result.level is SetupCheckLevel.OK
    assert result.data["variant"] == "docker compose"


@pytest.mark.unit
def test_check_compose_ok_via_legacy_binary() -> None:
    """Verify the legacy docker-compose binary satisfies the compose check."""
    result = check_compose(
        run=_command_runner(
            {
                ("docker", "compose", "version"): CommandResult(1),
                ("docker-compose", "version"): CommandResult(0),
            }
        )
    )
    assert result.level is SetupCheckLevel.OK
    assert result.data["variant"] == "docker-compose"


@pytest.mark.unit
def test_check_compose_blocked_when_neither_available() -> None:
    """Verify a missing compose plugin and binary block."""
    result = check_compose(run=_command_runner({}))
    assert result.level is SetupCheckLevel.BLOCKED


# --- Git / gh -------------------------------------------------------------


@pytest.mark.unit
def test_check_git_ok_and_blocked() -> None:
    """Verify git presence drives OK and absence drives BLOCKED."""
    ok = check_git(
        which=lambda _cmd: "/usr/bin/git",
        run=_command_runner({("git", "--version"): CommandResult(0)}),
    )
    blocked = check_git(which=lambda _cmd: None, run=_command_runner({}))
    assert ok.level is SetupCheckLevel.OK
    assert blocked.level is SetupCheckLevel.BLOCKED


@pytest.mark.unit
def test_check_gh_warns_when_missing_and_ok_when_present() -> None:
    """Verify a missing gh CLI warns (non-blocking) but present is OK."""
    present = check_gh(which=lambda _cmd: "/usr/bin/gh")
    missing = check_gh(which=lambda _cmd: None)
    assert present.level is SetupCheckLevel.OK
    assert missing.level is SetupCheckLevel.WARNING


# --- Python runtime -------------------------------------------------------


@pytest.mark.unit
def test_check_python_runtime_ok_and_blocked() -> None:
    """Verify the Python floor blocks old interpreters and accepts current."""
    ok = check_python_runtime(version=(3, 12))
    blocked = check_python_runtime(version=(3, 11))
    assert ok.level is SetupCheckLevel.OK
    assert blocked.level is SetupCheckLevel.BLOCKED


# --- Ports ----------------------------------------------------------------


@pytest.mark.unit
def test_check_ports_ok_when_free_and_warns_when_in_use() -> None:
    """Verify a free port is OK and an in-use port warns with the port in data."""
    free = check_ports(8000, is_available=lambda _port: True)
    in_use = check_ports(8000, is_available=lambda _port: False)
    assert free.level is SetupCheckLevel.OK
    assert in_use.level is SetupCheckLevel.WARNING
    assert in_use.data["port"] == 8000


# --- Disk -----------------------------------------------------------------


@pytest.mark.unit
def test_check_disk_levels() -> None:
    """Verify disk free bytes drive OK/WARNING and inspection failure warns."""
    ample = check_disk(Path("/tmp"), free_bytes=lambda _p: 100 * 1024**3)
    low = check_disk(Path("/tmp"), free_bytes=lambda _p: 1)
    unknown = check_disk(Path("/tmp"), free_bytes=lambda _p: None)
    assert ample.level is SetupCheckLevel.OK
    assert low.level is SetupCheckLevel.WARNING
    assert low.data["free_bytes"] == 1
    assert unknown.level is SetupCheckLevel.WARNING


# --- Shell / PATH ---------------------------------------------------------


@pytest.mark.unit
def test_check_shell_path_on_path_is_ok() -> None:
    """Verify a script dir present on PATH reports OK."""
    result = check_shell_path(
        script_dir=Path("/opt/awf/bin"),
        path_value="/usr/bin:/opt/awf/bin",
        shell="/bin/zsh",
    )
    assert result.level is SetupCheckLevel.OK


@pytest.mark.unit
def test_check_shell_path_off_path_warns_with_shell_fix() -> None:
    """Verify a script dir absent from PATH warns with a shell-specific fix."""
    result = check_shell_path(
        script_dir=Path("/opt/awf/bin"),
        path_value="/usr/bin:/bin",
        shell="/usr/bin/zsh",
    )
    assert result.level is SetupCheckLevel.WARNING
    assert result.fix is not None
    assert "zshrc" in result.fix


@pytest.mark.unit
def test_check_shell_path_resolves_symlinked_entries(tmp_path: Path) -> None:
    """Verify a symlinked PATH entry pointing at the script dir reports OK.

    Comparing unresolved paths would treat ``link_bin`` and ``real_bin`` as
    different and emit a false-negative warning even though ``awf`` is reachable.
    """
    real_bin = tmp_path / "real_bin"
    real_bin.mkdir()
    link_bin = tmp_path / "link_bin"
    link_bin.symlink_to(real_bin, target_is_directory=True)
    result = check_shell_path(
        script_dir=real_bin,
        path_value=str(link_bin),
        shell="/bin/zsh",
    )
    assert result.level is SetupCheckLevel.OK


@pytest.mark.unit
def test_check_shell_path_derives_script_dir_from_executable(tmp_path: Path) -> None:
    """Verify the script dir defaults to the executable's parent when omitted."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "python"
    executable.touch()
    result = check_shell_path(
        executable=str(executable),
        path_value=str(bin_dir),
        shell="/bin/zsh",
    )
    assert result.level is SetupCheckLevel.OK
    assert result.data["script_dir"] == str(bin_dir.resolve())


@pytest.mark.unit
def test_resolve_path_tolerates_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify path resolution falls back to the raw path on a filesystem error."""

    def boom(self: Path, *args: object, **kwargs: object) -> Path:
        raise OSError("resolve failed")

    monkeypatch.setattr(Path, "resolve", boom)
    raw = Path("/usr/local/bin")
    assert system_checks._resolve_path(raw) == raw


# --- Local capacity -------------------------------------------------------


@pytest.mark.unit
def test_check_local_capacity_ok_and_starved() -> None:
    """Verify adequate capacity is OK and a starved CPU count warns."""
    ok = check_local_capacity(
        cpu_count=lambda: 8,
        total_memory_bytes=lambda: 16 * 1024**3,
    )
    starved = check_local_capacity(
        cpu_count=lambda: 1,
        total_memory_bytes=lambda: 16 * 1024**3,
    )
    assert ok.level is SetupCheckLevel.OK
    assert starved.level is SetupCheckLevel.WARNING
    assert starved.data["cpus"] == 1


@pytest.mark.unit
def test_check_local_capacity_warns_on_low_memory() -> None:
    """Verify a low total-memory estimate warns."""
    result = check_local_capacity(
        cpu_count=lambda: 8,
        total_memory_bytes=lambda: 1 * 1024**3,
    )
    assert result.level is SetupCheckLevel.WARNING


# --- run_system_checks wiring --------------------------------------------


@pytest.mark.unit
def test_run_system_checks_orders_and_wires_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify aggregation runs every check and wires port/work dir from config."""
    captured: dict[str, object] = {}

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    monkeypatch.setattr(system_checks, "check_docker", lambda: fake_ok("docker"))
    monkeypatch.setattr(system_checks, "check_compose", lambda: fake_ok("compose"))
    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))

    def fake_ports(port: int) -> SetupCheckResult:
        captured["port"] = port
        return fake_ok("ports")

    def fake_disk(path: Path) -> SetupCheckResult:
        captured["disk_path"] = path
        return fake_ok("disk")

    monkeypatch.setattr(system_checks, "check_ports", fake_ports)
    monkeypatch.setattr(system_checks, "check_disk", fake_disk)
    monkeypatch.setattr(system_checks, "check_shell_path", lambda: fake_ok("shell_path"))
    monkeypatch.setattr(system_checks, "check_local_capacity", lambda: fake_ok("local_capacity"))

    results = run_system_checks(config=HostSetupConfig())

    assert [r.name for r in results] == [
        "docker",
        "compose",
        "git",
        "gh",
        "python",
        "ports",
        "disk",
        "shell_path",
        "local_capacity",
    ]
    assert captured["port"] == HostSetupConfig().api.host_port
    assert isinstance(captured["disk_path"], Path)


# --- Provider normalization ----------------------------------------------


@pytest.mark.unit
def test_normalize_provider_known_and_alias() -> None:
    """Verify canonical names and aliases normalize into the known set."""
    assert normalize_provider("github") == "github"
    assert normalize_provider("claude") == "claude_code"
    assert normalize_provider("OpenAI") == "codex"
    assert normalize_provider("github") in KNOWN_SETUP_PROVIDERS


@pytest.mark.unit
def test_normalize_provider_unknown_raises_reason_coded() -> None:
    """Verify an unknown provider raises a reason-coded SetupCheckError."""
    with pytest.raises(SetupCheckError) as excinfo:
        normalize_provider("bogus")
    assert excinfo.value.reason_code == SETUP_PROVIDER_UNKNOWN
    assert excinfo.value.details["provider"] == "bogus"


@pytest.mark.unit
def test_normalize_providers_dedupes_and_orders() -> None:
    """Verify repeated and aliased selectors de-dupe while preserving order."""
    assert normalize_providers(["github", "claude", "github", "anthropic"]) == [
        "github",
        "claude_code",
    ]


# --- Interactive guard ----------------------------------------------------


@pytest.mark.unit
def test_require_interactive_raises_only_when_non_interactive() -> None:
    """Verify the interactive guard raises only under --non-interactive."""
    require_interactive(False, "configure providers")  # no raise
    with pytest.raises(SetupCheckError) as excinfo:
        require_interactive(True, "configure providers")
    assert excinfo.value.reason_code == INTERACTIVE_INPUT_REQUIRED


# --- Readiness payload builder -------------------------------------------


def _ok(name: str) -> SetupCheckResult:
    return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")


@pytest.mark.unit
def test_build_payload_success_when_all_ok() -> None:
    """Verify an all-OK readiness pass yields a success payload and a next step."""
    payload = build_setup_readiness_payload(
        [_ok("docker"), _ok("git")],
        selected_providers=["github"],
        allow_plain_secrets=True,
        dry_run=True,
    )
    assert payload.status == "success"
    assert payload.issues == ()
    assert payload.details["selected_providers"] == ["github"]
    assert payload.details["plain_file_consent"] is True
    assert payload.details["dry_run"] is True
    assert payload.next_steps


@pytest.mark.unit
def test_build_payload_blocked_with_mixed_results() -> None:
    """Verify blockers and warnings become issues with a blocked status."""
    docker_blocked = SetupCheckResult(
        name="docker",
        level=SetupCheckLevel.BLOCKED,
        summary="Docker daemon unreachable.",
        detail="`docker info` failed.",
        fix="Start Docker.",
    )
    gh_warning = SetupCheckResult(
        name="gh",
        level=SetupCheckLevel.WARNING,
        summary="gh missing.",
        detail="GitHub CLI not found.",
        fix="Install gh.",
    )
    payload = build_setup_readiness_payload(
        [docker_blocked, gh_warning],
        selected_providers=[],
        allow_plain_secrets=False,
        dry_run=True,
    )
    assert payload.status == "blocked"
    assert payload.reason_code == SETUP_READINESS_FAILED
    severities = {issue.severity for issue in payload.issues}
    assert severities == {"blocked", "warning"}

    rendered_json = render_first_run_json(payload)
    rendered_pretty = render_first_run_pretty(payload)
    assert rendered_json["status"] == "blocked"
    assert "Docs:" in rendered_pretty
    assert "Next:" in rendered_pretty


@pytest.mark.unit
def test_build_payload_source_checkout_error_is_blocked_issue() -> None:
    """Verify a source-checkout error adds a SOURCE_CHECKOUT_INVALID blocker."""
    error = SourceCheckoutError(
        reason_code=SOURCE_CHECKOUT_INVALID,
        message="AWF source checkout is missing required assets.",
        root=Path("/tmp/not-awf"),
        missing_markers=("pyproject.toml", "uv.lock"),
        details={"path_status": "missing"},
    )
    payload = build_setup_readiness_payload(
        [_ok("docker")],
        source_checkout_error=error,
    )
    assert payload.status == "blocked"
    source_issue = next(
        issue for issue in payload.issues if issue.reason_code == SOURCE_CHECKOUT_INVALID
    )
    assert source_issue.details["missing_markers"] == ["pyproject.toml", "uv.lock"]
    assert source_issue.details["path_status"] == "missing"


@pytest.mark.unit
def test_build_payload_redacts_token_shaped_check_data() -> None:
    """Verify token-shaped values inside check data are redacted on render."""
    leaky = SetupCheckResult(
        name="docker",
        level=SetupCheckLevel.WARNING,
        summary="warn",
        detail="warn",
        fix="fix",
        data={"hint": "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGG1234"},
    )
    payload = build_setup_readiness_payload([leaky])
    rendered = render_first_run_json(payload)
    assert "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGG1234" not in str(rendered)


@pytest.mark.unit
def test_build_payload_warning_only_status() -> None:
    """Verify a warnings-only readiness pass yields a warning status/summary."""
    warn = SetupCheckResult(
        name="gh",
        level=SetupCheckLevel.WARNING,
        summary="warn",
        detail="detail",
        fix="fix",
    )
    payload = build_setup_readiness_payload([warn])
    assert payload.status == "warning"
    assert "warning" in payload.summary


# --- Remaining check branches --------------------------------------------


@pytest.mark.unit
def test_check_git_blocked_when_version_fails() -> None:
    """Verify a present git whose --version fails is blocked."""
    result = check_git(which=lambda _cmd: "/usr/bin/git", run=_command_runner({}))
    assert result.level is SetupCheckLevel.BLOCKED


@pytest.mark.unit
def test_check_local_capacity_warns_when_cpu_count_unknown() -> None:
    """Verify an unknown CPU count warns without recording a cpus value."""
    result = check_local_capacity(
        cpu_count=lambda: None,
        total_memory_bytes=lambda: 16 * 1024**3,
    )
    assert result.level is SetupCheckLevel.WARNING
    assert "cpus" not in result.data


@pytest.mark.unit
def test_check_local_capacity_ok_when_memory_unknown() -> None:
    """Verify adequate CPUs with an unknown memory estimate is still OK."""
    result = check_local_capacity(
        cpu_count=lambda: 8,
        total_memory_bytes=lambda: None,
    )
    assert result.level is SetupCheckLevel.OK
    assert "memory_bytes" not in result.data


@pytest.mark.unit
def test_build_payload_source_checkout_error_without_missing_markers() -> None:
    """Verify a source error with no missing markers still carries its details."""
    error = SourceCheckoutError(
        reason_code=SOURCE_CHECKOUT_INVALID,
        message="AWF source checkout path is not readable.",
        root=Path("/tmp/unreadable"),
        details={"path_status": "unreadable"},
    )
    payload = build_setup_readiness_payload([_ok("docker")], source_checkout_error=error)
    source_issue = next(
        issue for issue in payload.issues if issue.reason_code == SOURCE_CHECKOUT_INVALID
    )
    assert "missing_markers" not in source_issue.details
    assert source_issue.details["path_status"] == "unreadable"


@pytest.mark.unit
def test_shell_path_fix_variants() -> None:
    """Verify the PATH fix hint is tailored per shell."""
    script_dir = Path("/opt/awf/bin")
    assert "fish_add_path" in system_checks._shell_path_fix("/usr/bin/fish", script_dir)
    assert "bashrc" in system_checks._shell_path_fix("/bin/bash", script_dir)
    assert "shell profile" in system_checks._shell_path_fix("", script_dir)


# --- Default real-IO probe helpers (hermetic) ----------------------------


class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.unit
def test_default_command_runner_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the default runner captures a completed probe result."""
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _FakeCompleted(0, "out", "err"))
    result = system_checks._default_command_runner(["echo", "hi"])
    assert result is not None
    assert result.returncode == 0
    assert result.stdout == "out"


@pytest.mark.unit
def test_default_command_runner_returns_none_when_unlaunchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a probe that cannot launch returns None (no raise)."""
    import subprocess

    def boom(*_a: object, **_k: object) -> _FakeCompleted:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert system_checks._default_command_runner(["missing-binary"]) is None


@pytest.mark.unit
def test_default_command_runner_decodes_with_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe output must decode with errors='replace' so a non-UTF-8 locale or a

    binary emitting raw bytes cannot raise UnicodeDecodeError (a ValueError
    subclass the probe except-tuple does not catch) and crash ``awf setup``.
    """
    import subprocess

    captured: dict[str, object] = {}

    def fake_run(*_a: object, **kwargs: object) -> _FakeCompleted:
        captured.update(kwargs)
        return _FakeCompleted(0, "out", "err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = system_checks._default_command_runner(["probe"])
    assert result is not None
    assert captured["text"] is True
    assert captured["errors"] == "replace"


@pytest.mark.unit
def test_default_port_available_detects_in_use_and_free() -> None:
    """Verify the default port probe distinguishes in-use from free ports."""
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        assert system_checks._default_port_available(port) is False
    finally:
        listener.close()
    assert system_checks._default_port_available(port) is True


@pytest.mark.unit
def test_default_free_disk_bytes_real_and_parent_fallback(tmp_path: Path) -> None:
    """Verify free-disk reads a real path and falls back to an existing parent."""
    assert system_checks._default_free_disk_bytes(tmp_path) >= 0
    nested = tmp_path / "does" / "not" / "exist"
    assert system_checks._default_free_disk_bytes(nested) >= 0


@pytest.mark.unit
def test_default_free_disk_bytes_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify an unreadable filesystem yields None rather than raising."""
    import shutil

    def boom(_path: object) -> object:
        raise OSError

    monkeypatch.setattr(shutil, "disk_usage", boom)
    assert system_checks._default_free_disk_bytes("/anything") is None


@pytest.mark.unit
def test_default_total_memory_bytes_real() -> None:
    """Verify the default memory probe returns None or a positive estimate."""
    value = system_checks._default_total_memory_bytes()
    assert value is None or value > 0


@pytest.mark.unit
def test_default_total_memory_bytes_handles_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify sysconf failures and non-positive values yield None."""
    import os

    def boom(_name: str) -> int:
        raise ValueError

    monkeypatch.setattr(os, "sysconf", boom)
    assert system_checks._default_total_memory_bytes() is None

    monkeypatch.setattr(os, "sysconf", lambda _name: 0)
    assert system_checks._default_total_memory_bytes() is None
