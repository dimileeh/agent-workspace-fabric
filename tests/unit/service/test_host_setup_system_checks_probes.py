"""Probe-level and run_system_checks aggregation tests for host setup checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from awf.host_setup import system_checks
from awf.host_setup.config import HostSetupConfig
from awf.host_setup.system_checks import (
    CommandResult,
    PortProbeResult,
    SetupCheckLevel,
    SetupCheckResult,
    check_compose,
    check_disk,
    check_docker,
    check_gh,
    check_git,
    check_local_capacity,
    check_ports,
    check_python_runtime,
    check_shell_path,
    checks_core,
    primitives,
    run_system_checks,
)
from tests.unit.service.host_setup_system_checks_support import (
    _command_runner,
    _stub_non_docker_checks_ok,
)

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
    assert primitives._docker_probe_environ(None) is None


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

    resolved = primitives._docker_probe_environ({"AWF_DOCKER_HOST": "tcp://remote:2375"})

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

    resolved = primitives._docker_probe_environ({"DOCKER_HOST": ""})

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

    monkeypatch.setattr(primitives.subprocess, "run", fake_run)
    monkeypatch.setattr(primitives.shutil, "which", lambda _cmd, **_kwargs: "/usr/bin/docker")
    _stub_non_docker_checks_ok(monkeypatch)

    results = run_system_checks(
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

    monkeypatch.setattr(primitives.subprocess, "run", fake_run)
    monkeypatch.setattr(primitives.shutil, "which", lambda _cmd: "/usr/bin/docker")
    _stub_non_docker_checks_ok(monkeypatch)

    run_system_checks()

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

    monkeypatch.setattr(primitives.subprocess, "run", fake_run)
    monkeypatch.setattr(primitives.shutil, "which", fake_which)
    _stub_non_docker_checks_ok(monkeypatch)

    results = run_system_checks(
        environ={"PATH": "/opt/docker/bin"},
    )

    docker_check = next(r for r in results if r.name == "docker")
    assert docker_check.level is SetupCheckLevel.OK
    assert docker_check.data["available"] is True
    # The gate searched the resolved service-env PATH, not the bare process PATH.
    assert any(path is not None and "/opt/docker/bin" in path for path in seen)


@pytest.mark.unit
def test_run_system_checks_resolves_docker_probe_env_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon-selection probe env is resolved once and shared by both helpers.

    Regression for review comment issue:4585200251: the separate
    ``_docker_probe_runner`` and ``_docker_probe_which`` helpers each resolved
    ``_docker_probe_environ`` independently, so the ``{**os.environ, **environ}``
    merge and the ``AWF_DOCKER_HOST`` / ``DOCKER_HOST`` daemon-selection scrub ran
    twice per ``run_system_checks`` call for an identical result.
    ``_docker_probe_helpers`` now resolves it once and hands the same env to both
    the runner and the binary-presence ``which``.
    """
    real_probe_environ = primitives._docker_probe_environ
    resolved_envs: list[Mapping[str, str] | None] = []

    def counting_probe_environ(
        environ: Mapping[str, str] | None,
    ) -> dict[str, str] | None:
        resolved_envs.append(environ)
        return real_probe_environ(environ)

    monkeypatch.setattr(primitives, "_docker_probe_environ", counting_probe_environ)

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

    monkeypatch.setattr(primitives.subprocess, "run", fake_run)
    monkeypatch.setattr(primitives.shutil, "which", lambda _cmd, **_kwargs: "/usr/bin/docker")
    _stub_non_docker_checks_ok(monkeypatch)

    # Pass a service env so the resolver returns a non-None probe env (the path
    # that previously ran the merge twice, once per helper).
    run_system_checks(environ={"AWF_DOCKER_HOST": "tcp://remote:2375"})

    # One resolution feeds both the docker/compose runner and the ``which`` gate.
    assert len(resolved_envs) == 1


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
    # The missing-gh warning must point Bitbucket users at the .env auth path so
    # they are not told to install gh for a forge that does not need it (#525).
    assert "BITBUCKET_API_TOKEN" in missing.detail
    assert "bitbucket.org" in missing.detail
    # Basic auth mode (the default) also requires the email and auth-mode vars, so
    # the warning must name them — token alone leaves PR/git auth broken (#528).
    assert "BITBUCKET_EMAIL" in missing.detail
    assert "BITBUCKET_AUTH_MODE" in missing.detail
    # The fix must be scoped to GitHub so it does not contradict the Bitbucket
    # guidance in the detail — a Bitbucket user should not be told to install gh.
    assert missing.fix is not None
    assert missing.fix.startswith("For GitHub repos:")


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
    assert checks_core._resolve_path(raw) == raw


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

    results = run_system_checks()

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

    results = run_system_checks()

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

    names = [r.name for r in run_system_checks()]

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
        environ={"AWF_HOST_WORK_DIR": "~olduser/.awf/service"},
    )

    assert [r.name for r in results].count("disk") == 1
    disk = next(result for result in results if result.name == "disk")
    assert disk.level is SetupCheckLevel.BLOCKED
    assert disk.data["env_value"] == "~olduser/.awf/service"
    # Blocked before any expansion, so the disk probe never runs (no traceback).
    assert "disk_path" not in captured
