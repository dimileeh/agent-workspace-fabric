"""Host-home, required-service-env, HOME-fallback, and ollama-bridge tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.host_setup import system_checks
from awf.host_setup.rendering import (
    render_first_run_json,
)
from awf.host_setup.system_checks import (
    SetupCheckLevel,
    SetupCheckResult,
    build_setup_readiness_payload,
    checks_host,
    checks_ports,
    run_system_checks,
)
from tests.unit.service.host_setup_system_checks_support import (
    _patch_probes_capture_disk_path,
    _patch_probes_capture_postgres_port,
    _stub_non_docker_checks_ok,
)

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
    resolved = checks_host._resolve_work_dir(work_dir=None, environ={})
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
    enabled = checks_ports._ollama_bridge_profile_enabled
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

    results = run_system_checks(work_dir=Path("/tmp"), environ={})

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
        work_dir=Path("/tmp"),
        environ={"COMPOSE_PROFILES": "ollama-bridge", "AWF_OLLAMA_BRIDGE_TARGET_HOST": "foo bar"},
    )

    target = next(result for result in results if result.name == "ollama_bridge_target_host")
    assert target.level is SetupCheckLevel.BLOCKED
    assert target.data["env_value"] == "foo bar"
