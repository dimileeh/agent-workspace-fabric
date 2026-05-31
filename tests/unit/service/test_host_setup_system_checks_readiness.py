"""Provider, readiness-payload, real-IO probe, and API host-port override tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from awf.host_setup import system_checks
from awf.host_setup.config import DEFAULT_API_HOST_PORT
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
    PortProbeResult,
    SetupCheckError,
    SetupCheckLevel,
    SetupCheckResult,
    build_setup_readiness_payload,
    check_git,
    check_local_capacity,
    checks_core,
    normalize_provider,
    normalize_providers,
    primitives,
    require_interactive,
    run_system_checks,
)
from tests.unit.service.host_setup_system_checks_support import (
    _command_runner,
    _FakeCompleted,
    _ok,
    _patch_probes_capture_port,
)

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
def test_normalize_provider_accepts_cursor() -> None:
    """Verify the supported Cursor runtime is selectable through setup.

    Cursor is a first-class provider everywhere else (provider readiness's
    ``PROVIDER_NAMES``/``ProviderName``, ``awf service`` help, and the
    ``cursor-agent`` adapter runtime), so ``awf setup --provider cursor`` must
    resolve instead of failing ``SETUP_PROVIDER_UNKNOWN`` -- otherwise the
    dry-run payload can never forward the Cursor selector to provider setup.
    """
    assert normalize_provider("cursor") == "cursor"
    assert "cursor" in KNOWN_SETUP_PROVIDERS
    assert normalize_provider("Cursor") == "cursor"


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
    assert "fish_add_path" in checks_core._shell_path_fix("/usr/bin/fish", script_dir)
    assert "bashrc" in checks_core._shell_path_fix("/bin/bash", script_dir)
    assert "shell profile" in checks_core._shell_path_fix("", script_dir)


# --- Default real-IO probe helpers (hermetic) ----------------------------


@pytest.mark.unit
def test_default_command_runner_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the default runner captures a completed probe result."""
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _FakeCompleted(0, "out", "err"))
    result = primitives._default_command_runner(["echo", "hi"])
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
    assert primitives._default_command_runner(["missing-binary"]) is None


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
    assert primitives._default_command_runner(["probe"]) is None


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
    result = primitives._default_command_runner(["probe"])
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
        assert primitives._default_port_probe(port) is PortProbeResult.IN_USE
    finally:
        listener.close()
    assert primitives._default_port_probe(port) is PortProbeResult.FREE


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
            assert primitives._default_port_probe(8000) is expected


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
        assert primitives._default_port_probe(port) is PortProbeResult.IN_USE
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
        assert primitives._loopback_port_probe(port) is PortProbeResult.IN_USE
    finally:
        listener.close()
    assert primitives._loopback_port_probe(port) is PortProbeResult.FREE


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
        assert primitives._loopback_port_probe(port) is PortProbeResult.FREE
    finally:
        listener.close()


@pytest.mark.unit
def test_default_free_disk_bytes_real_and_parent_fallback(tmp_path: Path) -> None:
    """Verify free-disk reads a real path and falls back to an existing parent."""
    assert primitives._default_free_disk_bytes(tmp_path) >= 0
    nested = tmp_path / "does" / "not" / "exist"
    assert primitives._default_free_disk_bytes(nested) >= 0


@pytest.mark.unit
def test_default_free_disk_bytes_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify an unreadable filesystem yields None rather than raising."""
    import shutil

    def boom(_path: object) -> object:
        raise OSError

    monkeypatch.setattr(shutil, "disk_usage", boom)
    assert primitives._default_free_disk_bytes("/anything") is None


@pytest.mark.unit
def test_default_free_disk_bytes_tolerates_unresolvable_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable ``~user`` work dir must probe parents, not raise RuntimeError."""
    import os.path

    monkeypatch.setattr(os.path, "expanduser", lambda value: value)
    # Falls back to the raw path and walks up to an existing parent (the CWD).
    assert primitives._default_free_disk_bytes("~olduser/.awf/service") >= 0


@pytest.mark.unit
def test_safe_expanduser_falls_back_on_unresolvable_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_safe_expanduser`` returns the raw path when ``~user`` cannot be resolved."""
    import os.path

    assert primitives._safe_expanduser("~/awf") == Path("~/awf").expanduser()
    monkeypatch.setattr(os.path, "expanduser", lambda value: value)
    assert primitives._safe_expanduser("~olduser/.awf/service") == Path("~olduser/.awf/service")


@pytest.mark.unit
def test_default_total_memory_bytes_real() -> None:
    """Verify the default memory probe returns None or a positive estimate."""
    value = primitives._default_total_memory_bytes()
    assert value is None or value > 0


@pytest.mark.unit
def test_default_total_memory_bytes_handles_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify sysconf failures and non-positive values yield None."""
    import os

    def boom(_name: str) -> int:
        raise ValueError

    monkeypatch.setattr(os, "sysconf", boom)
    assert primitives._default_total_memory_bytes() is None

    monkeypatch.setattr(os, "sysconf", lambda _name: 0)
    assert primitives._default_total_memory_bytes() is None


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


@pytest.mark.unit
def test_run_system_checks_honors_awf_api_host_port_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``AWF_API_HOST_PORT`` override is probed instead of the config default."""
    captured: dict[str, object] = {}
    _patch_probes_capture_port(monkeypatch, captured)

    run_system_checks(
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
