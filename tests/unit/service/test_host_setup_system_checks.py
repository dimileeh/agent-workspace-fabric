"""Fixture-driven tests for host setup system checks and readiness payload."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from awf.host_setup import system_checks
from awf.host_setup.config import DEFAULT_API_HOST_PORT, ApiConfig, HostSetupConfig
from awf.host_setup.rendering import (
    INTERACTIVE_INPUT_REQUIRED,
    SETUP_PROVIDER_UNKNOWN,
    SETUP_READINESS_FAILED,
    render_first_run_json,
    render_first_run_pretty,
)
from awf.host_setup.source_assets import SOURCE_CHECKOUT_INVALID, SourceCheckoutError
from awf.host_setup.system_checks import (
    KNOWN_SETUP_PROVIDERS,
    CommandResult,
    PortProbeResult,
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


# --- Docker probe environment (resolved daemon selection) -----------------


@pytest.mark.unit
def test_docker_probe_environ_returns_none_without_service_env() -> None:
    """No resolved service env means the probe inherits the caller environment."""
    assert system_checks._docker_probe_environ(None) is None


@pytest.mark.unit
def test_docker_probe_environ_materializes_awf_docker_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AWF_DOCKER_HOST`` becomes ``DOCKER_HOST`` and scrubs conflicting context.

    ``awf start`` selects the daemon from the resolved service env via
    ``bootstrap._docker_cli_environ``; the readiness probe must reproduce that same
    selection so it talks to the daemon ``awf start`` will use.
    """
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    resolved = system_checks._docker_probe_environ({"AWF_DOCKER_HOST": "tcp://remote:2375"})

    assert resolved is not None
    assert resolved["DOCKER_HOST"] == "tcp://remote:2375"
    assert "AWF_DOCKER_HOST" not in resolved
    assert "DOCKER_CONTEXT" not in resolved


@pytest.mark.unit
def test_docker_probe_environ_clears_inherited_docker_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service env that blanks ``DOCKER_HOST`` drops the inherited daemon.

    Mirrors ``awf start``: when the resolved env explicitly clears an inherited
    ``DOCKER_HOST``, the probe must fall back to the default local daemon rather
    than the inherited remote one.
    """
    monkeypatch.setenv("DOCKER_HOST", "tcp://inherited:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")

    resolved = system_checks._docker_probe_environ({"DOCKER_HOST": ""})

    assert resolved is not None
    assert "DOCKER_HOST" not in resolved
    assert "DOCKER_CONTEXT" not in resolved


@pytest.mark.unit
def test_run_system_checks_docker_probe_targets_resolved_docker_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Docker/Compose readiness probes target the daemon ``awf start`` will use.

    ``check_docker`` previously ran ``docker info`` with the bare process
    environment, so a resolved service env that points Docker at a different
    daemon (``AWF_DOCKER_HOST``) was ignored and ``awf setup --dry-run`` could
    block on (or pass against) a daemon ``awf start`` never uses. Aggregation now
    threads the resolved env into both probes.
    """
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    class _Completed:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def fake_run(
        args: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        errors: str,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> _Completed:
        calls.append((tuple(args), env))
        return _Completed()

    monkeypatch.setattr(system_checks.subprocess, "run", fake_run)
    monkeypatch.setattr(system_checks.shutil, "which", lambda _cmd, **_kwargs: "/usr/bin/docker")
    _stub_non_docker_checks_ok(monkeypatch)

    results = run_system_checks(
        config=HostSetupConfig(),
        environ={"AWF_DOCKER_HOST": "tcp://remote:2375"},
    )

    names = [r.name for r in results]
    assert names[:2] == ["docker", "compose"]
    info_env = next(env for args, env in calls if args[:2] == ("docker", "info"))
    compose_env = next(env for args, env in calls if args == ("docker", "compose", "version"))
    assert info_env is not None
    assert info_env["DOCKER_HOST"] == "tcp://remote:2375"
    assert "AWF_DOCKER_HOST" not in info_env
    assert compose_env is not None
    assert compose_env["DOCKER_HOST"] == "tcp://remote:2375"


@pytest.mark.unit
def test_run_system_checks_docker_probe_inherits_env_without_service_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no resolved service env the probe inherits the caller environment."""
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    class _Completed:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def fake_run(
        args: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        errors: str,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> _Completed:
        calls.append((tuple(args), env))
        return _Completed()

    monkeypatch.setattr(system_checks.subprocess, "run", fake_run)
    monkeypatch.setattr(system_checks.shutil, "which", lambda _cmd: "/usr/bin/docker")
    _stub_non_docker_checks_ok(monkeypatch)

    run_system_checks(config=HostSetupConfig())

    info_env = next(env for args, env in calls if args[:2] == ("docker", "info"))
    assert info_env is None


@pytest.mark.unit
def test_run_system_checks_docker_binary_lookup_uses_resolved_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Docker binary-presence gate searches the resolved service env PATH.

    The readiness runner locates ``docker`` via the resolved service env PATH
    (``subprocess`` honours ``env['PATH']`` for executable resolution, exactly as
    ``awf start`` hands its merged service env to the Docker subprocesses), so the
    binary-presence ``which`` gate must search that same PATH. Using the bare
    process PATH would report "Docker CLI is not installed" for a ``docker``
    reachable only through the resolved service env's PATH -- falsely blocking
    ``awf setup --dry-run`` for a startup configuration that would succeed.
    """

    class _Completed:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def fake_run(
        args: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        errors: str,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> _Completed:
        return _Completed()

    seen: list[str | None] = []

    def fake_which(cmd: str, path: str | None = None) -> str | None:
        seen.append(path)
        # ``docker`` resolves only against the service-env PATH, never the bare
        # process environment.
        if cmd == "docker" and path is not None and "/opt/docker/bin" in path:
            return "/opt/docker/bin/docker"
        return None

    monkeypatch.setattr(system_checks.subprocess, "run", fake_run)
    monkeypatch.setattr(system_checks.shutil, "which", fake_which)
    _stub_non_docker_checks_ok(monkeypatch)

    results = run_system_checks(
        config=HostSetupConfig(),
        environ={"PATH": "/opt/docker/bin"},
    )

    docker_check = next(r for r in results if r.name == "docker")
    assert docker_check.level is SetupCheckLevel.OK
    assert docker_check.data["available"] is True
    # The gate searched the resolved service-env PATH, not the bare process PATH.
    assert any(path is not None and "/opt/docker/bin" in path for path in seen)


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
def test_check_compose_blocked_when_only_legacy_binary() -> None:
    """Verify legacy docker-compose alone blocks: startup uses the plugin only.

    AWF's bootstrap and per-workspace stack lifecycle invoke the ``docker compose``
    plugin with no fallback to the legacy ``docker-compose`` binary, so a host with
    only the legacy binary would pass readiness yet fail ``awf start``. Readiness must
    require the plugin startup actually uses.
    """
    result = check_compose(
        run=_command_runner(
            {
                ("docker", "compose", "version"): CommandResult(1),
                ("docker-compose", "version"): CommandResult(0),
            }
        )
    )
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["variant"] is None
    assert result.data["legacy_docker_compose"] is True


@pytest.mark.unit
def test_check_compose_blocked_when_neither_available() -> None:
    """Verify a missing compose plugin and binary block."""
    result = check_compose(run=_command_runner({}))
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["legacy_docker_compose"] is False


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
def test_check_ports_ok_when_free_and_blocks_when_in_use() -> None:
    """Verify a free port is OK and an in-use port BLOCKS with the port in data.

    The local-service Compose stack publishes the API on a fixed host port
    (``${AWF_API_HOST_PORT:-8000}:8000``) with no auto-fallback, so an occupied
    port makes ``awf start`` fail to publish it. Reporting only a warning would
    let ``awf setup --dry-run`` exit 0 and still advise ``awf start``, which then
    fails — so an occupied API port is a readiness blocker, not advisory.
    """
    free = check_ports(8000, probe=lambda _port: PortProbeResult.FREE)
    in_use = check_ports(8000, probe=lambda _port: PortProbeResult.IN_USE)
    assert free.level is SetupCheckLevel.OK
    assert free.data["available"] is True
    assert in_use.level is SetupCheckLevel.BLOCKED
    assert in_use.data["port"] == 8000
    assert in_use.data["available"] is False
    assert in_use.fix is not None
    # The fix must not promise an auto-fallback that the codebase does not implement.
    assert "can also start on another port" not in in_use.fix


@pytest.mark.unit
def test_check_ports_distinguishes_permission_and_other_bind_errors() -> None:
    """Verify non-occupancy bind failures get their own cause/fix, not "in use".

    The setup probe runs as the current (possibly unprivileged) user, while
    ``awf start`` publishes the port through the root Docker daemon, so a
    permission or other bind error does not prove the port is unusable. Such
    failures must be reported as advisory warnings with an accurate cause and
    remediation — never mislabelled as occupancy with a "free the port" fix.
    """
    permission = check_ports(80, probe=lambda _port: PortProbeResult.PERMISSION_DENIED)
    other = check_ports(8000, probe=lambda _port: PortProbeResult.UNAVAILABLE)

    assert permission.level is SetupCheckLevel.WARNING
    assert permission.data["probe"] == PortProbeResult.PERMISSION_DENIED.value
    assert "permission" in permission.summary.lower()
    assert "already in use" not in permission.summary
    assert permission.fix is not None
    assert "Free the port" not in permission.fix

    assert other.level is SetupCheckLevel.WARNING
    assert other.data["probe"] == PortProbeResult.UNAVAILABLE.value
    assert "already in use" not in other.summary
    assert other.fix is not None
    assert "Free the port" not in other.fix


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
    """Verify the script dir falls back to the interpreter parent when no entry point.

    When ``which`` cannot locate the ``awf`` entry point (e.g. running from
    source via ``python -m awf``), the check falls back to the interpreter's
    parent directory.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "python"
    executable.touch()
    result = check_shell_path(
        executable=str(executable),
        path_value=str(bin_dir),
        shell="/bin/zsh",
        which=lambda _name: None,
    )
    assert result.level is SetupCheckLevel.OK
    assert result.data["script_dir"] == str(bin_dir.resolve())


@pytest.mark.unit
def test_check_shell_path_prefers_entry_point_over_interpreter(tmp_path: Path) -> None:
    """Verify the script dir comes from the awf entry point, not the interpreter.

    Regression for the ``uv tool install awf`` case: the interpreter lives in an
    isolated tool venv whose parent is *not* on PATH, while the ``awf`` console
    script sits in a separate directory that *is* on PATH. Inferring the script
    directory from the interpreter would emit a false "not on PATH" warning even
    though ``awf`` is reachable.
    """
    tool_venv_bin = tmp_path / "uv" / "tools" / "awf" / "bin"
    tool_venv_bin.mkdir(parents=True)
    interpreter = tool_venv_bin / "python"
    interpreter.touch()
    entry_bin = tmp_path / "local" / "bin"
    entry_bin.mkdir(parents=True)
    entry_point = entry_bin / "awf"
    entry_point.touch()
    result = check_shell_path(
        executable=str(interpreter),
        path_value=str(entry_bin),  # only the entry-point dir is on PATH
        shell="/bin/zsh",
        which=lambda _name: str(entry_point),
    )
    assert result.level is SetupCheckLevel.OK
    assert result.data["script_dir"] == str(entry_bin.resolve())


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
    assert ok.detail == "Host-local CPU and memory estimates are at or above the recommended floor."
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


@pytest.mark.unit
def test_check_local_capacity_reports_cpu_and_memory_issues_together() -> None:
    """A host starved of both CPU and memory must surface both in one warning.

    Regression: the check used to early-return on the first failing condition,
    so an operator only ever saw one issue per run and would step-debug the
    next after fixing the first.
    """
    result = check_local_capacity(
        cpu_count=lambda: 1,
        total_memory_bytes=lambda: 1 * 1024**3,
    )
    assert result.level is SetupCheckLevel.WARNING
    assert "usable CPU(s) is below the recommended" in result.detail
    assert "bytes of memory is below the recommended" in result.detail
    # The summary is the first line an operator reads at a glance: when both
    # CPU and memory are starved it must name both, not just CPUs.
    assert result.summary == (
        "Detected fewer CPUs and less memory than recommended for AWF workspaces."
    )


@pytest.mark.unit
def test_check_local_capacity_unknown_cpu_and_low_memory_names_both() -> None:
    """An unknown CPU count plus low memory must name memory, not claim 'fewer CPUs'.

    Regression: the summary branch fired ``elif low_cpu or unknown_cpu`` and
    emitted 'Detected fewer CPUs than recommended' whenever the CPU count was
    merely unknown. With low memory too, that one-liner both misreported the
    CPU count as 'fewer' (it is unknown, not low) and dropped the memory
    shortfall entirely, steering an operator to provision CPUs they may not
    need while leaving the real memory problem invisible.
    """
    result = check_local_capacity(
        cpu_count=lambda: None,
        total_memory_bytes=lambda: 1 * 1024**3,
    )
    assert result.level is SetupCheckLevel.WARNING
    assert "cpus" not in result.data
    assert "bytes of memory is below the recommended" in result.detail
    assert "os.cpu_count() returned no value" in result.detail
    # The summary must surface the memory shortfall and describe the CPU count
    # as unknown rather than 'fewer'.
    assert result.summary == "Detected less memory and an unknown CPU count for AWF workspaces."
    assert "fewer CPUs" not in result.summary


@pytest.mark.unit
def test_check_local_capacity_fix_names_only_the_starved_resource() -> None:
    """The remediation hint must name only the resource(s) actually below floor.

    Regression for PR #332 review (comment issue:4585200251): the fix message
    read "Provision more CPU or memory ..." even when a single resource was
    short, telling an operator to provision both when only one needed attention.
    The narrowed wording must track the affected dimension(s) the way the
    summary already does.
    """
    enough_memory = 16 * 1024**3
    starved_memory = 1 * 1024**3

    low_cpu_only = check_local_capacity(
        cpu_count=lambda: 1, total_memory_bytes=lambda: enough_memory
    )
    low_memory_only = check_local_capacity(
        cpu_count=lambda: 8, total_memory_bytes=lambda: starved_memory
    )
    both_low = check_local_capacity(cpu_count=lambda: 1, total_memory_bytes=lambda: starved_memory)
    unknown_cpu_low_memory = check_local_capacity(
        cpu_count=lambda: None, total_memory_bytes=lambda: starved_memory
    )
    unknown_cpu_only = check_local_capacity(
        cpu_count=lambda: None, total_memory_bytes=lambda: enough_memory
    )

    assert low_cpu_only.fix == (
        "Provision more CPU or expect slower, lower-concurrency workspaces."
    )
    assert low_memory_only.fix == (
        "Provision more memory or expect slower, lower-concurrency workspaces."
    )
    # Both starved keeps the combined wording — provisioning either helps.
    assert both_low.fix == (
        "Provision more CPU or memory or expect slower, lower-concurrency workspaces."
    )
    # An unknown CPU count is not a confirmed CPU shortfall: when memory is the
    # only thing below floor, the fix must point at memory alone, not "CPU or
    # memory".
    assert unknown_cpu_low_memory.fix == (
        "Provision more memory or expect slower, lower-concurrency workspaces."
    )
    # CPU count unknown with adequate memory: the only actionable step is to
    # expose CPU info, not to provision anything.
    assert unknown_cpu_only.fix == "Verify the host environment exposes CPU information."


@pytest.mark.unit
def test_check_local_capacity_unknown_cpu_and_unknown_memory_names_both() -> None:
    """Both an unknown CPU count and an unknown memory total must surface together.

    Regression for PR #332 review (comment issue:4585200251): when both
    ``os.cpu_count()`` and the memory estimate returned no value, only the CPU
    gap was reported — the summary, detail, and fix all omitted memory, so an
    operator who fixed CPU reporting would discover the memory gap only on a
    follow-up run, a step-debug loop for two independent unknowns.
    """
    result = check_local_capacity(
        cpu_count=lambda: None,
        total_memory_bytes=lambda: None,
    )
    assert result.level is SetupCheckLevel.WARNING
    assert "cpus" not in result.data
    assert "memory_bytes" not in result.data
    assert result.summary == (
        "CPU count and memory total could not be determined for AWF workspaces."
    )
    assert "os.cpu_count() returned no value" in result.detail
    assert "total memory could not be determined" in result.detail
    # Neither dimension is a confirmed shortfall, so the only action is to expose
    # the host's CPU *and* memory information, not provision anything.
    assert result.fix == "Verify the host environment exposes CPU and memory information."


@pytest.mark.unit
def test_check_local_capacity_low_cpu_and_unknown_memory_surfaces_memory() -> None:
    """A confirmed CPU shortfall with an unknown memory total must still surface memory.

    Symmetric to the unknown-CPU / low-memory case: the unknown dimension never
    widens the *fix* (an unknown total is not a confirmed shortfall, so the only
    actionable step is the confirmed CPU shortfall), yet it must still appear in
    the summary and detail so the gap is not invisible until a follow-up run.
    """
    result = check_local_capacity(
        cpu_count=lambda: 1,
        total_memory_bytes=lambda: None,
    )
    assert result.level is SetupCheckLevel.WARNING
    assert "memory_bytes" not in result.data
    assert result.summary == (
        "Detected fewer CPUs than recommended and an unknown memory total for AWF workspaces."
    )
    assert "usable CPU(s) is below the recommended" in result.detail
    assert "total memory could not be determined" in result.detail
    # An unknown memory total is not a confirmed shortfall, so the fix names the
    # confirmed CPU shortfall alone (mirroring unknown-CPU + low-memory).
    assert result.fix == "Provision more CPU or expect slower, lower-concurrency workspaces."


@pytest.mark.unit
def test_check_local_capacity_unknown_memory_alone_stays_ok() -> None:
    """An unknown memory total with adequate CPUs must stay OK, not warn.

    Guards the boundary the issue:4585200251 fix must not cross: the memory gap
    is folded into an existing capacity warning, but on its own it is advisory —
    the OK branch already notes it — and must not promote a healthy host to a
    warning.
    """
    result = check_local_capacity(
        cpu_count=lambda: 8,
        total_memory_bytes=lambda: None,
    )
    assert result.level is SetupCheckLevel.OK
    assert "memory capacity could not be determined" in result.detail


# --- run_system_checks wiring --------------------------------------------


@pytest.mark.unit
def test_run_system_checks_orders_and_wires_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify aggregation runs every check and wires port/work dir from config."""
    captured: dict[str, object] = {}

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    # A real OK docker result carries ``data={"available": True}``; aggregation
    # guards the compose probe on that flag, so the fake must mirror it or compose
    # would be skipped and drop out of the ordered result list.
    monkeypatch.setattr(
        system_checks,
        "check_docker",
        lambda **_kwargs: SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.OK,
            summary="ok",
            detail="ok",
            data={"available": True},
        ),
    )
    monkeypatch.setattr(system_checks, "check_compose", lambda **_kwargs: fake_ok("compose"))
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
    monkeypatch.setattr(
        system_checks, "check_postgres_port", lambda _port: fake_ok("postgres_port")
    )
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
        "postgres_port",
        "disk",
        "host_home",
        "required_service_env",
        "shell_path",
        "local_capacity",
    ]
    assert captured["port"] == HostSetupConfig().api.host_port
    assert isinstance(captured["disk_path"], Path)


def _stub_non_docker_checks_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every non-docker/compose probe to OK so aggregation runs in isolation."""

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))
    monkeypatch.setattr(
        system_checks, "check_postgres_port", lambda _port: fake_ok("postgres_port")
    )
    monkeypatch.setattr(system_checks, "check_disk", lambda _path: fake_ok("disk"))
    monkeypatch.setattr(system_checks, "check_shell_path", lambda: fake_ok("shell_path"))
    monkeypatch.setattr(system_checks, "check_local_capacity", lambda: fake_ok("local_capacity"))


@pytest.mark.unit
def test_run_system_checks_omits_compose_when_docker_binary_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing Docker binary surfaces once, not as a second compose BLOCKED.

    ``check_docker`` already reports the absent binary as BLOCKED. Running
    ``check_compose`` afterwards would re-probe the same missing ``docker`` binary
    and append a second BLOCKED result for one root cause, so an operator whose
    only problem is a missing Docker install would chase a phantom Compose fix.
    Aggregation guards the compose probe on ``check_docker``'s ``available`` flag,
    so the probe is skipped entirely (not merely dropped) when Docker is absent.
    """
    docker_absent = SetupCheckResult(
        name="docker",
        level=SetupCheckLevel.BLOCKED,
        summary="missing",
        detail="missing",
        data={"binary": "docker", "available": False},
    )
    compose_calls: list[int] = []

    def tracking_compose(**_kwargs: object) -> SetupCheckResult:
        compose_calls.append(1)
        return SetupCheckResult(
            name="compose", level=SetupCheckLevel.BLOCKED, summary="x", detail="x"
        )

    monkeypatch.setattr(system_checks, "check_docker", lambda **_kwargs: docker_absent)
    monkeypatch.setattr(system_checks, "check_compose", tracking_compose)
    _stub_non_docker_checks_ok(monkeypatch)

    results = run_system_checks(config=HostSetupConfig())

    names = [r.name for r in results]
    assert compose_calls == []
    assert names == [
        "docker",
        "git",
        "gh",
        "python",
        "ports",
        "postgres_port",
        "disk",
        "host_home",
        "required_service_env",
        "shell_path",
        "local_capacity",
    ]


@pytest.mark.unit
def test_run_system_checks_keeps_compose_when_docker_daemon_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable Docker binary with a stopped daemon still gets a compose probe.

    ``docker compose version`` reports the plugin without contacting the daemon,
    so a down daemon (``available`` true, ``daemon`` false) is independent of
    whether the Compose plugin is installed. The guard keys on ``available``, not
    on the docker check being OK, so a genuinely missing plugin is not masked by a
    daemon outage.
    """
    docker_daemon_down = SetupCheckResult(
        name="docker",
        level=SetupCheckLevel.BLOCKED,
        summary="daemon down",
        detail="daemon down",
        data={"binary": "docker", "available": True, "daemon": False},
    )
    monkeypatch.setattr(system_checks, "check_docker", lambda **_kwargs: docker_daemon_down)
    monkeypatch.setattr(
        system_checks,
        "check_compose",
        lambda **_kwargs: SetupCheckResult(
            name="compose", level=SetupCheckLevel.OK, summary="ok", detail="ok"
        ),
    )
    _stub_non_docker_checks_ok(monkeypatch)

    names = [r.name for r in run_system_checks(config=HostSetupConfig())]

    assert names.index("compose") == 1


@pytest.mark.unit
def test_run_system_checks_blocks_unresolvable_work_dir_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``docker/compose/.env`` ``AWF_HOST_WORK_DIR: ~olduser/...`` blocks, never crashes.

    A ``~user`` override is non-absolute: Compose and ``awf service`` keep it
    verbatim (they do not expand ``~``) and Docker's mount target must be
    absolute, so ``awf start`` could never mount it. The readiness probe blocks it
    before attempting any expansion, so an unresolvable ``~user`` component —
    which would make ``Path.expanduser`` raise ``RuntimeError`` — can never abort
    ``awf setup --dry-run`` with a traceback.
    """
    import os.path

    # Simulate a host that cannot resolve the ``~user`` component: if anything
    # tried to expand it, ``Path.expanduser`` would raise ``RuntimeError``. The
    # non-absolute block fires first, so nothing does.
    monkeypatch.setattr(os.path, "expanduser", lambda value: value)

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    for name in (
        "check_docker",
        "check_compose",
        "check_git",
        "check_gh",
        "check_python_runtime",
        "check_shell_path",
        "check_local_capacity",
    ):
        monkeypatch.setattr(system_checks, name, lambda name=name, **_kwargs: fake_ok(name))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))
    monkeypatch.setattr(
        system_checks, "check_postgres_port", lambda _port: fake_ok("postgres_port")
    )

    captured: dict[str, object] = {}

    def fake_disk(path: Path) -> SetupCheckResult:
        captured["disk_path"] = path
        return fake_ok("disk")

    monkeypatch.setattr(system_checks, "check_disk", fake_disk)

    results = run_system_checks(
        config=HostSetupConfig(),
        environ={"AWF_HOST_WORK_DIR": "~olduser/.awf/service"},
    )

    assert [r.name for r in results].count("disk") == 1
    disk = next(result for result in results if result.name == "disk")
    assert disk.level is SetupCheckLevel.BLOCKED
    assert disk.data["env_value"] == "~olduser/.awf/service"
    # Blocked before any expansion, so the disk probe never runs (no traceback).
    assert "disk_path" not in captured


# --- Provider normalization ----------------------------------------------


@pytest.mark.unit
def test_normalize_provider_known_and_alias() -> None:
    """Verify canonical names and aliases normalize into the known set."""
    assert normalize_provider("github") == "github"
    assert normalize_provider("claude") == "claude_code"
    assert normalize_provider("OpenAI") == "codex"
    assert normalize_provider("github") in KNOWN_SETUP_PROVIDERS


@pytest.mark.unit
def test_normalize_provider_accepts_grok_and_xai_alias() -> None:
    """Verify the supported Grok runtime is selectable through setup.

    Grok is a first-class provider everywhere else (provider readiness,
    ``awf service`` help, the agent adapters), so ``awf setup --provider grok``
    must resolve instead of failing ``SETUP_PROVIDER_UNKNOWN``. ``xai`` mirrors
    the brand alias every other provider carries and matches the credential key
    Grok uses across the codebase.
    """
    assert normalize_provider("grok") == "grok"
    assert "grok" in KNOWN_SETUP_PROVIDERS
    assert normalize_provider("xai") == "grok"
    assert normalize_provider("XAI") == "grok"


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
def test_build_payload_command_label_is_overridable() -> None:
    """Verify the rendered command label is injectable, defaulting to ``awf setup``.

    The CLI layer owns the command name; the domain builder now accepts it as a
    parameter instead of hardcoding a presentation detail, so a command rename
    stays a single-source edit.
    """
    assert build_setup_readiness_payload([_ok("docker")]).command == "awf setup"
    assert (
        build_setup_readiness_payload([_ok("docker")], command="awf check").command == "awf check"
    )


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
    # The detail must not claim the memory estimate is at/above the floor when
    # memory could not be determined (e.g. Windows where os.sysconf is missing).
    assert "memory capacity could not be determined" in result.detail
    assert "CPU and memory estimates are at or above" not in result.detail


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
def test_default_command_runner_returns_none_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a probe that times out returns None (no raise).

    ``subprocess.TimeoutExpired`` is not an ``OSError`` subclass, so it must be
    listed explicitly in the ``except`` clause alongside ``OSError``.
    """
    import subprocess

    def slow(*_a: object, **_k: object) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd="probe", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", slow)
    assert system_checks._default_command_runner(["probe"]) is None


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
def test_default_port_probe_detects_in_use_and_free() -> None:
    """Verify the default port probe distinguishes in-use from free ports."""
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        assert system_checks._default_port_probe(port) is PortProbeResult.IN_USE
    finally:
        listener.close()
    assert system_checks._default_port_probe(port) is PortProbeResult.FREE


@pytest.mark.unit
def test_default_port_probe_classifies_bind_errno() -> None:
    """Verify the probe maps bind errnos to distinct outcomes, not just in-use.

    EADDRINUSE is occupancy, EACCES/EPERM is a permission failure (e.g. a
    privileged ``<1024`` port without root), and any other OSError is an
    unspecified bind failure rather than being collapsed into "port in use".
    """
    import errno
    import socket

    class _BindError:
        def __init__(self, exc: OSError) -> None:
            self._exc = exc

        def __enter__(self) -> _BindError:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def setsockopt(self, *_args: object) -> None:
            return None

        def bind(self, *_args: object) -> None:
            raise self._exc

    def _factory(exc: OSError) -> Callable[..., _BindError]:
        return lambda *_args, **_kwargs: _BindError(exc)

    cases = {
        errno.EADDRINUSE: PortProbeResult.IN_USE,
        errno.EACCES: PortProbeResult.PERMISSION_DENIED,
        errno.EPERM: PortProbeResult.PERMISSION_DENIED,
        errno.EADDRNOTAVAIL: PortProbeResult.UNAVAILABLE,
    }
    for code, expected in cases.items():
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(socket, "socket", _factory(OSError(code, "boom")))
            assert system_checks._default_port_probe(8000) is expected


@pytest.mark.unit
def test_default_port_probe_detects_non_loopback_listener() -> None:
    """Verify the probe matches Docker's all-interface bind, not just loopback.

    ``docker/compose/local-service.yml`` publishes the API port without a host
    IP (``${AWF_API_HOST_PORT:-8000}:8000``), so Docker reserves it on every
    host interface (``0.0.0.0``). A listener on a non-loopback address must
    therefore be reported as in-use; a loopback-only (``127.0.0.1``) probe would
    miss it and let ``awf start`` fail later to publish the port.
    """
    import socket

    def _non_loopback_ipv4() -> str | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as discover:
                discover.connect(("8.8.8.8", 80))
                address = discover.getsockname()[0]
        except OSError:
            return None
        return address if address and not address.startswith("127.") else None

    host_ip = _non_loopback_ipv4()
    if host_ip is None:
        pytest.skip("no non-loopback IPv4 interface available to bind")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host_ip, 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        assert system_checks._default_port_probe(port) is PortProbeResult.IN_USE
    finally:
        listener.close()


@pytest.mark.unit
def test_loopback_port_probe_detects_in_use_and_free() -> None:
    """Verify the loopback probe distinguishes an occupied loopback port from free."""
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        assert system_checks._loopback_port_probe(port) is PortProbeResult.IN_USE
    finally:
        listener.close()
    assert system_checks._loopback_port_probe(port) is PortProbeResult.FREE


@pytest.mark.unit
def test_loopback_port_probe_ignores_non_loopback_listener() -> None:
    """Verify the loopback probe matches Docker's loopback-only Postgres bind.

    ``docker/compose/local-service.yml`` publishes Postgres bound to loopback
    (``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``), so Docker reserves the
    port on ``127.0.0.1`` only. A listener on a *different* (non-loopback) host
    address does not conflict with that bind, so the loopback probe must report
    the port free -- the all-interface (``0.0.0.0``) probe would wrongly report it
    in-use and block ``awf setup --dry-run`` even though ``awf start`` would
    succeed.
    """
    import socket

    def _non_loopback_ipv4() -> str | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as discover:
                discover.connect(("8.8.8.8", 80))
                address = discover.getsockname()[0]
        except OSError:
            return None
        return address if address and not address.startswith("127.") else None

    host_ip = _non_loopback_ipv4()
    if host_ip is None:
        pytest.skip("no non-loopback IPv4 interface available to bind")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host_ip, 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        assert system_checks._loopback_port_probe(port) is PortProbeResult.FREE
    finally:
        listener.close()


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
def test_default_free_disk_bytes_tolerates_unresolvable_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable ``~user`` work dir must probe parents, not raise RuntimeError."""
    import os.path

    monkeypatch.setattr(os.path, "expanduser", lambda value: value)
    # Falls back to the raw path and walks up to an existing parent (the CWD).
    assert system_checks._default_free_disk_bytes("~olduser/.awf/service") >= 0


@pytest.mark.unit
def test_safe_expanduser_falls_back_on_unresolvable_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_safe_expanduser`` returns the raw path when ``~user`` cannot be resolved."""
    import os.path

    assert system_checks._safe_expanduser("~/awf") == Path("~/awf").expanduser()
    monkeypatch.setattr(os.path, "expanduser", lambda value: value)
    assert system_checks._safe_expanduser("~olduser/.awf/service") == Path("~olduser/.awf/service")


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


# --- Package re-exports ---------------------------------------------------


@pytest.mark.unit
def test_system_checks_public_surface_reexported_from_package() -> None:
    """Verify ``awf.host_setup`` re-exports the full ``system_checks`` surface.

    Consumers that treat ``awf.host_setup`` as the public package (the same way
    config/rendering/source_assets symbols are surfaced) must reach the host
    system-check types and functions without importing the submodule directly.
    """
    import awf.host_setup as host_setup

    for name in system_checks.__all__:
        assert name in host_setup.__all__, f"{name} missing from awf.host_setup.__all__"
        assert getattr(host_setup, name) is getattr(system_checks, name)


@pytest.mark.unit
def test_port_probe_result_is_publicly_exported() -> None:
    """Verify ``PortProbeResult`` is on the public surface like other injectables.

    ``PortProbeResult`` is the return type of the ``PortProbeFn`` callable injected
    into ``check_ports``; callers wiring a custom probe must return correctly-typed
    values. Every other injectable type (``CommandResult``, ``CommandRunner``,
    ``SetupCheckResult``) is exported, so the enum must be too — reachable via both
    ``system_checks`` and the ``awf.host_setup`` package re-export, not direct import only.
    """
    import awf.host_setup as host_setup

    assert "PortProbeResult" in system_checks.__all__
    assert "PortProbeResult" in host_setup.__all__
    assert host_setup.PortProbeResult is system_checks.PortProbeResult


@pytest.mark.unit
def test_injected_callable_aliases_are_publicly_exported() -> None:
    """Verify the injected-dependency callable aliases are on the public surface.

    ``WhichFn``, ``FreeDiskFn``, ``CpuCountFn``, ``MemoryFn`` and ``PortProbeFn``
    are the parameter types of the public ``check_*`` keyword-only dependencies
    (e.g. ``check_disk(free_bytes: FreeDiskFn)``, ``check_gh(which: WhichFn)``,
    ``check_ports(probe: PortProbeFn)``). Callers wiring their own probes for
    testing must be able to annotate them from the public package, exactly like
    ``CommandRunner``/``CommandResult``/``PortProbeResult`` already can be — not by
    reaching into the submodule directly.
    """
    import awf.host_setup as host_setup

    for name in ("WhichFn", "FreeDiskFn", "CpuCountFn", "MemoryFn", "PortProbeFn"):
        assert name in system_checks.__all__, f"{name} missing from system_checks.__all__"
        assert name in host_setup.__all__, f"{name} missing from awf.host_setup.__all__"
        assert getattr(host_setup, name) is getattr(system_checks, name)


def _patch_probes_capture_port(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
) -> None:
    """Patch every probe so ``run_system_checks`` runs in isolation, capturing the port."""

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    def fake_ports(port: int) -> SetupCheckResult:
        captured["port"] = port
        return fake_ok("ports")

    # A real OK docker result carries ``data={"available": True}``; aggregation
    # guards the compose probe on that flag, so the fake must mirror it or compose
    # would be skipped and drop out of the ordered result list.
    monkeypatch.setattr(
        system_checks,
        "check_docker",
        lambda **_kwargs: SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.OK,
            summary="ok",
            detail="ok",
            data={"available": True},
        ),
    )
    monkeypatch.setattr(system_checks, "check_compose", lambda **_kwargs: fake_ok("compose"))
    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", fake_ports)
    monkeypatch.setattr(
        system_checks, "check_postgres_port", lambda _port: fake_ok("postgres_port")
    )
    monkeypatch.setattr(system_checks, "check_disk", lambda _path: fake_ok("disk"))
    monkeypatch.setattr(system_checks, "check_shell_path", lambda: fake_ok("shell_path"))
    monkeypatch.setattr(system_checks, "check_local_capacity", lambda: fake_ok("local_capacity"))


@pytest.mark.unit
def test_run_system_checks_honors_awf_api_host_port_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``AWF_API_HOST_PORT`` override is probed instead of the config default."""
    captured: dict[str, object] = {}
    _patch_probes_capture_port(monkeypatch, captured)

    run_system_checks(
        config=HostSetupConfig(api=ApiConfig(host_port=8000)),
        work_dir=Path("/tmp"),
        environ={"AWF_API_HOST_PORT": "9100"},
    )

    assert captured["port"] == 9100


@pytest.mark.unit
def test_run_system_checks_explicit_port_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``port`` wins over both the env override and config."""
    captured: dict[str, object] = {}
    _patch_probes_capture_port(monkeypatch, captured)

    run_system_checks(
        config=HostSetupConfig(api=ApiConfig(host_port=8000)),
        work_dir=Path("/tmp"),
        port=9999,
        environ={"AWF_API_HOST_PORT": "9100"},
    )

    assert captured["port"] == 9999


@pytest.mark.unit
def test_run_system_checks_falls_back_to_compose_default_when_override_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing or empty ``AWF_API_HOST_PORT`` falls back to Compose's default.

    Compose interpolates ``${AWF_API_HOST_PORT:-8000}`` and ``awf start`` never
    reads the persisted ``config.api.host_port`` — it publishes that Compose
    default from the resolved service env. ``${VAR:-8000}`` substitutes the
    default only when the variable is *unset or empty* (a zero-length string),
    so an absent or genuinely-empty override probes Compose's built-in ``8000``
    rather than blocking. A whitespace-only value is a non-empty literal Compose
    rejects, so it blocks instead — see
    ``test_run_system_checks_blocks_on_whitespace_only_override``. A non-default
    ``config.api.host_port`` is deliberately ignored: probing it would report
    readiness for a port ``awf start`` would never publish.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_port(monkeypatch, captured)

    for blank in (None, ""):
        environ = {} if blank is None else {"AWF_API_HOST_PORT": blank}
        run_system_checks(
            config=HostSetupConfig(api=ApiConfig(host_port=8123)),
            work_dir=Path("/tmp"),
            environ=environ,
        )
        assert captured["port"] == DEFAULT_API_HOST_PORT, repr(blank)


@pytest.mark.unit
def test_run_system_checks_blocks_on_whitespace_only_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only ``AWF_API_HOST_PORT`` blocks; it is not a blank fall-back.

    Docker Compose ``${AWF_API_HOST_PORT:-8000}`` substitutes the ``8000`` default
    only when the variable is *unset or empty* (a zero-length string). A
    whitespace-only value such as ``"   "`` is a non-empty string, so Compose
    interpolates it verbatim into ``"   :8000"`` and ``awf start`` fails to
    publish the port. ``awf service`` settings parse the same override and reject
    it too (``_default_local_service_api_base_url`` reaches ``int("   ")``, which
    raises). The readiness probe must therefore block on it rather than strip it
    to blank and silently probe the default ``8000``, reporting the wrong port as
    free.
    """
    for whitespace in ("   ", "\t", " \t "):
        captured: dict[str, object] = {}
        _patch_probes_capture_port(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(api=ApiConfig(host_port=8123)),
            work_dir=Path("/tmp"),
            environ={"AWF_API_HOST_PORT": whitespace},
        )

        ports = next(result for result in results if result.name == "ports")
        assert ports.level is SetupCheckLevel.BLOCKED, repr(whitespace)
        assert ports.data["env_value"] == whitespace
        # The bind probe must not run for a port the operator never published.
        assert "port" not in captured, repr(whitespace)


@pytest.mark.unit
def test_run_system_checks_blocks_on_padded_api_host_port_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surrounding-whitespace ``AWF_API_HOST_PORT`` blocks; it is not stripped-then-probed.

    The port helper used to ``strip`` the override before parsing, so a padded
    ``" 8000"`` parsed to ``8000``, passed validation, and the readiness probe
    bound and reported port 8000 free. But Compose interpolates
    ``${AWF_API_HOST_PORT:-8000}:8000`` verbatim, producing ``" 8000:8000"`` — an
    invalid port spec ``awf start`` cannot publish. Mirroring the padded work-dir
    guard, the probe must block on the surrounding whitespace instead of probing
    the stripped port and reporting readiness for a port the operator can never
    publish.
    """
    for padded in (" 8000", "8000 ", "\t8000", "8000\n"):
        captured: dict[str, object] = {}
        _patch_probes_capture_port(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(api=ApiConfig(host_port=8123)),
            work_dir=Path("/tmp"),
            environ={"AWF_API_HOST_PORT": padded},
        )

        ports = next(result for result in results if result.name == "ports")
        assert ports.level is SetupCheckLevel.BLOCKED, repr(padded)
        assert ports.data["env_value"] == padded
        # The bind probe must not run for a port the operator never published.
        assert "port" not in captured, repr(padded)


@pytest.mark.unit
def test_run_system_checks_blocks_on_python_only_api_host_port_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Python-only ``AWF_API_HOST_PORT`` spelling blocks; it is not parsed-then-probed.

    ``int()`` accepts underscore grouping (``8_000``) and a leading sign
    (``+8000``), so the parser used to treat them as usable overrides and probe
    the *parsed* port 8000 — letting ``awf setup --dry-run`` pass. But Compose's
    port short syntax is plain decimal, so it interpolates the literal into
    ``${AWF_API_HOST_PORT:-8000}:8000`` (``8_000:8000`` / ``+8000:8000``) and
    ``awf start`` fails to publish it. The probe must reject the non-decimal
    spelling instead of probing the wrong port and reporting it free.
    """
    for spelling in ("8_000", "+8000", "-8000", "0x1f40"):
        captured: dict[str, object] = {}
        _patch_probes_capture_port(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(api=ApiConfig(host_port=8123)),
            work_dir=Path("/tmp"),
            environ={"AWF_API_HOST_PORT": spelling},
        )

        ports = next(result for result in results if result.name == "ports")
        assert ports.level is SetupCheckLevel.BLOCKED, repr(spelling)
        assert ports.data["env_value"] == spelling
        # The bind probe must not run for a port the operator never published.
        assert "port" not in captured, repr(spelling)


@pytest.mark.unit
def test_run_system_checks_blocks_on_set_but_invalid_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty but unusable ``AWF_API_HOST_PORT`` blocks instead of probing.

    Compose publishes ``${AWF_API_HOST_PORT:-8000}:8000`` and ``awf service``
    settings parse the same override, so a malformed or out-of-range value makes
    ``awf start`` fail to publish the port. The readiness probe surfaces it as a
    startup blocker rather than silently probing the default port and reporting
    the wrong port as free.
    """
    for invalid in ("not-a-port", "0", "70000"):
        captured: dict[str, object] = {}
        _patch_probes_capture_port(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(api=ApiConfig(host_port=8123)),
            work_dir=Path("/tmp"),
            environ={"AWF_API_HOST_PORT": invalid},
        )

        ports = next(result for result in results if result.name == "ports")
        assert ports.level is SetupCheckLevel.BLOCKED, repr(invalid)
        assert ports.data["env_value"] == invalid
        # The bind probe must not run for a port the operator never published.
        assert "port" not in captured, repr(invalid)


@pytest.mark.unit
def test_run_system_checks_explicit_port_suppresses_invalid_override_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``port`` wins over an invalid env override without blocking."""
    captured: dict[str, object] = {}
    _patch_probes_capture_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(api=ApiConfig(host_port=8000)),
        work_dir=Path("/tmp"),
        port=9999,
        environ={"AWF_API_HOST_PORT": "not-a-port"},
    )

    ports = next(result for result in results if result.name == "ports")
    assert ports.level is SetupCheckLevel.OK
    assert captured["port"] == 9999


@pytest.mark.unit
def test_check_api_host_port_override_blocks_with_value_in_data() -> None:
    """The override check is a hard blocker carrying the offending value."""
    result = system_checks.check_api_host_port_override("abc")

    assert result.name == "ports"
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["env_value"] == "abc"
    assert result.data["available"] is False
    assert "abc" in result.summary
    assert result.fix is not None


# --- Postgres host port --------------------------------------------------


def _patch_probes_capture_postgres_port(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
) -> None:
    """Patch every probe so ``run_system_checks`` runs in isolation, capturing the pg port."""

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    def fake_postgres_port(port: int) -> SetupCheckResult:
        captured["postgres_port"] = port
        return fake_ok("postgres_port")

    # A real OK docker result carries ``data={"available": True}``; aggregation
    # guards the compose probe on that flag, so the fake must mirror it or compose
    # would be skipped and drop out of the ordered result list.
    monkeypatch.setattr(
        system_checks,
        "check_docker",
        lambda **_kwargs: SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.OK,
            summary="ok",
            detail="ok",
            data={"available": True},
        ),
    )
    monkeypatch.setattr(system_checks, "check_compose", lambda **_kwargs: fake_ok("compose"))
    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))
    monkeypatch.setattr(system_checks, "check_postgres_port", fake_postgres_port)
    monkeypatch.setattr(system_checks, "check_disk", lambda _path: fake_ok("disk"))
    monkeypatch.setattr(system_checks, "check_shell_path", lambda: fake_ok("shell_path"))
    monkeypatch.setattr(system_checks, "check_local_capacity", lambda: fake_ok("local_capacity"))


@pytest.mark.unit
def test_check_postgres_port_ok_when_free_and_blocks_when_in_use() -> None:
    """Verify a free pg port is OK and an in-use pg port BLOCKS with the port in data.

    The local-service Compose stack brings ``postgres`` up first and publishes it
    on a fixed host port (``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``) with
    no auto-fallback, so an occupied port makes ``awf start`` fail to publish it.
    An occupied Postgres host port is therefore a readiness blocker, not advisory.
    """
    free = system_checks.check_postgres_port(5433, probe=lambda _port: PortProbeResult.FREE)
    in_use = system_checks.check_postgres_port(5433, probe=lambda _port: PortProbeResult.IN_USE)
    assert free.name == "postgres_port"
    assert free.level is SetupCheckLevel.OK
    assert free.data["available"] is True
    assert in_use.name == "postgres_port"
    assert in_use.level is SetupCheckLevel.BLOCKED
    assert in_use.data["port"] == 5433
    assert in_use.data["available"] is False
    assert in_use.fix is not None
    # Compose publishes Postgres on loopback only, so the operator-facing detail
    # must describe a loopback bind, not an all-interface (0.0.0.0) bind.
    assert "127.0.0.1" in in_use.detail
    assert "0.0.0.0" not in in_use.detail


@pytest.mark.unit
def test_check_postgres_port_default_probe_is_loopback() -> None:
    """Verify the Postgres check defaults to the loopback probe.

    Compose publishes Postgres as ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``
    (loopback only), so readiness must probe the loopback bind Docker will reserve
    rather than the all-interface bind used for the API port.
    """
    import inspect

    default_probe = inspect.signature(system_checks.check_postgres_port).parameters["probe"].default
    assert default_probe is system_checks._loopback_port_probe


@pytest.mark.unit
def test_check_postgres_port_distinguishes_permission_and_other_bind_errors() -> None:
    """Verify non-occupancy bind failures get their own cause/fix, not "in use"."""
    permission = system_checks.check_postgres_port(
        80, probe=lambda _port: PortProbeResult.PERMISSION_DENIED
    )
    other = system_checks.check_postgres_port(5433, probe=lambda _port: PortProbeResult.UNAVAILABLE)

    assert permission.name == "postgres_port"
    assert permission.level is SetupCheckLevel.WARNING
    assert permission.data["probe"] == PortProbeResult.PERMISSION_DENIED.value
    assert "permission" in permission.summary.lower()
    assert "already in use" not in permission.summary
    assert permission.fix is not None
    assert "Free the port" not in permission.fix

    assert other.name == "postgres_port"
    assert other.level is SetupCheckLevel.WARNING
    assert other.data["probe"] == PortProbeResult.UNAVAILABLE.value
    assert "already in use" not in other.summary
    assert other.fix is not None
    assert "Free the port" not in other.fix


@pytest.mark.unit
def test_check_postgres_host_port_override_blocks_with_value_in_data() -> None:
    """The pg override check is a hard blocker carrying the offending value."""
    result = system_checks.check_postgres_host_port_override("abc")

    assert result.name == "postgres_port"
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["env_value"] == "abc"
    assert result.data["available"] is False
    assert "abc" in result.summary
    assert result.fix is not None


@pytest.mark.unit
def test_run_system_checks_probes_postgres_default_host_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default Postgres host port 5433 in use yields a non-OK ``postgres_port`` result.

    The local-service Compose stack publishes Postgres as
    ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432`` and bootstrap brings it up
    first, so an occupied 5433 makes ``awf start`` fail. ``run_system_checks`` must
    surface this as a blocker, not silently report success.
    """

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    captured: dict[str, object] = {}

    def fake_postgres_port(port: int) -> SetupCheckResult:
        captured["postgres_port"] = port
        return SetupCheckResult(
            name="postgres_port",
            level=SetupCheckLevel.BLOCKED,
            summary="pg in use",
            detail="pg in use",
            fix="free it",
            data={"port": port, "available": False},
        )

    for name in (
        "check_docker",
        "check_compose",
        "check_git",
        "check_gh",
        "check_python_runtime",
        "check_shell_path",
        "check_local_capacity",
    ):
        monkeypatch.setattr(system_checks, name, lambda name=name, **_kwargs: fake_ok(name))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))
    monkeypatch.setattr(system_checks, "check_disk", lambda _path: fake_ok("disk"))
    monkeypatch.setattr(system_checks, "check_postgres_port", fake_postgres_port)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={},
    )

    assert captured["postgres_port"] == system_checks.DEFAULT_POSTGRES_HOST_PORT
    postgres = next(result for result in results if result.name == "postgres_port")
    assert postgres.level is SetupCheckLevel.BLOCKED
    assert postgres.data["port"] == 5433


@pytest.mark.unit
def test_run_system_checks_honors_awf_postgres_host_port_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``AWF_POSTGRES_HOST_PORT`` override is probed instead of the default 5433."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"AWF_POSTGRES_HOST_PORT": "6543"},
    )

    assert captured["postgres_port"] == 6543


@pytest.mark.unit
def test_run_system_checks_blocks_on_set_but_invalid_postgres_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty but unusable ``AWF_POSTGRES_HOST_PORT`` blocks instead of probing."""
    for invalid in ("abc", "0", "70000"):
        captured: dict[str, object] = {}
        _patch_probes_capture_postgres_port(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(),
            work_dir=Path("/tmp"),
            environ={"AWF_POSTGRES_HOST_PORT": invalid},
        )

        postgres = next(result for result in results if result.name == "postgres_port")
        assert postgres.level is SetupCheckLevel.BLOCKED, repr(invalid)
        assert postgres.data["env_value"] == invalid
        # The bind probe must not run for a port the operator never published.
        assert "postgres_port" not in captured, repr(invalid)


@pytest.mark.unit
def test_run_system_checks_blocks_on_padded_postgres_host_port_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surrounding-whitespace ``AWF_POSTGRES_HOST_PORT`` blocks; it is not stripped-then-probed.

    Mirrors ``test_run_system_checks_blocks_on_padded_api_host_port_override`` for
    Postgres. Compose interpolates ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``
    verbatim, so a padded ``" 5433"`` becomes ``127.0.0.1: 5433:5432`` — an invalid
    port spec ``awf start`` cannot publish. The probe must block on the surrounding
    whitespace rather than strip it and report the wrong port as free.
    """
    for padded in (" 5433", "5433 ", "\t5433", "5433\n"):
        captured: dict[str, object] = {}
        _patch_probes_capture_postgres_port(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(),
            work_dir=Path("/tmp"),
            environ={"AWF_POSTGRES_HOST_PORT": padded},
        )

        postgres = next(result for result in results if result.name == "postgres_port")
        assert postgres.level is SetupCheckLevel.BLOCKED, repr(padded)
        assert postgres.data["env_value"] == padded
        # The bind probe must not run for a port the operator never published.
        assert "postgres_port" not in captured, repr(padded)


@pytest.mark.unit
def test_run_system_checks_blocks_on_python_only_postgres_host_port_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Python-only ``AWF_POSTGRES_HOST_PORT`` spelling blocks; not parsed-then-probed.

    Mirrors ``test_run_system_checks_blocks_on_python_only_api_host_port_spelling``
    for Postgres. ``int()`` accepts ``5_433`` and ``+5433``, but Compose's plain
    decimal port syntax interpolates the literal into
    ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432`` so ``awf start`` fails to
    publish it. The probe must reject the non-decimal spelling rather than probe
    the parsed port and report it free.
    """
    for spelling in ("5_433", "+5433", "-5433", "0x1531"):
        captured: dict[str, object] = {}
        _patch_probes_capture_postgres_port(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(),
            work_dir=Path("/tmp"),
            environ={"AWF_POSTGRES_HOST_PORT": spelling},
        )

        postgres = next(result for result in results if result.name == "postgres_port")
        assert postgres.level is SetupCheckLevel.BLOCKED, repr(spelling)
        assert postgres.data["env_value"] == spelling
        # The bind probe must not run for a port the operator never published.
        assert "postgres_port" not in captured, repr(spelling)


@pytest.mark.unit
def test_run_system_checks_falls_back_to_postgres_default_when_override_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing or empty ``AWF_POSTGRES_HOST_PORT`` falls back to Compose's 5433 default."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    for blank in (None, ""):
        environ = {} if blank is None else {"AWF_POSTGRES_HOST_PORT": blank}
        run_system_checks(
            config=HostSetupConfig(),
            work_dir=Path("/tmp"),
            environ=environ,
        )
        assert captured["postgres_port"] == system_checks.DEFAULT_POSTGRES_HOST_PORT, repr(blank)


# --- API/Postgres host port collision ------------------------------------


@pytest.mark.unit
def test_check_host_port_conflict_blocks_when_ports_equal() -> None:
    """A shared API/Postgres host port is a hard blocker carrying both ports.

    The local-service Compose stack publishes the API on every interface
    (``${AWF_API_HOST_PORT:-8000}:8000`` -> ``0.0.0.0``) and Postgres on loopback
    (``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``). ``check_ports`` and
    ``check_postgres_port`` bind and release each port independently, so both pass
    in isolation when the two resolve to the same value, yet ``awf start`` asks
    Docker to reserve both at once and a wildcard 0.0.0.0 reservation conflicts
    with a 127.0.0.1 reservation on the same port. The cross-check must block.
    """
    result = system_checks.check_host_port_conflict(5433, 5433)

    assert result is not None
    assert result.name == "port_conflict"
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["api_port"] == 5433
    assert result.data["postgres_port"] == 5433
    assert "5433" in result.summary
    assert result.fix is not None
    assert "AWF_API_HOST_PORT" in result.fix
    assert "AWF_POSTGRES_HOST_PORT" in result.fix


@pytest.mark.unit
def test_check_host_port_conflict_passes_when_ports_differ() -> None:
    """Distinct API/Postgres host ports add no readiness line (the common case)."""
    assert system_checks.check_host_port_conflict(8000, 5433) is None


@pytest.mark.unit
def test_run_system_checks_blocks_when_api_and_postgres_host_ports_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both single-port probes pass yet the cross-check blocks on a shared port.

    Setting ``AWF_API_HOST_PORT`` to the default Postgres port (5433) makes both
    services publish the same host port, which ``awf start`` cannot reserve. The
    per-port probes each report FREE in isolation, so only the cross-check catches
    the collision; ``run_system_checks`` must surface it as a blocker.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"AWF_API_HOST_PORT": "5433"},
    )

    conflict = next(result for result in results if result.name == "port_conflict")
    assert conflict.level is SetupCheckLevel.BLOCKED
    assert conflict.data["api_port"] == 5433
    assert conflict.data["postgres_port"] == 5433
    # The cross-check is additive: the standalone port probes still run.
    assert any(result.name == "ports" for result in results)
    assert any(result.name == "postgres_port" for result in results)
    # It sits with the other port checks, before disk.
    names = [result.name for result in results]
    assert names.index("port_conflict") == names.index("postgres_port") + 1
    assert names.index("port_conflict") < names.index("disk")


@pytest.mark.unit
def test_run_system_checks_omits_port_conflict_when_ports_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct API/Postgres host ports add no ``port_conflict`` result."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={},
    )

    assert all(result.name != "port_conflict" for result in results)


@pytest.mark.unit
def test_run_system_checks_skips_port_conflict_when_an_override_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid port override blocks on its own; the cross-check is skipped.

    When ``AWF_API_HOST_PORT`` cannot be parsed there is no resolved API port to
    compare against Postgres, so the collision cross-check must not run (and must
    not crash) -- the override blocker already wedges readiness, and the operator
    must fix the malformed value before any collision is meaningful.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"AWF_API_HOST_PORT": "abc", "AWF_POSTGRES_HOST_PORT": "5433"},
    )

    assert all(result.name != "port_conflict" for result in results)
    ports = next(result for result in results if result.name == "ports")
    assert ports.level is SetupCheckLevel.BLOCKED


@pytest.mark.unit
def test_check_ollama_bridge_api_port_conflict_blocks_when_ports_equal() -> None:
    """A shared API/ollama-bridge host port is a hard blocker carrying both ports.

    The local-service Compose stack publishes the API on every interface
    (``${AWF_API_HOST_PORT:-8000}:8000`` -> ``0.0.0.0``) and, with the
    ``ollama-bridge`` profile on, runs a host-networking socat that binds
    ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``
    *before* the API is published. A wildcard 0.0.0.0 reservation overlaps every
    specific address on the same port, so a shared port makes ``awf start`` fail
    even though the isolated single-port probes each pass. The cross-check blocks.
    """
    result = system_checks.check_ollama_bridge_api_port_conflict(8000, 8000)

    assert result is not None
    assert result.name == "ollama_bridge_port_conflict"
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["api_port"] == 8000
    assert result.data["ollama_bridge_listen_port"] == 8000
    assert "8000" in result.summary
    assert result.fix is not None
    assert "AWF_API_HOST_PORT" in result.fix
    assert "AWF_OLLAMA_BRIDGE_LISTEN_PORT" in result.fix


@pytest.mark.unit
def test_check_ollama_bridge_api_port_conflict_passes_when_ports_differ() -> None:
    """Distinct API/ollama-bridge host ports add no readiness line (the common case)."""
    assert system_checks.check_ollama_bridge_api_port_conflict(8000, 11434) is None


@pytest.mark.unit
def test_run_system_checks_blocks_when_api_and_ollama_bridge_ports_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the bridge profile on, a shared API/bridge port surfaces as a blocker.

    Setting ``AWF_OLLAMA_BRIDGE_LISTEN_PORT`` to the default API host port (8000)
    makes socat and the API publish claim the same host port. The bridge comes up
    first, so ``awf start`` cannot publish the API; only the cross-check catches
    it (the per-port probes each report FREE in isolation).
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "8000"},
    )

    conflict = next(result for result in results if result.name == "ollama_bridge_port_conflict")
    assert conflict.level is SetupCheckLevel.BLOCKED
    assert conflict.data["api_port"] == 8000
    assert conflict.data["ollama_bridge_listen_port"] == 8000
    # The cross-check is additive: the standalone probes still run.
    assert any(result.name == "ports" for result in results)
    assert any(result.name == "ollama_bridge_port" for result in results)
    # It sits with the other port checks, before disk.
    names = [result.name for result in results]
    assert names.index("ollama_bridge_port_conflict") < names.index("disk")
    assert names.index("ollama_bridge_port_conflict") > names.index("postgres_port")


@pytest.mark.unit
def test_run_system_checks_omits_ollama_bridge_port_conflict_when_profile_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge port equal to the API port is harmless when the profile is off.

    ``awf start`` never appends the bridge stage with the profile disabled, so
    there is no socat bind to collide with the API publish and no extra readiness
    line is emitted even when the ports would match.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"AWF_OLLAMA_BRIDGE_LISTEN_PORT": "8000"},
    )

    assert all(result.name != "ollama_bridge_port_conflict" for result in results)


@pytest.mark.unit
def test_run_system_checks_omits_ollama_bridge_port_conflict_when_ports_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active bridge profile on its default port adds no conflict line."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge"},
    )

    assert all(result.name != "ollama_bridge_port_conflict" for result in results)


@pytest.mark.unit
def test_run_system_checks_skips_ollama_bridge_port_conflict_when_api_override_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid API port override blocks on its own; the bridge cross-check is skipped.

    When ``AWF_API_HOST_PORT`` cannot be parsed there is no resolved API port to
    compare against the bridge listen port, so the collision cross-check must not
    run (and must not crash) -- the override blocker already wedges readiness.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={
            "COMPOSE_PROFILES": "ollama-bridge",
            "AWF_API_HOST_PORT": "abc",
            "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "8000",
        },
    )

    assert all(result.name != "ollama_bridge_port_conflict" for result in results)
    ports = next(result for result in results if result.name == "ports")
    assert ports.level is SetupCheckLevel.BLOCKED


@pytest.mark.unit
def test_run_system_checks_skips_ollama_bridge_port_conflict_when_bridge_override_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed bridge listen port blocks on its own; the cross-check is skipped.

    When ``AWF_OLLAMA_BRIDGE_LISTEN_PORT`` cannot be parsed there is no resolved
    bridge port to compare, so the collision cross-check must not run -- the
    listen-port blocker already fires.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "abc"},
    )

    assert all(result.name != "ollama_bridge_port_conflict" for result in results)
    port = next(result for result in results if result.name == "ollama_bridge_port")
    assert port.level is SetupCheckLevel.BLOCKED
    assert port.data["env_value"] == "abc"


def _patch_probes_capture_disk_path(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
) -> None:
    """Patch every probe so ``run_system_checks`` runs in isolation, capturing the disk path."""

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    def fake_disk(path: Path) -> SetupCheckResult:
        captured["disk_path"] = path
        return fake_ok("disk")

    # A real OK docker result carries ``data={"available": True}``; aggregation
    # guards the compose probe on that flag, so the fake must mirror it or compose
    # would be skipped and drop out of the ordered result list.
    monkeypatch.setattr(
        system_checks,
        "check_docker",
        lambda **_kwargs: SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.OK,
            summary="ok",
            detail="ok",
            data={"available": True},
        ),
    )
    monkeypatch.setattr(system_checks, "check_compose", lambda **_kwargs: fake_ok("compose"))
    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))
    monkeypatch.setattr(
        system_checks, "check_postgres_port", lambda _port: fake_ok("postgres_port")
    )
    monkeypatch.setattr(system_checks, "check_disk", fake_disk)
    monkeypatch.setattr(system_checks, "check_shell_path", lambda: fake_ok("shell_path"))
    monkeypatch.setattr(system_checks, "check_local_capacity", lambda: fake_ok("local_capacity"))


@pytest.mark.unit
def test_run_system_checks_honors_awf_host_work_dir_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``AWF_HOST_WORK_DIR`` override is inspected instead of the config default.

    The local-service Compose stack bind-mounts ``${AWF_HOST_WORK_DIR:-...}`` and
    the running service resolves the same override as its work_dir, so the disk
    readiness probe must report on that directory, not the saved ``config.work_dir``.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    run_system_checks(
        config=HostSetupConfig(work_dir="/persisted/state"),
        environ={"AWF_HOST_WORK_DIR": "/custom/state"},
    )

    assert captured["disk_path"] == Path("/custom/state")


@pytest.mark.unit
def test_run_system_checks_blocks_on_non_absolute_work_dir_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative or ``~``-prefixed ``AWF_HOST_WORK_DIR`` blocks; it is not probed.

    The local-service Compose file uses ``${AWF_HOST_WORK_DIR}`` as *both* the
    bind source and the mount target (``docker/compose/local-service.yml``), and
    Docker's mount target must be an absolute path. Neither Compose nor ``awf
    service``'s ``_resolve_service_work_dir`` expands a leading ``~`` or resolves
    a relative path, so a value such as ``data/awf`` or ``~/.awf/service`` is
    mounted verbatim and ``awf start`` fails — even though the readiness probe
    could expand ``~`` or read the relative path against the current process. The
    probe must block on it instead of reporting readiness for a directory that is
    never mounted.

    (The old behavior expanded ``~`` for the disk probe; that hid this exact
    divergence, so the readiness check now blocks non-absolute overrides the same
    way it already blocks whitespace-only and padded ones.)
    """
    for non_absolute in ("data/awf", "./data/awf", "~/.awf/service", "~op/.awf/service"):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": "/home/op", "AWF_HOST_WORK_DIR": non_absolute},
        )

        disk = next(result for result in results if result.name == "disk")
        assert disk.level is SetupCheckLevel.BLOCKED, repr(non_absolute)
        assert disk.data["env_value"] == non_absolute
        # The disk probe must not run for a path the operator never mounted.
        assert "disk_path" not in captured, repr(non_absolute)


@pytest.mark.unit
def test_run_system_checks_explicit_work_dir_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``work_dir`` wins over both the env override and config."""
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    run_system_checks(
        config=HostSetupConfig(work_dir="/persisted/state"),
        work_dir=Path("/explicit/state"),
        environ={"AWF_HOST_WORK_DIR": "/custom/state"},
    )

    assert captured["disk_path"] == Path("/explicit/state")


@pytest.mark.unit
def test_run_system_checks_falls_back_to_compose_default_work_dir_when_override_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing or empty ``AWF_HOST_WORK_DIR`` falls back to Compose's default.

    The local-service Compose stack bind-mounts
    ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` and ``awf start`` never reads
    the persisted ``config.work_dir`` — it resolves the bind from the Compose
    env. ``${VAR:-default}`` substitutes the default only when the variable is
    *unset or empty* (a zero-length string), so an absent or genuinely-empty
    override probes Compose's built-in ``${HOME}/.awf/service`` default. A
    whitespace-only value is a non-empty literal Compose keeps verbatim, so it
    blocks instead — see
    ``test_run_system_checks_blocks_on_whitespace_only_work_dir_override``. A
    non-default ``config.work_dir`` is deliberately ignored: probing it would
    report disk readiness for a directory ``awf start`` would never mount.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    for blank in (None, ""):
        environ = {"HOME": "/home/op"}
        if blank is not None:
            environ["AWF_HOST_WORK_DIR"] = blank
        run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ=environ,
        )
        assert captured["disk_path"] == Path("/home/op/.awf/service"), repr(blank)


@pytest.mark.unit
def test_run_system_checks_blocks_on_whitespace_only_work_dir_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only ``AWF_HOST_WORK_DIR`` blocks; it is not a blank fall-back.

    ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` substitutes the default only
    when the variable is *unset or empty* (a zero-length string). A
    whitespace-only value such as ``"   "`` is a non-empty string, so Compose
    interpolates it verbatim into the bind source/target and ``awf service``
    resolves the same override as its work_dir, so ``awf start`` mounts (or
    fails on) that path instead of the default. The readiness probe must
    therefore block on it rather than strip it to blank and silently probe the
    ``${HOME}/.awf/service`` default, reporting readiness for the wrong
    directory.
    """
    for whitespace in ("   ", "\t", " \t "):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": "/home/op", "AWF_HOST_WORK_DIR": whitespace},
        )

        disk = next(result for result in results if result.name == "disk")
        assert disk.level is SetupCheckLevel.BLOCKED, repr(whitespace)
        assert disk.data["env_value"] == whitespace
        # The disk probe must not run for a work dir the operator never mounted.
        assert "disk_path" not in captured, repr(whitespace)


@pytest.mark.unit
def test_run_system_checks_blocks_on_padded_work_dir_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surrounding-whitespace ``AWF_HOST_WORK_DIR`` blocks; it is not stripped-then-probed.

    The disk probe used to ``strip`` the override before inspecting it, but
    Compose interpolates ``${AWF_HOST_WORK_DIR}`` verbatim and ``awf service``'s
    ``_resolve_service_work_dir`` returns the override *unstripped*. A padded
    value such as ``" /data/awf"`` would therefore pass disk readiness for the
    stripped ``/data/awf`` while ``awf start`` mounts (and the service resolves)
    the spaced path — reporting readiness for a directory that is never mounted.
    The readiness probe must block on the surrounding whitespace instead of
    silently probing the stripped path.
    """
    for padded in (" /data/awf", "/data/awf ", "\t/data/awf", "/data/awf\n"):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": "/home/op", "AWF_HOST_WORK_DIR": padded},
        )

        disk = next(result for result in results if result.name == "disk")
        assert disk.level is SetupCheckLevel.BLOCKED, repr(padded)
        assert disk.data["env_value"] == padded
        # The disk probe must not run for a stripped path the operator never mounted.
        assert "disk_path" not in captured, repr(padded)


@pytest.mark.unit
def test_run_system_checks_explicit_work_dir_suppresses_whitespace_override_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``work_dir`` wins over a whitespace env override without blocking."""
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(work_dir="/persisted/state"),
        work_dir=Path("/explicit/state"),
        environ={"AWF_HOST_WORK_DIR": "   "},
    )

    disk = next(result for result in results if result.name == "disk")
    assert disk.level is SetupCheckLevel.OK
    assert captured["disk_path"] == Path("/explicit/state")


@pytest.mark.unit
def test_run_system_checks_blocks_on_unset_home_work_dir_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset ``HOME`` blocks the work-dir default; Compose anchors it at ``/``.

    With ``AWF_HOST_WORK_DIR`` unset the local-service Compose stack binds
    ``${HOME}/.awf/service``, and ``${HOME}`` itself has no ``:-`` default — an
    unset ``HOME`` interpolates to nothing, so Compose binds ``/.awf/service`` (the
    filesystem root) while the readiness probe would expand ``~`` to the account
    home. The probe must block instead of reporting disk readiness for a directory
    ``awf start`` never mounts. ``AWF_HOST_HOME`` is pinned absolute so only the
    work-dir fallback is exercised.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(work_dir="/persisted/state"),
        environ={"AWF_HOST_HOME": "/home/op"},
    )

    disk = next(result for result in results if result.name == "disk")
    assert disk.level is SetupCheckLevel.BLOCKED
    assert disk.data["env_value"] == ""
    assert "unset or empty" in disk.summary
    # The disk probe must not run for a path awf start never mounts.
    assert "disk_path" not in captured


# --- AWF_HOST_HOME override validation ------------------------------------


@pytest.mark.unit
def test_run_system_checks_blocks_on_non_absolute_host_home_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative or ``~``-prefixed ``AWF_HOST_HOME`` blocks readiness.

    The local-service Compose file uses ``${AWF_HOST_HOME:-${HOME}}`` as *both*
    the host source and the container target for every auth mount (for example
    ``${AWF_HOST_HOME:-${HOME}}/.config/gh:${AWF_HOST_HOME:-${HOME}}/.config/gh:ro``),
    and Docker's mount target must be an absolute path. Compose does not expand a
    leading ``~`` or resolve a relative path, so a value such as ``home/op`` or
    ``~`` is mounted verbatim and ``awf start`` fails — even though the readiness
    probe could resolve it. The probe must block on it instead of declaring the
    machine ready.
    """
    for non_absolute in ("home/op", "./home/op", "~", "~/home", "~op/home"):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": "/home/op", "AWF_HOST_HOME": non_absolute},
        )

        host_home = next(result for result in results if result.name == "host_home")
        assert host_home.level is SetupCheckLevel.BLOCKED, repr(non_absolute)
        assert host_home.data["env_value"] == non_absolute
        assert "absolute" in host_home.summary


@pytest.mark.unit
def test_run_system_checks_blocks_on_whitespace_only_host_home_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only ``AWF_HOST_HOME`` blocks; it is not a blank fall-back.

    ``${AWF_HOST_HOME:-${HOME}}`` substitutes the ``${HOME}`` default only when
    the variable is *unset or empty* (a zero-length string). A whitespace-only
    value such as ``"   "`` is a non-empty string, so Compose interpolates it
    verbatim into the auth mounts and ``awf start`` mounts (or fails on) that path
    instead of the default. The readiness probe must block on it rather than strip
    it to blank and report the machine ready.
    """
    for whitespace in ("   ", "\t", " \t "):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": "/home/op", "AWF_HOST_HOME": whitespace},
        )

        host_home = next(result for result in results if result.name == "host_home")
        assert host_home.level is SetupCheckLevel.BLOCKED, repr(whitespace)
        assert host_home.data["env_value"] == whitespace
        assert "whitespace" in host_home.summary


@pytest.mark.unit
def test_run_system_checks_blocks_on_padded_host_home_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surrounding-whitespace ``AWF_HOST_HOME`` blocks; it is not stripped-then-passed.

    Compose interpolates ``${AWF_HOST_HOME}`` verbatim, so a padded value such as
    ``" /home/op"`` reaches Docker with its surrounding whitespace and ``awf
    start`` mounts (or fails on) the spaced path. The readiness probe must block
    on the surrounding whitespace instead of silently reporting readiness for the
    stripped path.
    """
    for padded in (" /home/op", "/home/op ", "\t/home/op", "/home/op\n"):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": "/home/op", "AWF_HOST_HOME": padded},
        )

        host_home = next(result for result in results if result.name == "host_home")
        assert host_home.level is SetupCheckLevel.BLOCKED, repr(padded)
        assert host_home.data["env_value"] == padded
        assert "whitespace" in host_home.summary


@pytest.mark.unit
def test_run_system_checks_host_home_ok_when_absolute_or_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absolute, unset, or empty ``AWF_HOST_HOME`` passes readiness.

    ``${AWF_HOST_HOME:-${HOME}}`` mounts an absolute override verbatim (usable) and
    falls back to ``${HOME}`` when the variable is unset or empty, so every auth
    mount resolves to an absolute target ``awf start`` can bind.
    """
    for value in ("/home/op", "/Users/op", None, ""):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        environ = {"HOME": "/home/op"}
        if value is not None:
            environ["AWF_HOST_HOME"] = value
        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ=environ,
        )

        host_home = next(result for result in results if result.name == "host_home")
        assert host_home.level is SetupCheckLevel.OK, repr(value)
        # The OK result records the concrete auth-mount root that was validated,
        # resolved as ${AWF_HOST_HOME:-${HOME}} from the same env the upstream
        # guards consult -- an absolute override wins, unset/empty falls back to
        # ${HOME} -- so JSON consumers see which root was confirmed ready.
        expected_root = value if value else "/home/op"
        assert host_home.data["resolved_root"] == expected_root, repr(value)
        assert host_home.data["env_value"] == value, repr(value)
        assert host_home.data["home"] == "/home/op", repr(value)


@pytest.mark.unit
def test_check_host_home_ok_records_resolved_auth_mount_root() -> None:
    """The OK result echoes the resolved ``${AWF_HOST_HOME:-${HOME}}`` root.

    Every other ``check_*`` OK result populates ``data`` with the value it
    validated; the auth-mount root must come from the same ``environ`` the
    upstream guards (``_invalid_host_home_override`` /
    ``_invalid_auth_mount_home_fallback``) consult -- the resolved service env --
    not the bare process env, or the reported root would diverge from the one the
    block/OK decision was actually made against.
    """
    override = system_checks.check_host_home(
        environ={"AWF_HOST_HOME": "/mnt/auth", "HOME": "/home/op"}
    )
    assert override.name == "host_home"
    assert override.level is SetupCheckLevel.OK
    assert override.data == {
        "env_value": "/mnt/auth",
        "home": "/home/op",
        "resolved_root": "/mnt/auth",
    }

    fallback = system_checks.check_host_home(environ={"HOME": "/home/op"})
    assert fallback.level is SetupCheckLevel.OK
    assert fallback.data == {
        "env_value": None,
        "home": "/home/op",
        "resolved_root": "/home/op",
    }

    # An empty override falls back to ${HOME}, exactly as the guards decide which
    # value they validated, so the resolved root is the HOME path, not the empty
    # override string.
    empty_override = system_checks.check_host_home(
        environ={"AWF_HOST_HOME": "", "HOME": "/home/op"}
    )
    assert empty_override.data["env_value"] == ""
    assert empty_override.data["resolved_root"] == "/home/op"


@pytest.mark.unit
def test_check_host_home_ok_text_names_validated_root() -> None:
    """The OK summary/detail describe the case that actually applies.

    Regression for PRRT_kwDOSJAM6s6F8PSF: with a set ``AWF_HOST_HOME`` the OK
    detail must not still claim the override is unset, or pretty/JSON readiness
    output misleads operators about which auth-mount root was validated. A set,
    absolute override is the auth-mount root verbatim; an unset/empty override
    falls back to ``${HOME}``.
    """
    override = system_checks.check_host_home(
        environ={"AWF_HOST_HOME": "/mnt/auth", "HOME": "/home/op"}
    )
    assert override.level is SetupCheckLevel.OK
    # The set override is named as the validated root, and the text never claims
    # it is unset.
    assert "/mnt/auth" in override.summary
    assert "/mnt/auth" in override.detail
    assert "unset" not in override.summary
    assert "unset" not in override.detail
    # Regression for PRRT_kwDOSJAM6s6F8vPe: every auth mount example must resolve
    # under the validated root, not the filesystem root. Both the gh config and
    # the ssh mount are anchored at the override.
    assert "/mnt/auth/.config/gh" in override.detail
    assert "/mnt/auth/.ssh" in override.detail

    fallback = system_checks.check_host_home(environ={"HOME": "/home/op"})
    assert fallback.level is SetupCheckLevel.OK
    # An unset override falls back to ${HOME}; name that as the validated root.
    assert "unset" in fallback.detail
    assert "/home/op" in fallback.detail
    # The ssh mount is anchored at ${HOME} too, not the filesystem root.
    assert "/home/op/.config/gh" in fallback.detail
    assert "/home/op/.ssh" in fallback.detail

    empty_override = system_checks.check_host_home(
        environ={"AWF_HOST_HOME": "", "HOME": "/home/op"}
    )
    assert empty_override.level is SetupCheckLevel.OK
    # An empty override also falls back to ${HOME}: describe the fallback case and
    # name ${HOME}, never echo the empty override as the root.
    assert "unset" in empty_override.detail
    assert "/home/op" in empty_override.detail


@pytest.mark.unit
def test_check_host_home_override_blocks_with_value_in_data() -> None:
    """``check_host_home_override`` distinguishes non-absolute vs. padded values.

    Both branches BLOCK and echo the raw ``AWF_HOST_HOME`` in ``data`` so the
    readiness payload can name the offending value, and the summary names the
    specific defect (absoluteness vs. surrounding whitespace).
    """
    non_absolute = system_checks.check_host_home_override("~/home")
    assert non_absolute.name == "host_home"
    assert non_absolute.level is SetupCheckLevel.BLOCKED
    assert non_absolute.data["env_value"] == "~/home"
    assert "absolute" in non_absolute.summary

    padded = system_checks.check_host_home_override(" /home/op")
    assert padded.level is SetupCheckLevel.BLOCKED
    assert padded.data["env_value"] == " /home/op"
    assert "whitespace" in padded.summary


# --- Required local-service Compose env -----------------------------------


@pytest.mark.unit
def test_check_required_service_env_ok_when_both_present_without_leaking_values() -> None:
    """Both mandatory Compose vars set reports OK and never echoes their values.

    The local-service stack interpolates AWF_API_TOKEN / AWF_POSTGRES_PASSWORD via
    ``${VAR:?...}``, so a non-empty pair means ``docker compose`` can start. The OK
    result is a non-secret presence fact: it records the variable *names* but must
    never surface the secret values it read.
    """
    api_token = "set-api-token-value"
    pg_password = "set-pg-password-value"
    result = system_checks.check_required_service_env(
        environ={"AWF_API_TOKEN": api_token, "AWF_POSTGRES_PASSWORD": pg_password}
    )

    assert result.name == "required_service_env"
    assert result.level is SetupCheckLevel.OK
    assert result.data == {
        "required": ["AWF_API_TOKEN", "AWF_POSTGRES_PASSWORD"],
        "missing": [],
    }
    rendered = " ".join(
        [result.summary, result.detail, result.fix or "", json.dumps(dict(result.data))]
    )
    assert api_token not in rendered
    assert pg_password not in rendered


@pytest.mark.unit
def test_check_required_service_env_blocks_when_unset() -> None:
    """Unset mandatory Compose vars block and name both missing variables.

    A clean first run (the documented ``cp .env.example docker/compose/.env`` ships
    AWF_API_TOKEN empty) would otherwise pass every probe yet make ``docker compose``
    abort, so this surfaces as a BLOCKED readiness issue naming both variables.
    """
    result = system_checks.check_required_service_env(environ={})

    assert result.name == "required_service_env"
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["missing"] == ["AWF_API_TOKEN", "AWF_POSTGRES_PASSWORD"]
    assert "AWF_API_TOKEN" in result.summary
    assert "AWF_POSTGRES_PASSWORD" in result.summary
    assert result.fix is not None
    assert "docker/compose/.env" in result.fix


@pytest.mark.unit
def test_check_required_service_env_treats_empty_value_as_unset() -> None:
    """An empty string is unset for Compose ``${VAR:?...}`` substitution.

    Compose aborts on an empty value exactly as it does on a missing one (the
    documented ``.env.example`` ships ``AWF_API_TOKEN=``), so the probe must treat
    ``AWF_API_TOKEN=""`` as missing rather than report ready. A non-empty value
    Compose would accept -- even an unusual whitespace one -- is left untouched so
    the gate cannot diverge from Compose's own ``${VAR:?}`` semantics.
    """
    result = system_checks.check_required_service_env(
        environ={"AWF_API_TOKEN": "", "AWF_POSTGRES_PASSWORD": ""}
    )

    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["missing"] == ["AWF_API_TOKEN", "AWF_POSTGRES_PASSWORD"]

    # A non-empty (even whitespace-only) value satisfies Compose's ``${VAR:?}``
    # guard, so the probe reports it set rather than over-reaching past Compose.
    whitespace_ok = system_checks.check_required_service_env(
        environ={"AWF_API_TOKEN": " ", "AWF_POSTGRES_PASSWORD": "pw"}
    )
    assert whitespace_ok.level is SetupCheckLevel.OK


@pytest.mark.unit
def test_check_required_service_env_blocks_single_missing_without_leaking_present() -> None:
    """One missing var blocks listing only it, never echoing the present secret."""
    pg_password = "present-pg-password-value"
    result = system_checks.check_required_service_env(
        environ={"AWF_POSTGRES_PASSWORD": pg_password}
    )

    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["missing"] == ["AWF_API_TOKEN"]
    assert "AWF_API_TOKEN" in result.summary
    assert "AWF_POSTGRES_PASSWORD" not in result.summary
    rendered = " ".join(
        [result.summary, result.detail, result.fix or "", json.dumps(dict(result.data))]
    )
    assert pg_password not in rendered


@pytest.mark.unit
def test_check_required_service_env_blocks_on_wrong_case_keys() -> None:
    """Differently-cased keys block: Compose ``${VAR:?...}`` is case-sensitive.

    ``docker/compose/local-service.yml`` interpolates the exact uppercase
    ``${AWF_API_TOKEN:?...}`` / ``${AWF_POSTGRES_PASSWORD:?...}``; on Unix env var
    names are case-sensitive, so a resolved env that only carries lowercase
    ``awf_api_token``/``awf_postgres_password`` makes ``docker compose`` abort. The
    probe must check the exact keys (not a case-insensitive lookup) so it cannot
    report readiness for an ``awf start`` Compose will reject.
    """
    result = system_checks.check_required_service_env(
        environ={
            "awf_api_token": "lower-case-api-token",
            "awf_postgres_password": "lower-case-pg-password",
        }
    )

    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["missing"] == ["AWF_API_TOKEN", "AWF_POSTGRES_PASSWORD"]


@pytest.mark.unit
def test_run_system_checks_blocks_on_missing_required_service_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aggregation surfaces the unset Compose vars as a blocker against the resolved env.

    The setup CLI feeds ``run_system_checks`` the resolved service env; when that env
    lacks the mandatory tokens the readiness payload must block instead of telling the
    operator to run a ``awf start`` Compose will reject.
    """

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    monkeypatch.setattr(
        system_checks,
        "check_docker",
        lambda **_kwargs: SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.OK,
            summary="ok",
            detail="ok",
            data={"available": True},
        ),
    )
    monkeypatch.setattr(system_checks, "check_compose", lambda **_kwargs: fake_ok("compose"))
    _stub_non_docker_checks_ok(monkeypatch)
    monkeypatch.setattr(system_checks, "check_host_home", lambda **_kwargs: fake_ok("host_home"))

    results = run_system_checks(
        config=HostSetupConfig(),
        environ={
            "AWF_API_HOST_PORT": "8000",
            "AWF_POSTGRES_HOST_PORT": "5433",
            "HOME": "/home/op",
        },
    )

    required = next(r for r in results if r.name == "required_service_env")
    assert required.level is SetupCheckLevel.BLOCKED
    assert required.data["missing"] == ["AWF_API_TOKEN", "AWF_POSTGRES_PASSWORD"]

    payload = build_setup_readiness_payload(results)
    assert payload.status == "blocked"
    rendered = json.dumps(render_first_run_json(payload))
    assert "required_service_env" in rendered
    assert "Run awf start" not in rendered


# --- ${HOME} fallback validation (no AWF_HOST_* override set) --------------


@pytest.mark.unit
def test_run_system_checks_blocks_on_non_absolute_home_work_dir_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative/``~`` ``HOME`` blocks the work-dir default when no override is set.

    With ``AWF_HOST_WORK_DIR`` unset, the local-service Compose stack binds
    ``${HOME}/.awf/service`` as both the source and the absolute-required mount
    target, interpolating ``${HOME}`` verbatim. A relative or ``~``-prefixed
    ``HOME`` (for example ``HOME=tmp``) therefore yields a non-absolute bind path
    Docker cannot mount, even though ``_default_compose_work_dir`` would expand or
    normalize it. The probe must block instead of reporting disk readiness for a
    directory ``awf start`` never mounts. ``AWF_HOST_HOME`` is pinned to an
    absolute value so only the work-dir fallback is exercised.
    """
    for bad_home in ("tmp", "./work", "~", "~/work", "~op/work"):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": bad_home, "AWF_HOST_HOME": "/home/op"},
        )

        disk = next(result for result in results if result.name == "disk")
        assert disk.level is SetupCheckLevel.BLOCKED, repr(bad_home)
        assert disk.data["env_value"] == bad_home
        assert "absolute" in disk.summary
        # The disk probe must not run for a path awf start never mounts.
        assert "disk_path" not in captured, repr(bad_home)


@pytest.mark.unit
def test_run_system_checks_blocks_on_padded_home_work_dir_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-padded/-only ``HOME`` blocks the work-dir default fallback.

    Compose interpolates ``${HOME}`` verbatim — with its surrounding whitespace —
    so a padded ``HOME`` makes ``awf start`` mount (or fail on) the spaced path
    instead of the stripped path the readiness probe would otherwise report.
    """
    for bad_home in (" /home/op", "/home/op ", "\t/home/op", "   "):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": bad_home, "AWF_HOST_HOME": "/home/op"},
        )

        disk = next(result for result in results if result.name == "disk")
        assert disk.level is SetupCheckLevel.BLOCKED, repr(bad_home)
        assert disk.data["env_value"] == bad_home
        assert "whitespace" in disk.summary
        assert "disk_path" not in captured, repr(bad_home)


@pytest.mark.unit
def test_run_system_checks_blocks_on_non_absolute_home_auth_mount_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative/``~`` ``HOME`` blocks the auth mounts when ``AWF_HOST_HOME`` is unset.

    The local-service Compose stack uses ``${AWF_HOST_HOME:-${HOME}}`` as both the
    host source and the absolute-required container target for every auth mount, so
    an unset ``AWF_HOST_HOME`` falls back to ``${HOME}`` verbatim. A relative or
    ``~``-prefixed ``HOME`` makes ``awf start`` fail to mount the auth directories,
    so the probe must block instead of declaring the machine ready.
    ``AWF_HOST_WORK_DIR`` is pinned to an absolute value so only the auth-mount
    fallback is exercised.
    """
    for bad_home in ("tmp", "./home", "~", "~/home", "~op/home"):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": bad_home, "AWF_HOST_WORK_DIR": "/data/awf"},
        )

        host_home = next(result for result in results if result.name == "host_home")
        assert host_home.level is SetupCheckLevel.BLOCKED, repr(bad_home)
        assert host_home.data["env_value"] == bad_home
        assert "absolute" in host_home.summary


@pytest.mark.unit
def test_run_system_checks_blocks_on_padded_home_auth_mount_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-padded/-only ``HOME`` blocks the auth-mount fallback.

    Compose keeps ``${HOME}`` unstripped, so a padded ``HOME`` reaches Docker with
    its surrounding whitespace and ``awf start`` mounts (or fails on) the spaced
    auth paths instead of the stripped path the readiness probe would report.
    """
    for bad_home in (" /home/op", "/home/op ", "\t/home/op", "   "):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ={"HOME": bad_home, "AWF_HOST_WORK_DIR": "/data/awf"},
        )

        host_home = next(result for result in results if result.name == "host_home")
        assert host_home.level is SetupCheckLevel.BLOCKED, repr(bad_home)
        assert host_home.data["env_value"] == bad_home
        assert "whitespace" in host_home.summary


@pytest.mark.unit
def test_run_system_checks_home_fallback_ok_when_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absolute ``HOME`` passes both fallbacks when no ``AWF_HOST_*`` override is set."""
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(work_dir="/persisted/state"),
        environ={"HOME": "/home/op"},
    )

    disk = next(result for result in results if result.name == "disk")
    host_home = next(result for result in results if result.name == "host_home")
    assert disk.level is SetupCheckLevel.OK
    assert host_home.level is SetupCheckLevel.OK
    assert captured["disk_path"] == Path("/home/op/.awf/service")


@pytest.mark.unit
def test_run_system_checks_blocks_on_unset_or_empty_home_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset or empty ``HOME`` blocks both fallbacks; Compose anchors them at ``/``.

    ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` and ``${AWF_HOST_HOME:-${HOME}}``
    fall back to ``${HOME}``, but ``${HOME}`` itself has no ``:-`` default: an unset
    or empty ``HOME`` interpolates to nothing, so Compose binds the work dir at
    ``/.awf/service`` and the auth mounts at ``/.config/gh`` (the filesystem root),
    not the directories under the account home. The readiness probe would instead
    expand ``~`` to the account home, so both checks must block rather than report
    readiness for directories ``awf start`` never mounts.
    """
    for empty in (None, ""):
        captured: dict[str, object] = {}
        _patch_probes_capture_disk_path(monkeypatch, captured)

        environ: dict[str, str] = {}
        if empty is not None:
            environ["HOME"] = empty
        results = run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ=environ,
        )

        disk = next(result for result in results if result.name == "disk")
        host_home = next(result for result in results if result.name == "host_home")
        assert disk.level is SetupCheckLevel.BLOCKED, repr(empty)
        assert host_home.level is SetupCheckLevel.BLOCKED, repr(empty)
        assert disk.data["env_value"] == "", repr(empty)
        assert host_home.data["env_value"] == "", repr(empty)
        # Neither probe runs for paths awf start never mounts.
        assert "disk_path" not in captured, repr(empty)


@pytest.mark.unit
def test_run_system_checks_home_fallback_suppressed_by_usable_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A usable ``AWF_HOST_WORK_DIR``/``AWF_HOST_HOME`` override hides a bad ``HOME``.

    When both overrides resolve to absolute paths Compose never interpolates
    ``${HOME}``, so a relative ``HOME`` is irrelevant to the bind/auth mounts and
    must not block readiness.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(work_dir="/persisted/state"),
        environ={
            "HOME": "tmp",
            "AWF_HOST_WORK_DIR": "/data/awf",
            "AWF_HOST_HOME": "/home/op",
        },
    )

    disk = next(result for result in results if result.name == "disk")
    host_home = next(result for result in results if result.name == "host_home")
    assert disk.level is SetupCheckLevel.OK
    assert host_home.level is SetupCheckLevel.OK
    assert captured["disk_path"] == Path("/data/awf")


@pytest.mark.unit
def test_run_system_checks_home_fallback_suppressed_by_explicit_work_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``work_dir`` wins over a bad ``HOME`` for the disk probe.

    The auth mounts still fall back to ``${HOME}`` (no ``AWF_HOST_HOME``), so the
    host_home check blocks while the disk check inspects the explicit directory.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(work_dir="/persisted/state"),
        work_dir=Path("/explicit/state"),
        environ={"HOME": "tmp"},
    )

    disk = next(result for result in results if result.name == "disk")
    host_home = next(result for result in results if result.name == "host_home")
    assert disk.level is SetupCheckLevel.OK
    assert captured["disk_path"] == Path("/explicit/state")
    assert host_home.level is SetupCheckLevel.BLOCKED
    assert host_home.data["env_value"] == "tmp"


@pytest.mark.unit
def test_resolve_work_dir_without_home_returns_relative_default_not_keyerror() -> None:
    """``_resolve_work_dir`` must not ``KeyError`` on a ``HOME``-less mapping.

    Regression for PR #332 review (comment issue:4585200251): the
    ``${HOME}/.awf/service`` default helper read ``environ["HOME"]`` directly.
    ``run_system_checks`` guards ``HOME`` (present + absolute) before this helper
    runs, but a direct internal/test call with a ``HOME``-less mapping (for
    example ``{}``) must fall through the normal path with an empty ``HOME``
    rather than raising an unguarded ``KeyError``.
    """
    resolved = system_checks._resolve_work_dir(work_dir=None, environ={})
    assert resolved == Path(".awf") / "service"


# --- Ollama-bridge profile readiness --------------------------------------
#
# Regression for PR #332 review thread PRRT_kwDOSJAM6s6F8Cuz: when
# COMPOSE_PROFILES enables ollama-bridge, awf start appends an ollama_bridge
# bootstrap stage and the local-service Compose stack binds
# ${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}
# via host networking. run_system_checks used to validate only the API/Postgres
# host ports, so a malformed bridge listen port/address passed awf setup
# --dry-run yet broke awf start. These tests pin the deterministic validation.


@pytest.mark.unit
def test_ollama_bridge_profile_enabled_parses_compose_profiles() -> None:
    """The profile gate mirrors bootstrap's comma/whitespace COMPOSE_PROFILES parse."""
    enabled = system_checks._ollama_bridge_profile_enabled
    assert enabled({"COMPOSE_PROFILES": "ollama-bridge"}) is True
    assert enabled({"COMPOSE_PROFILES": "a,ollama-bridge,b"}) is True
    assert enabled({"COMPOSE_PROFILES": "a ollama-bridge"}) is True
    assert enabled({"COMPOSE_PROFILES": "other"}) is False
    assert enabled({"COMPOSE_PROFILES": "ollama-bridgex"}) is False
    assert enabled({"COMPOSE_PROFILES": ""}) is False
    assert enabled({}) is False


@pytest.mark.unit
def test_ollama_bridge_checks_return_none_when_profile_disabled() -> None:
    """No readiness line is emitted when the optional ollama-bridge profile is off."""
    assert system_checks.check_ollama_bridge_listen_port(environ={}) is None
    assert system_checks.check_ollama_bridge_bind_address(environ={}) is None
    assert system_checks.check_ollama_bridge_target_port(environ={}) is None
    assert system_checks.check_ollama_bridge_target_host(environ={}) is None
    # A different enabled profile must not switch the bridge checks on.
    other = {"COMPOSE_PROFILES": "other", "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "nonsense"}
    assert system_checks.check_ollama_bridge_listen_port(environ=other) is None
    assert system_checks.check_ollama_bridge_bind_address(environ=other) is None
    assert system_checks.check_ollama_bridge_target_port(environ=other) is None
    assert system_checks.check_ollama_bridge_target_host(environ=other) is None


@pytest.mark.unit
def test_ollama_bridge_listen_port_ok_when_profile_active_and_valid() -> None:
    """An active profile with a usable (or unset) listen port reports OK with the resolved port."""
    default_ok = system_checks.check_ollama_bridge_listen_port(
        environ={"COMPOSE_PROFILES": "ollama-bridge"}
    )
    assert default_ok is not None
    assert default_ok.name == "ollama_bridge_port"
    assert default_ok.level is SetupCheckLevel.OK
    assert default_ok.data["port"] == system_checks.DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT
    assert default_ok.data["available"] is True

    override_ok = system_checks.check_ollama_bridge_listen_port(
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "11500"}
    )
    assert override_ok is not None
    assert override_ok.level is SetupCheckLevel.OK
    assert override_ok.data["port"] == 11500

    # An empty override is a legitimate fall-back to Compose's 11434 default.
    empty_ok = system_checks.check_ollama_bridge_listen_port(
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_LISTEN_PORT": ""}
    )
    assert empty_ok is not None
    assert empty_ok.level is SetupCheckLevel.OK
    assert empty_ok.data["port"] == system_checks.DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT


@pytest.mark.unit
def test_ollama_bridge_listen_port_blocks_on_unusable_override() -> None:
    """A set-but-unusable listen port blocks; Compose interpolates it verbatim into socat."""
    # Non-numeric, out-of-range, padded, Python-only, and Unicode-digit spellings
    # all break the socat TCP-LISTEN literal awf start runs.
    for invalid in (
        "abc",
        "0",
        "70000",
        " 11434",
        "11434 ",
        "   ",
        "11_434",
        "+11434",
        "１１４３４",
    ):
        result = system_checks.check_ollama_bridge_listen_port(
            environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_LISTEN_PORT": invalid}
        )
        assert result is not None, repr(invalid)
        assert result.name == "ollama_bridge_port"
        assert result.level is SetupCheckLevel.BLOCKED, repr(invalid)
        assert result.data["env_value"] == invalid
        assert result.data["available"] is False
        assert result.fix is not None


@pytest.mark.unit
def test_ollama_bridge_bind_address_ok_when_profile_active_and_valid() -> None:
    """An active profile with a usable (or unset) bind address reports OK."""
    default_ok = system_checks.check_ollama_bridge_bind_address(
        environ={"COMPOSE_PROFILES": "ollama-bridge"}
    )
    assert default_ok is not None
    assert default_ok.name == "ollama_bridge_bind_address"
    assert default_ok.level is SetupCheckLevel.OK
    assert default_ok.data["address"] == system_checks.DEFAULT_OLLAMA_BRIDGE_BIND_ADDRESS
    assert default_ok.data["available"] is True

    # A bare IP and a resolvable hostname are both legitimate -- the value is not
    # parsed as an IP, only checked for the verbatim-interpolation hazards.
    for address in ("0.0.0.0", "ollama.internal"):
        override_ok = system_checks.check_ollama_bridge_bind_address(
            environ={
                "COMPOSE_PROFILES": "ollama-bridge",
                "AWF_OLLAMA_BRIDGE_BIND_ADDRESS": address,
            }
        )
        assert override_ok is not None
        assert override_ok.level is SetupCheckLevel.OK
        assert override_ok.data["address"] == address


@pytest.mark.unit
def test_ollama_bridge_bind_address_blocks_on_unusable_value() -> None:
    """A whitespace- or comma-bearing bind address corrupts the socat command and blocks."""
    for invalid in (" 172.17.0.1", "172.17.0.1 ", "   ", "172.17.0.1,bind=evil", "172 .0", "\t172"):
        result = system_checks.check_ollama_bridge_bind_address(
            environ={
                "COMPOSE_PROFILES": "ollama-bridge",
                "AWF_OLLAMA_BRIDGE_BIND_ADDRESS": invalid,
            }
        )
        assert result is not None, repr(invalid)
        assert result.name == "ollama_bridge_bind_address"
        assert result.level is SetupCheckLevel.BLOCKED, repr(invalid)
        assert result.data["env_value"] == invalid
        assert result.data["available"] is False
        assert result.fix is not None


@pytest.mark.unit
def test_ollama_bridge_checks_default_to_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit environ the checks read the process env (os.environ)."""
    monkeypatch.delenv("AWF_OLLAMA_BRIDGE_LISTEN_PORT", raising=False)
    monkeypatch.delenv("AWF_OLLAMA_BRIDGE_BIND_ADDRESS", raising=False)
    monkeypatch.delenv("AWF_OLLAMA_BRIDGE_TARGET_PORT", raising=False)
    monkeypatch.delenv("AWF_OLLAMA_BRIDGE_TARGET_HOST", raising=False)
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    assert system_checks.check_ollama_bridge_listen_port() is None
    assert system_checks.check_ollama_bridge_bind_address() is None
    assert system_checks.check_ollama_bridge_target_port() is None
    assert system_checks.check_ollama_bridge_target_host() is None

    monkeypatch.setenv("COMPOSE_PROFILES", "ollama-bridge")
    port_active = system_checks.check_ollama_bridge_listen_port()
    address_active = system_checks.check_ollama_bridge_bind_address()
    target_active = system_checks.check_ollama_bridge_target_port()
    target_host_active = system_checks.check_ollama_bridge_target_host()
    assert port_active is not None and port_active.level is SetupCheckLevel.OK
    assert address_active is not None and address_active.level is SetupCheckLevel.OK
    assert target_active is not None and target_active.level is SetupCheckLevel.OK
    assert target_host_active is not None and target_host_active.level is SetupCheckLevel.OK


@pytest.mark.unit
def test_run_system_checks_omits_ollama_bridge_checks_when_profile_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (bridge-off) result list carries no ollama-bridge readiness lines."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(config=HostSetupConfig(), work_dir=Path("/tmp"), environ={})

    names = [result.name for result in results]
    assert "ollama_bridge_port" not in names
    assert "ollama_bridge_bind_address" not in names
    assert "ollama_bridge_target_port" not in names
    assert "ollama_bridge_target_host" not in names


@pytest.mark.unit
def test_run_system_checks_validates_ollama_bridge_when_profile_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active ollama-bridge profile adds OK bridge checks after the port block, before disk."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge"},
    )

    names = [result.name for result in results]
    assert "ollama_bridge_port" in names
    assert "ollama_bridge_bind_address" in names
    assert "ollama_bridge_target_port" in names
    assert "ollama_bridge_target_host" in names
    port = next(result for result in results if result.name == "ollama_bridge_port")
    address = next(result for result in results if result.name == "ollama_bridge_bind_address")
    target = next(result for result in results if result.name == "ollama_bridge_target_port")
    target_host = next(result for result in results if result.name == "ollama_bridge_target_host")
    assert port.level is SetupCheckLevel.OK
    assert address.level is SetupCheckLevel.OK
    assert target.level is SetupCheckLevel.OK
    assert target_host.level is SetupCheckLevel.OK
    # The bridge checks sit with the other port checks, before disk.
    assert names.index("ollama_bridge_port") > names.index("postgres_port")
    assert names.index("ollama_bridge_bind_address") < names.index("disk")
    assert names.index("ollama_bridge_target_port") < names.index("disk")
    assert names.index("ollama_bridge_target_host") < names.index("disk")


@pytest.mark.unit
def test_run_system_checks_blocks_on_invalid_ollama_bridge_port_when_profile_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed bridge listen port blocks readiness only when the profile is active."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "abc"},
    )

    port = next(result for result in results if result.name == "ollama_bridge_port")
    assert port.level is SetupCheckLevel.BLOCKED
    assert port.data["env_value"] == "abc"


@pytest.mark.unit
def test_run_system_checks_blocks_on_invalid_ollama_bridge_address_when_profile_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed bridge bind address blocks readiness only when the profile is active."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={
            "COMPOSE_PROFILES": "ollama-bridge",
            "AWF_OLLAMA_BRIDGE_BIND_ADDRESS": "172.17.0.1 ",
        },
    )

    address = next(result for result in results if result.name == "ollama_bridge_bind_address")
    assert address.level is SetupCheckLevel.BLOCKED
    assert address.data["env_value"] == "172.17.0.1 "


# Regression for PR #332 review thread PRRT_kwDOSJAM6s6F8KK3: the socat command
# has a *second* endpoint -- TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:
# ${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434} -- that Compose interpolates verbatim.
# Readiness validated only the listen port and bind address, so a malformed
# target port (for example abc) passed awf setup --dry-run yet broke awf start.
# These tests pin the same decimal validation for the upstream target port.


@pytest.mark.unit
def test_ollama_bridge_target_port_returns_none_when_profile_disabled() -> None:
    """No target-port readiness line is emitted when the ollama-bridge profile is off."""
    assert system_checks.check_ollama_bridge_target_port(environ={}) is None
    # A different enabled profile must not switch the target-port check on.
    other = {"COMPOSE_PROFILES": "other", "AWF_OLLAMA_BRIDGE_TARGET_PORT": "nonsense"}
    assert system_checks.check_ollama_bridge_target_port(environ=other) is None


@pytest.mark.unit
def test_ollama_bridge_target_port_ok_when_profile_active_and_valid() -> None:
    """An active profile with a usable (or unset) target port reports OK with the resolved port."""
    default_ok = system_checks.check_ollama_bridge_target_port(
        environ={"COMPOSE_PROFILES": "ollama-bridge"}
    )
    assert default_ok is not None
    assert default_ok.name == "ollama_bridge_target_port"
    assert default_ok.level is SetupCheckLevel.OK
    assert default_ok.data["port"] == system_checks.DEFAULT_OLLAMA_BRIDGE_TARGET_PORT
    assert default_ok.data["available"] is True

    override_ok = system_checks.check_ollama_bridge_target_port(
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_TARGET_PORT": "11500"}
    )
    assert override_ok is not None
    assert override_ok.level is SetupCheckLevel.OK
    assert override_ok.data["port"] == 11500

    # An empty override is a legitimate fall-back to Compose's 11434 default.
    empty_ok = system_checks.check_ollama_bridge_target_port(
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_TARGET_PORT": ""}
    )
    assert empty_ok is not None
    assert empty_ok.level is SetupCheckLevel.OK
    assert empty_ok.data["port"] == system_checks.DEFAULT_OLLAMA_BRIDGE_TARGET_PORT


@pytest.mark.unit
def test_ollama_bridge_target_port_blocks_on_unusable_override() -> None:
    """A set-but-unusable target port blocks; Compose interpolates it verbatim into socat's target."""
    for invalid in (
        "abc",
        "0",
        "70000",
        " 11434",
        "11434 ",
        "   ",
        "11_434",
        "+11434",
        "１１４３４",
    ):
        result = system_checks.check_ollama_bridge_target_port(
            environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_TARGET_PORT": invalid}
        )
        assert result is not None, repr(invalid)
        assert result.name == "ollama_bridge_target_port"
        assert result.level is SetupCheckLevel.BLOCKED, repr(invalid)
        assert result.data["env_value"] == invalid
        assert result.data["available"] is False
        assert result.fix is not None


@pytest.mark.unit
def test_run_system_checks_blocks_on_invalid_ollama_bridge_target_port_when_profile_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed bridge target port blocks readiness only when the profile is active."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_TARGET_PORT": "abc"},
    )

    target = next(result for result in results if result.name == "ollama_bridge_target_port")
    assert target.level is SetupCheckLevel.BLOCKED
    assert target.data["env_value"] == "abc"


# Regression for PR #332 review thread PRRT_kwDOSJAM6s6F8P2b: the socat target's
# *host* half -- TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:... -- is also
# interpolated verbatim, so a whitespace- or comma-bearing host corrupts the
# socat address yet passed awf setup --dry-run. These tests pin the same
# verbatim-interpolation guard the bind address already has, for the target host.


@pytest.mark.unit
def test_ollama_bridge_target_host_returns_none_when_profile_disabled() -> None:
    """No target-host readiness line is emitted when the ollama-bridge profile is off."""
    assert system_checks.check_ollama_bridge_target_host(environ={}) is None
    # A different enabled profile must not switch the target-host check on.
    other = {"COMPOSE_PROFILES": "other", "AWF_OLLAMA_BRIDGE_TARGET_HOST": "foo bar"}
    assert system_checks.check_ollama_bridge_target_host(environ=other) is None


@pytest.mark.unit
def test_ollama_bridge_target_host_ok_when_profile_active_and_valid() -> None:
    """An active profile with a usable (or unset) target host reports OK with the resolved host."""
    default_ok = system_checks.check_ollama_bridge_target_host(
        environ={"COMPOSE_PROFILES": "ollama-bridge"}
    )
    assert default_ok is not None
    assert default_ok.name == "ollama_bridge_target_host"
    assert default_ok.level is SetupCheckLevel.OK
    assert default_ok.data["host"] == system_checks.DEFAULT_OLLAMA_BRIDGE_TARGET_HOST
    assert default_ok.data["available"] is True

    # A bare IP and a resolvable hostname are both legitimate -- the value is not
    # parsed as an IP, only checked for the verbatim-interpolation hazards.
    for host in ("10.0.0.5", "ollama.internal"):
        override_ok = system_checks.check_ollama_bridge_target_host(
            environ={
                "COMPOSE_PROFILES": "ollama-bridge",
                "AWF_OLLAMA_BRIDGE_TARGET_HOST": host,
            }
        )
        assert override_ok is not None
        assert override_ok.level is SetupCheckLevel.OK
        assert override_ok.data["host"] == host


@pytest.mark.unit
def test_ollama_bridge_target_host_blocks_on_unusable_value() -> None:
    """A whitespace- or comma-bearing target host corrupts the socat target and blocks."""
    for invalid in (" 127.0.0.1", "127.0.0.1 ", "   ", "127.0.0.1,fork", "foo bar", "\t127"):
        result = system_checks.check_ollama_bridge_target_host(
            environ={
                "COMPOSE_PROFILES": "ollama-bridge",
                "AWF_OLLAMA_BRIDGE_TARGET_HOST": invalid,
            }
        )
        assert result is not None, repr(invalid)
        assert result.name == "ollama_bridge_target_host"
        assert result.level is SetupCheckLevel.BLOCKED, repr(invalid)
        assert result.data["env_value"] == invalid
        assert result.data["available"] is False
        assert result.fix is not None


@pytest.mark.unit
def test_run_system_checks_blocks_on_invalid_ollama_bridge_target_host_when_profile_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed bridge target host blocks readiness only when the profile is active."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        config=HostSetupConfig(),
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_TARGET_HOST": "foo bar"},
    )

    target = next(result for result in results if result.name == "ollama_bridge_target_host")
    assert target.level is SetupCheckLevel.BLOCKED
    assert target.data["env_value"] == "foo bar"
