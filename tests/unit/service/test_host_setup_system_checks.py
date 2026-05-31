"""Fixture-driven tests for host setup system checks and readiness payload."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from awf.host_setup import system_checks
from awf.host_setup.config import DEFAULT_API_HOST_PORT, ApiConfig, HostSetupConfig
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


@pytest.mark.unit
def test_run_system_checks_tolerates_unresolvable_work_dir_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``docker/compose/.env`` ``AWF_HOST_WORK_DIR: ~olduser/...`` must not crash.

    ``Path.expanduser`` raises ``RuntimeError`` for a ``~user`` component the host
    cannot resolve. Aggregation runs before the reason-coded setup handlers, so an
    unguarded expansion of the Compose work-dir override would abort
    ``awf setup --dry-run`` with a traceback instead of producing the readiness
    payload.
    """
    import os.path

    # Simulate a host that cannot resolve the ``~user`` component: os.path leaves
    # the leading ``~user`` intact, which makes Path.expanduser raise RuntimeError.
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
        monkeypatch.setattr(system_checks, name, lambda name=name: fake_ok(name))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))

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
    # The unresolvable expansion falls back to the raw path rather than raising.
    assert captured["disk_path"] == Path("~olduser/.awf/service")


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

    monkeypatch.setattr(system_checks, "check_docker", lambda: fake_ok("docker"))
    monkeypatch.setattr(system_checks, "check_compose", lambda: fake_ok("compose"))
    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", fake_ports)
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

    monkeypatch.setattr(system_checks, "check_docker", lambda: fake_ok("docker"))
    monkeypatch.setattr(system_checks, "check_compose", lambda: fake_ok("compose"))
    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))
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
def test_run_system_checks_expands_awf_host_work_dir_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``~`` in the env override is expanded before the disk probe inspects it."""
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    run_system_checks(
        config=HostSetupConfig(work_dir="/persisted/state"),
        environ={"AWF_HOST_WORK_DIR": "~/custom/state"},
    )

    assert captured["disk_path"] == Path("~/custom/state").expanduser()


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
    """A missing or blank ``AWF_HOST_WORK_DIR`` falls back to Compose's default.

    The local-service Compose stack bind-mounts
    ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` and ``awf start`` never reads
    the persisted ``config.work_dir`` — it resolves the bind from the Compose
    env. So an absent or whitespace-only override probes Compose's built-in
    ``${HOME}/.awf/service`` default, and a non-default ``config.work_dir`` is
    deliberately ignored: probing it would report disk readiness for a directory
    ``awf start`` would never mount.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    for blank in (None, "", "   "):
        environ = {"HOME": "/home/op"}
        if blank is not None:
            environ["AWF_HOST_WORK_DIR"] = blank
        run_system_checks(
            config=HostSetupConfig(work_dir="/persisted/state"),
            environ=environ,
        )
        assert captured["disk_path"] == Path("/home/op/.awf/service"), repr(blank)


@pytest.mark.unit
def test_run_system_checks_default_work_dir_expands_home_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``HOME`` the default mirrors Compose expanding ``~`` to the user home."""
    captured: dict[str, object] = {}
    _patch_probes_capture_disk_path(monkeypatch, captured)

    run_system_checks(
        config=HostSetupConfig(work_dir="/persisted/state"),
        environ={},
    )

    assert captured["disk_path"] == Path("~/.awf/service").expanduser()
