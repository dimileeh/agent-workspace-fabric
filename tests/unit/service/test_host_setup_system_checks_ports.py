"""Postgres host-port, host-port collision, and work-dir readiness tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.host_setup import system_checks
from awf.host_setup.system_checks import (
    PortProbeResult,
    SetupCheckLevel,
    SetupCheckResult,
    primitives,
    run_system_checks,
)
from tests.unit.service.host_setup_system_checks_support import (
    _patch_probes_capture_disk_path,
    _patch_probes_capture_postgres_port,
)

# --- Postgres host port --------------------------------------------------


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
    assert default_probe is primitives._loopback_port_probe


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
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "abc"},
    )

    assert all(result.name != "ollama_bridge_port_conflict" for result in results)
    port = next(result for result in results if result.name == "ollama_bridge_port")
    assert port.level is SetupCheckLevel.BLOCKED
    assert port.data["env_value"] == "abc"


@pytest.mark.unit
def test_check_ollama_bridge_postgres_port_conflict_blocks_when_loopback_overlaps() -> None:
    """A bridge bound to Postgres's 127.0.0.1 on a shared host port is a hard blocker.

    The Compose stack publishes Postgres on 127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}
    and, with the ollama-bridge profile on, runs a host-networking socat binding
    ${AWF_OLLAMA_BRIDGE_BIND_ADDRESS}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT}. awf service
    bootstrap starts postgres before ollama_bridge, so Docker reserves the Postgres
    loopback port first and socat fails to bind the same 127.0.0.1 port. The
    single-port probes bind and release independently, so only the cross-check
    catches it; it must block and carry both ports and the bind address.
    """
    result = system_checks.check_ollama_bridge_postgres_port_conflict(5433, 5433, "127.0.0.1")

    assert result is not None
    assert result.name == "ollama_bridge_postgres_port_conflict"
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["postgres_port"] == 5433
    assert result.data["ollama_bridge_listen_port"] == 5433
    assert result.data["bridge_bind_address"] == "127.0.0.1"
    assert "5433" in result.summary
    assert result.fix is not None
    assert "AWF_OLLAMA_BRIDGE_LISTEN_PORT" in result.fix
    assert "AWF_POSTGRES_HOST_PORT" in result.fix


@pytest.mark.unit
def test_check_ollama_bridge_postgres_port_conflict_blocks_when_bridge_wildcard() -> None:
    """A 0.0.0.0 bridge bind overlaps Postgres's loopback on a shared host port.

    An IPv4 wildcard bind reserves the port on every address, including the
    127.0.0.1 loopback Docker publishes Postgres on, so a shared port still
    collides even though the literal bind addresses differ.
    """
    result = system_checks.check_ollama_bridge_postgres_port_conflict(5433, 5433, "0.0.0.0")

    assert result is not None
    assert result.name == "ollama_bridge_postgres_port_conflict"
    assert result.level is SetupCheckLevel.BLOCKED
    assert result.data["bridge_bind_address"] == "0.0.0.0"


@pytest.mark.unit
def test_check_ollama_bridge_postgres_port_conflict_passes_when_ports_differ() -> None:
    """Distinct Postgres/bridge host ports add no readiness line (the common case)."""
    assert (
        system_checks.check_ollama_bridge_postgres_port_conflict(5433, 11434, "127.0.0.1") is None
    )


@pytest.mark.unit
def test_check_ollama_bridge_postgres_port_conflict_passes_when_addresses_distinct() -> None:
    """A shared port is harmless when the bridge binds a non-loopback address.

    The default bridge bind (172.17.0.1, the docker0 gateway) is a distinct
    specific address from Postgres's 127.0.0.1, so Docker can reserve both even on
    a shared port -- the cross-check must not false-positive on the default config.
    """
    assert (
        system_checks.check_ollama_bridge_postgres_port_conflict(5433, 5433, "172.17.0.1") is None
    )


@pytest.mark.unit
def test_run_system_checks_blocks_when_bridge_and_postgres_collide_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge bound to 127.0.0.1 on the Postgres host port surfaces as a blocker.

    With the bridge profile on, binding it to Postgres's loopback
    (AWF_OLLAMA_BRIDGE_BIND_ADDRESS=127.0.0.1) on the default Postgres port makes
    socat and the Postgres publish both claim 127.0.0.1:5433. Postgres comes up
    first, so awf start cannot bind the bridge; only the cross-check catches it
    (the per-port probes each report FREE in isolation).
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        work_dir=Path("/tmp"),
        environ={
            "COMPOSE_PROFILES": "ollama-bridge",
            "AWF_OLLAMA_BRIDGE_BIND_ADDRESS": "127.0.0.1",
            "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "5433",
        },
    )

    conflict = next(
        result for result in results if result.name == "ollama_bridge_postgres_port_conflict"
    )
    assert conflict.level is SetupCheckLevel.BLOCKED
    assert conflict.data["postgres_port"] == 5433
    assert conflict.data["ollama_bridge_listen_port"] == 5433
    assert conflict.data["bridge_bind_address"] == "127.0.0.1"
    # The cross-check is additive: the standalone probes still run.
    assert any(result.name == "postgres_port" for result in results)
    assert any(result.name == "ollama_bridge_port" for result in results)
    # It sits with the other port checks, before disk.
    names = [result.name for result in results]
    assert names.index("ollama_bridge_postgres_port_conflict") < names.index("disk")
    assert names.index("ollama_bridge_postgres_port_conflict") > names.index("postgres_port")


@pytest.mark.unit
def test_run_system_checks_omits_bridge_postgres_conflict_on_default_bind_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge listen port equal to Postgres's is harmless on the default bind.

    The default bridge bind (172.17.0.1) is a distinct address from Postgres's
    127.0.0.1, so Docker can reserve both even when the ports match; no conflict
    line is emitted.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "5433"},
    )

    assert all(result.name != "ollama_bridge_postgres_port_conflict" for result in results)


@pytest.mark.unit
def test_run_system_checks_omits_bridge_postgres_conflict_when_profile_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the bridge profile off there is no socat bind to collide with Postgres."""
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        work_dir=Path("/tmp"),
        environ={
            "AWF_OLLAMA_BRIDGE_BIND_ADDRESS": "127.0.0.1",
            "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "5433",
        },
    )

    assert all(result.name != "ollama_bridge_postgres_port_conflict" for result in results)


@pytest.mark.unit
def test_run_system_checks_skips_bridge_postgres_conflict_when_postgres_override_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid Postgres port override blocks on its own; the cross-check is skipped.

    A malformed AWF_POSTGRES_HOST_PORT leaves no resolved Postgres port to compare,
    so the collision cross-check must not run (and must not crash) -- the override
    blocker already wedges readiness.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        work_dir=Path("/tmp"),
        environ={
            "COMPOSE_PROFILES": "ollama-bridge",
            "AWF_OLLAMA_BRIDGE_BIND_ADDRESS": "127.0.0.1",
            "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "5433",
            "AWF_POSTGRES_HOST_PORT": "abc",
        },
    )

    assert all(result.name != "ollama_bridge_postgres_port_conflict" for result in results)
    postgres = next(result for result in results if result.name == "postgres_port")
    assert postgres.level is SetupCheckLevel.BLOCKED


@pytest.mark.unit
def test_run_system_checks_skips_bridge_postgres_conflict_when_bind_address_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed bridge bind address blocks on its own; the cross-check is skipped.

    A bind address with whitespace corrupts the socat command, so
    check_ollama_bridge_bind_address already blocks; there is no resolved bind
    address to compare against Postgres and the cross-check must not run.
    """
    captured: dict[str, object] = {}
    _patch_probes_capture_postgres_port(monkeypatch, captured)

    results = run_system_checks(
        work_dir=Path("/tmp"),
        environ={
            "COMPOSE_PROFILES": "ollama-bridge",
            "AWF_OLLAMA_BRIDGE_BIND_ADDRESS": "127.0.0.1 ",
            "AWF_OLLAMA_BRIDGE_LISTEN_PORT": "5433",
        },
    )

    assert all(result.name != "ollama_bridge_postgres_port_conflict" for result in results)
    bind = next(result for result in results if result.name == "ollama_bridge_bind_address")
    assert bind.level is SetupCheckLevel.BLOCKED


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
        environ={"AWF_HOST_HOME": "/home/op"},
    )

    disk = next(result for result in results if result.name == "disk")
    assert disk.level is SetupCheckLevel.BLOCKED
    assert disk.data["env_value"] == ""
    assert "unset or empty" in disk.summary
    # The disk probe must not run for a path awf start never mounts.
    assert "disk_path" not in captured
