"""API/Postgres host-port and optional ollama-bridge readiness checks.

These probes validate the host-port overrides and the ollama-bridge profile that
``awf start`` consumes from the resolved service env, blocking on values that
would pass an isolated probe yet break ``docker compose`` at start time.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from awf.host_setup.config import DEFAULT_API_HOST_PORT
from awf.host_setup.system_checks.primitives import (
    _BRIDGE_BIND_ADDRESSES_OVERLAPPING_POSTGRES,
    _POSTGRES_HOST_BIND_ADDRESS,
    DEFAULT_OLLAMA_BRIDGE_BIND_ADDRESS,
    DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT,
    DEFAULT_OLLAMA_BRIDGE_TARGET_HOST,
    DEFAULT_OLLAMA_BRIDGE_TARGET_PORT,
    DEFAULT_POSTGRES_HOST_PORT,
    SetupCheckLevel,
    SetupCheckResult,
)
from awf.service.environment import env_lookup


def _env_api_host_port(environ: Mapping[str, str]) -> int | None:
    """Return a usable ``AWF_API_HOST_PORT`` override, or ``None`` when unusable.

    A missing, empty, whitespace-only, *surrounding-whitespace* (padded),
    malformed (including Python-only spellings such as ``8_000`` or ``+8000``
    that ``int`` accepts but Compose's decimal port syntax rejects), or
    out-of-range value yields ``None``. The override is "usable"
    only when AWF can honor it identically across every layer, and a value with
    leading/trailing whitespace cannot be: this helper would ``strip`` it to a
    valid port, but Compose interpolates ``${AWF_API_HOST_PORT:-8000}:8000``
    verbatim, so a padded ``" 8000"`` would pass the bind probe for the stripped
    8000 while ``awf start`` tries to publish ``" 8000:8000"`` and fails. Whether
    that ``None`` means a legitimate fall-back to Compose's ``8000`` default (only
    an *unset or empty* override, mirroring ``${AWF_API_HOST_PORT:-8000}``) or a
    startup blocker (any other set-but-unusable value, including whitespace-only
    or padded, which Compose interpolates verbatim) is decided by
    :func:`_invalid_api_host_port_override`, which the readiness probe surfaces as
    a blocker instead of silently probing the stripped or default port. This
    mirrors the padded-value guard in :func:`_env_host_work_dir`.
    """
    raw = environ.get("AWF_API_HOST_PORT")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    # Compose's port short syntax accepts only plain ASCII decimal digits, but
    # ``int()`` also accepts underscore grouping (``8_000``), a leading sign
    # (``+8000``/``-8000``), and non-ASCII Unicode digits. Honoring such a value
    # would probe the *parsed* port while Compose interpolates the literal into
    # ``${AWF_API_HOST_PORT:-8000}:8000`` and ``awf start`` fails to parse and
    # publish it, so reject anything that is not ASCII-decimal before parsing
    # (this also makes ``int`` below total, so there is no dead error branch).
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    parsed = int(candidate)
    if not 1 <= parsed <= 65535:
        return None
    return parsed


def _resolve_api_host_port(
    *,
    port: int | None,
    environ: Mapping[str, str] | None,
) -> int:
    """Resolve which host port the readiness probe should bind.

    Precedence mirrors the port ``awf start`` actually publishes: an explicit
    caller override wins, then the ``AWF_API_HOST_PORT`` environment override
    that Docker Compose interpolates into ``${AWF_API_HOST_PORT:-8000}:8000``
    (and that ``awf service bootstrap`` resolves the host-side URL from), and
    finally Compose's built-in ``8000`` default when no override is set. Honoring
    the env override keeps ``awf setup`` from falsely blocking on the default
    8000 when an operator has moved the published port elsewhere.

    The persisted ``config.api.host_port`` is deliberately *not* consulted here.
    ``awf start`` publishes ``${AWF_API_HOST_PORT:-8000}`` from the resolved
    Compose env and never reads ``config.api.host_port``; nothing propagates that
    persisted value into the Compose env. Probing it would report readiness for a
    port ``awf start`` would never publish whenever an operator set a non-default
    ``config.api.host_port`` without also exporting ``AWF_API_HOST_PORT``.
    """
    if port is not None:
        return port
    env = os.environ if environ is None else environ
    override = _env_api_host_port(env)
    if override is not None:
        return override
    return DEFAULT_API_HOST_PORT


def _invalid_api_host_port_override(
    *,
    port: int | None,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return the raw ``AWF_API_HOST_PORT`` when it is set to an unusable value.

    Returns ``None`` (no configuration error) when an explicit caller ``port``
    wins, when the override is unset, or when it is *genuinely empty* (a
    zero-length string) — the empty case is a legitimate fall-back to Compose's
    ``8000`` default because ``${AWF_API_HOST_PORT:-8000}`` substitutes the
    default only when the variable is unset or empty. Any other set value that
    :func:`_env_api_host_port` cannot honor verbatim is returned, *including a
    whitespace-only or surrounding-whitespace (padded) value*: Compose treats
    ``"   "`` and ``" 8000"`` as non-empty literals and publishes them verbatim
    into ``"   :8000"`` / ``" 8000:8000"`` (so ``awf start`` fails). A
    whitespace-only value is additionally rejected by ``awf service`` settings
    (``_default_local_service_api_base_url`` reaches ``int("   ")``, which raises);
    a padded ``" 8000"`` survives ``int`` there but still breaks Compose, so the
    readiness probe must block on both instead of silently probing the stripped or
    default port and reporting the wrong port as free. The ``not raw`` guard
    mirrors ``awf service``'s own ``if not host_port`` fall-back so the two layers
    agree on empty-vs-whitespace, and the padded-value rejection mirrors
    :func:`_env_host_work_dir`.
    """
    if port is not None:
        return None
    env = os.environ if environ is None else environ
    raw = env.get("AWF_API_HOST_PORT")
    if not raw:
        return None
    if _env_api_host_port(env) is not None:
        return None
    return raw


def check_api_host_port_override(raw: str) -> SetupCheckResult:
    """Report a set-but-unusable ``AWF_API_HOST_PORT`` as a startup blocker.

    The local-service Compose stack publishes the API as
    ``${AWF_API_HOST_PORT:-8000}:8000`` and ``awf service`` settings parse the
    same override, so a non-empty value that is not a ``1..65535`` TCP port is
    used verbatim and ``awf start`` fails to publish the port. The readiness
    probe blocks on it rather than silently falling back to the default port and
    reporting the wrong port as free.
    """
    return SetupCheckResult(
        name="ports",
        level=SetupCheckLevel.BLOCKED,
        summary=f"AWF_API_HOST_PORT={raw!r} is not a valid TCP port.",
        detail="AWF_API_HOST_PORT must be an integer between 1 and 65535. The local-service "
        "Compose stack publishes the API as ${AWF_API_HOST_PORT:-8000}:8000 and awf service "
        "settings parse the same override, so this value is used verbatim and awf start fails "
        "to publish the port.",
        fix="Set AWF_API_HOST_PORT to an integer between 1 and 65535, or unset it to use the "
        "default 8000, then re-run awf setup --dry-run.",
        data={"port": None, "available": False, "env_value": raw},
    )


def _env_postgres_host_port(environ: Mapping[str, str]) -> int | None:
    """Return a usable ``AWF_POSTGRES_HOST_PORT`` override, or ``None`` when unusable.

    Mirrors :func:`_env_api_host_port` for the Postgres host port. A missing,
    empty, whitespace-only, *surrounding-whitespace* (padded), malformed
    (including Python-only spellings such as ``5_433`` or ``+5433`` that ``int``
    accepts but Compose's decimal port syntax rejects), or
    out-of-range value yields ``None`` — a padded value cannot be honored
    identically across layers because Compose interpolates
    ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432`` verbatim. Whether that
    ``None`` means a legitimate fall-back to Compose's ``5433`` default (only an
    *unset or empty* override, mirroring ``${AWF_POSTGRES_HOST_PORT:-5433}``) or a
    startup blocker (any other set-but-unusable value, including whitespace-only
    or padded, which Compose interpolates verbatim) is decided by
    :func:`_invalid_postgres_host_port_override`.
    """
    raw = environ.get("AWF_POSTGRES_HOST_PORT")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    # Compose's port short syntax accepts only plain ASCII decimal digits, but
    # ``int()`` also accepts underscore grouping (``5_433``), a leading sign
    # (``+5433``/``-5433``), and non-ASCII Unicode digits. Honoring such a value
    # would probe the *parsed* port while Compose interpolates the literal into
    # ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432`` and ``awf start`` fails
    # to parse and publish it, so reject anything that is not ASCII-decimal
    # before parsing (this also makes ``int`` below total, no dead error branch).
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    parsed = int(candidate)
    if not 1 <= parsed <= 65535:
        return None
    return parsed


def _resolve_postgres_host_port(*, environ: Mapping[str, str] | None) -> int:
    """Resolve which Postgres host port the readiness probe should bind.

    Mirrors :func:`_resolve_api_host_port`, minus a caller ``port`` override:
    nothing passes an explicit Postgres port. Precedence follows the port
    ``awf start`` actually publishes — the ``AWF_POSTGRES_HOST_PORT`` environment
    override that Docker Compose interpolates into
    ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``, and finally Compose's
    built-in ``5433`` default when no override is set.
    """
    env = os.environ if environ is None else environ
    override = _env_postgres_host_port(env)
    if override is not None:
        return override
    return DEFAULT_POSTGRES_HOST_PORT


def _invalid_postgres_host_port_override(*, environ: Mapping[str, str] | None) -> str | None:
    """Return the raw ``AWF_POSTGRES_HOST_PORT`` when it is set to an unusable value.

    Mirrors :func:`_invalid_api_host_port_override` for Postgres (no caller
    ``port`` override exists). Returns ``None`` when the override is unset or
    *genuinely empty* (a zero-length string) — the empty case is a legitimate
    fall-back to Compose's ``5433`` default because
    ``${AWF_POSTGRES_HOST_PORT:-5433}`` substitutes the default only when the
    variable is unset or empty. Any other set value that
    :func:`_env_postgres_host_port` cannot honor verbatim is returned, *including
    a whitespace-only or surrounding-whitespace (padded) value*, which Compose
    interpolates verbatim into ``127.0.0.1: 5433:5432`` so that ``awf start``
    fails to publish the port. The padded-value rejection mirrors
    :func:`_env_host_work_dir`.
    """
    env = os.environ if environ is None else environ
    raw = env.get("AWF_POSTGRES_HOST_PORT")
    if not raw:
        return None
    if _env_postgres_host_port(env) is not None:
        return None
    return raw


def check_postgres_host_port_override(raw: str) -> SetupCheckResult:
    """Report a set-but-unusable ``AWF_POSTGRES_HOST_PORT`` as a startup blocker.

    Mirrors :func:`check_api_host_port_override` for Postgres. The local-service
    Compose stack publishes Postgres as
    ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``, so a non-empty value that
    is not a ``1..65535`` TCP port is used verbatim and ``awf start`` fails to
    publish the port. The readiness probe blocks on it rather than silently
    falling back to the default port and reporting the wrong port as free.
    """
    return SetupCheckResult(
        name="postgres_port",
        level=SetupCheckLevel.BLOCKED,
        summary=f"AWF_POSTGRES_HOST_PORT={raw!r} is not a valid TCP port.",
        detail="AWF_POSTGRES_HOST_PORT must be an integer between 1 and 65535. The local-service "
        "Compose stack publishes Postgres as 127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432, so "
        "this value is used verbatim and awf start fails to publish the port.",
        fix="Set AWF_POSTGRES_HOST_PORT to an integer between 1 and 65535, or unset it to use the "
        "default 5433, then re-run awf setup --dry-run.",
        data={"port": None, "available": False, "env_value": raw},
    )


def check_host_port_conflict(api_port: int, postgres_port: int) -> SetupCheckResult | None:
    """Block when the API and Postgres host ports resolve to the same value.

    The local-service Compose stack publishes the API on every interface
    (``${AWF_API_HOST_PORT:-8000}:8000`` -> ``0.0.0.0``) and Postgres on loopback
    (``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``). :func:`check_ports` and
    :func:`check_postgres_port` each bind *and release* their port before the
    other runs, so when both resolve to the same value each still reports the port
    free -- neither holds it while the other probes. ``awf start`` instead asks
    Docker to reserve both host ports at once, and a wildcard ``0.0.0.0``
    reservation always conflicts with a ``127.0.0.1`` reservation on the same
    port, so Docker refuses to publish both and start fails.

    Returns ``None`` when the two ports differ (the common case -- no extra
    readiness line is emitted) and a BLOCKED result when they collide, closing the
    dry-run-passes / start-fails gap the independent single-port probes leave open.
    """
    if api_port != postgres_port:
        return None
    return SetupCheckResult(
        name="port_conflict",
        level=SetupCheckLevel.BLOCKED,
        summary=f"API and Postgres host ports both resolve to {api_port}.",
        detail=(
            f"The local-service Compose stack publishes the API on 0.0.0.0:{api_port} "
            "(${AWF_API_HOST_PORT:-8000}:8000) and Postgres on "
            f"127.0.0.1:{postgres_port} (127.0.0.1:${{AWF_POSTGRES_HOST_PORT:-5433}}:5432). The "
            "host-port probes bind and release each port independently, so both pass in "
            "isolation, but awf start asks Docker to reserve both host ports at once and a "
            "wildcard 0.0.0.0 reservation conflicts with a 127.0.0.1 reservation on the same "
            "port, so Docker refuses to publish both and start fails."
        ),
        fix="Set AWF_API_HOST_PORT and AWF_POSTGRES_HOST_PORT to different ports (the defaults "
        "are 8000 and 5433), then re-run awf setup --dry-run.",
        data={"api_port": api_port, "postgres_port": postgres_port, "conflict": True},
    )


def check_ollama_bridge_api_port_conflict(
    api_port: int, bridge_listen_port: int
) -> SetupCheckResult | None:
    """Block when the API and ollama-bridge host ports resolve to the same value.

    The local-service Compose stack publishes the API on every interface
    (``${AWF_API_HOST_PORT:-8000}:8000`` -> ``0.0.0.0``), and the optional
    ``ollama-bridge`` profile runs a host-networking socat that binds
    ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``
    directly on the host. ``awf service bootstrap`` brings the bridge up *before*
    it publishes the API (the ``ollama_bridge`` stage precedes ``api_worker``), so
    socat holds the listen port first; a wildcard ``0.0.0.0`` reservation overlaps
    every specific address on the same port, so when the two ports match Docker
    cannot publish the API and ``awf start`` fails. :func:`check_ports` and
    :func:`check_ollama_bridge_listen_port` each validate their port in isolation,
    so neither catches the collision -- this cross-check closes that
    dry-run-passes / start-fails gap, mirroring :func:`check_host_port_conflict`
    for the API/Postgres pair.

    The comparison is intentionally port-only: the API side is *always* the
    wildcard ``0.0.0.0`` publish (the Compose mapping carries no host IP), which
    overlaps the bridge's bind address whatever it resolves to, so no address
    comparison is needed for soundness. (The Postgres pair is left to
    :func:`check_host_port_conflict`; Postgres and the bridge bind two distinct
    *specific* addresses by default, so they would not collide on a shared port.)

    Returns ``None`` when the two ports differ (the common case -- the bridge
    defaults to 11434 and the API to 8000 -- so no extra readiness line is
    emitted) and a BLOCKED result when they collide.
    """
    if api_port != bridge_listen_port:
        return None
    return SetupCheckResult(
        name="ollama_bridge_port_conflict",
        level=SetupCheckLevel.BLOCKED,
        summary=f"API and ollama-bridge host ports both resolve to {api_port}.",
        detail=(
            f"The local-service Compose stack publishes the API on 0.0.0.0:{api_port} "
            "(${AWF_API_HOST_PORT:-8000}:8000) and, with COMPOSE_PROFILES=ollama-bridge, runs a "
            f"host-networking socat that binds host port {bridge_listen_port} "
            "(${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}). "
            "awf service bootstrap starts the bridge before it publishes the API, so socat holds "
            "the port first, and a wildcard 0.0.0.0 reservation conflicts with any specific-address "
            "reservation on the same port, so Docker refuses to publish the API and start fails. The "
            "single-port probes bind and release each port independently, so both pass in isolation."
        ),
        fix="Set AWF_API_HOST_PORT and AWF_OLLAMA_BRIDGE_LISTEN_PORT to different ports (the "
        "defaults are 8000 and 11434), then re-run awf setup --dry-run.",
        data={
            "api_port": api_port,
            "ollama_bridge_listen_port": bridge_listen_port,
            "conflict": True,
        },
    )


def check_ollama_bridge_postgres_port_conflict(
    postgres_port: int, bridge_listen_port: int, bridge_bind_address: str
) -> SetupCheckResult | None:
    """Block when the ollama-bridge binds Postgres's loopback on a shared host port.

    The local-service Compose stack publishes Postgres on the IPv4 loopback
    (``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``), and the optional
    ``ollama-bridge`` profile runs a host-networking socat that binds
    ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``
    directly on the host. ``awf service bootstrap`` brings ``postgres`` up *before*
    ``ollama_bridge``, so Docker reserves the Postgres loopback port first; socat
    then fails to bind that same loopback port and ``awf start`` fails.
    :func:`check_postgres_port` and :func:`check_ollama_bridge_listen_port` each
    validate their port in isolation, so neither catches the collision -- this
    cross-check closes that dry-run-passes / start-fails gap, mirroring
    :func:`check_ollama_bridge_api_port_conflict` for the API/bridge pair.

    Unlike the API/bridge cross-check -- where the API side is *always* the
    wildcard ``0.0.0.0`` publish, so a shared port collides regardless of address
    -- both Postgres and the bridge bind *specific* addresses here, so the
    comparison is address-aware: a conflict is reported only when the ports match
    *and* the resolved bridge bind address overlaps Postgres's ``127.0.0.1``
    loopback (the literal ``127.0.0.1`` or the IPv4 wildcard ``0.0.0.0``). The
    default bridge bind (``172.17.0.1``, the docker0 gateway) is a distinct address
    that never collides, so the common case emits no readiness line. The bind
    address is matched verbatim and never DNS-resolved (mirroring
    :func:`check_ollama_bridge_bind_address`), so a hostname or IPv6 form that
    happens to resolve to loopback is left unflagged rather than risk a
    DNS-dependent false positive.

    Returns ``None`` when the ports differ or the bridge binds a non-overlapping
    address, and a BLOCKED result when they collide.
    """
    if (
        postgres_port != bridge_listen_port
        or bridge_bind_address not in _BRIDGE_BIND_ADDRESSES_OVERLAPPING_POSTGRES
    ):
        return None
    return SetupCheckResult(
        name="ollama_bridge_postgres_port_conflict",
        level=SetupCheckLevel.BLOCKED,
        summary=(
            f"ollama-bridge bind {bridge_bind_address}:{bridge_listen_port} collides with "
            f"Postgres on {_POSTGRES_HOST_BIND_ADDRESS}:{postgres_port}."
        ),
        detail=(
            f"The local-service Compose stack publishes Postgres on "
            f"{_POSTGRES_HOST_BIND_ADDRESS}:{postgres_port} "
            "(127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432) and, with "
            "COMPOSE_PROFILES=ollama-bridge, runs a host-networking socat that binds "
            f"{bridge_bind_address}:{bridge_listen_port} "
            "(${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}). "
            "awf service bootstrap starts postgres before the bridge, so Docker reserves the "
            "Postgres loopback port first and socat cannot bind the same loopback port (a literal "
            "127.0.0.1 match or a 0.0.0.0 wildcard that overlaps it), so awf start fails to publish "
            "the bridge. The single-port probes bind and release each port independently, so both "
            "pass in isolation."
        ),
        fix=(
            "Set AWF_OLLAMA_BRIDGE_LISTEN_PORT and AWF_POSTGRES_HOST_PORT to different ports (the "
            "defaults are 11434 and 5433), or set AWF_OLLAMA_BRIDGE_BIND_ADDRESS to an address that "
            "does not overlap Postgres's 127.0.0.1 loopback (the default 172.17.0.1), then re-run "
            "awf setup --dry-run."
        ),
        data={
            "postgres_port": postgres_port,
            "ollama_bridge_listen_port": bridge_listen_port,
            "bridge_bind_address": bridge_bind_address,
            "conflict": True,
        },
    )


def _ollama_bridge_profile_enabled(environ: Mapping[str, str]) -> bool:
    """Return whether the optional ``ollama-bridge`` Compose profile is active.

    Mirrors ``awf.service.bootstrap._compose_profile_enabled`` (the single source
    that decides whether ``awf start`` appends the ``ollama_bridge`` bootstrap
    stage), so readiness validates the bridge bind exactly when start would
    publish it. ``COMPOSE_PROFILES`` is a comma- *or* whitespace-separated list,
    read from the same merged service env ``run_system_checks`` already receives
    (the setup CLI feeds it ``local_service_environ``), so a profile set in
    root ``.env`` is honored. Re-implemented here rather than imported
    from ``service.bootstrap`` to avoid coupling host-setup readiness to a private
    bootstrap symbol.
    """
    _, raw = env_lookup(environ, "COMPOSE_PROFILES")
    return "ollama-bridge" in {
        item.strip() for chunk in raw.split(",") for item in chunk.split() if item.strip()
    }


def _env_ollama_bridge_listen_port(environ: Mapping[str, str]) -> int | None:
    """Return a usable ``AWF_OLLAMA_BRIDGE_LISTEN_PORT`` override, or ``None``.

    Mirrors :func:`_env_postgres_host_port`: a missing, empty, whitespace-only,
    surrounding-whitespace (padded), non-ASCII-decimal (``11_434``/``+11434``/
    Unicode-digit), or out-of-range value yields ``None`` because Compose
    interpolates ``TCP-LISTEN:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}`` verbatim
    into the bridge's socat command and ``awf start`` cannot honor a value the
    socat option parser rejects. Rejecting non-ASCII-decimal before ``int`` keeps
    that parse total (no dead error branch), exactly as the Postgres helper does.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_LISTEN_PORT")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    parsed = int(candidate)
    if not 1 <= parsed <= 65535:
        return None
    return parsed


def _invalid_ollama_bridge_listen_port_override(environ: Mapping[str, str]) -> str | None:
    """Return the raw ``AWF_OLLAMA_BRIDGE_LISTEN_PORT`` when set to an unusable value.

    Mirrors :func:`_invalid_postgres_host_port_override`. ``None`` when the
    override is unset or *genuinely empty* (a zero-length string is a legitimate
    fall-back to Compose's ``11434`` default, matching
    ``${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``). Any other set-but-unhonorable
    value -- including a whitespace-only or padded one Compose interpolates
    verbatim -- is returned so readiness can block on it.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_LISTEN_PORT")
    if not raw:
        return None
    if _env_ollama_bridge_listen_port(environ) is not None:
        return None
    return raw


def _env_ollama_bridge_target_port(environ: Mapping[str, str]) -> int | None:
    """Return a usable ``AWF_OLLAMA_BRIDGE_TARGET_PORT`` override, or ``None``.

    Mirrors :func:`_env_ollama_bridge_listen_port` for the *upstream* half of the
    bridge. The socat command's second endpoint is
    ``TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}``,
    so a missing, empty, whitespace-only, surrounding-whitespace (padded),
    non-ASCII-decimal (``11_434``/``+11434``/Unicode-digit), or out-of-range value
    yields ``None`` because Compose interpolates it verbatim into that TCP target
    and ``awf start`` cannot honor a value the socat option parser rejects.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_TARGET_PORT")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    parsed = int(candidate)
    if not 1 <= parsed <= 65535:
        return None
    return parsed


def _invalid_ollama_bridge_target_port_override(environ: Mapping[str, str]) -> str | None:
    """Return the raw ``AWF_OLLAMA_BRIDGE_TARGET_PORT`` when set to an unusable value.

    Mirrors :func:`_invalid_ollama_bridge_listen_port_override` for the target
    port. ``None`` when the override is unset or *genuinely empty* (a zero-length
    string is a legitimate fall-back to Compose's ``11434`` default, matching
    ``${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}``). Any other set-but-unhonorable
    value Compose interpolates verbatim is returned so readiness can block on it.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_TARGET_PORT")
    if not raw:
        return None
    if _env_ollama_bridge_target_port(environ) is not None:
        return None
    return raw


def _invalid_ollama_bridge_bind_address(environ: Mapping[str, str]) -> str | None:
    """Return the raw ``AWF_OLLAMA_BRIDGE_BIND_ADDRESS`` when set to an unusable value.

    Compose interpolates the value verbatim into the bridge's socat option list
    ``TCP-LISTEN:<port>,bind=<addr>,fork,reuseaddr`` inside a single YAML command
    argument, so any whitespace (which splits the socat argument) or comma (which
    terminates the ``bind=`` option) yields a broken command ``awf start`` cannot
    run. ``None`` when the override is unset or empty (a legitimate fall-back to
    Compose's ``172.17.0.1`` default, matching
    ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}``). The value is intentionally
    *not* parsed as an IP -- a bare IP, another docker-bridge address, or a
    resolvable hostname are all legitimate -- so only the verbatim-interpolation
    hazards (whitespace, comma) are rejected, keeping AWF core generic.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_BIND_ADDRESS")
    if not raw:
        return None
    if any(char.isspace() for char in raw) or "," in raw:
        return raw
    return None


def _invalid_ollama_bridge_target_host(environ: Mapping[str, str]) -> str | None:
    """Return the raw ``AWF_OLLAMA_BRIDGE_TARGET_HOST`` when set to an unusable value.

    Companion to :func:`_invalid_ollama_bridge_bind_address` for the *upstream*
    half of the bridge. Compose interpolates the value verbatim into socat's
    second endpoint ``TCP:<host>:<port>`` inside a single YAML command argument,
    so any whitespace (which leaves an unresolvable host such as ``foo bar``) or
    comma (which socat reads as the option separator, truncating the host and
    corrupting the address) yields a target ``awf start`` cannot parse or connect
    to. ``None`` when the override is unset or empty (a legitimate fall-back to
    Compose's ``127.0.0.1`` default, matching
    ``${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}``). Like the bind-address guard
    the value is intentionally *not* parsed as an IP -- a bare IP, a loopback
    address, or a resolvable hostname are all legitimate -- so only the
    verbatim-interpolation hazards (whitespace, comma) are rejected, keeping AWF
    core generic.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_TARGET_HOST")
    if not raw:
        return None
    if any(char.isspace() for char in raw) or "," in raw:
        return raw
    return None


def check_ollama_bridge_listen_port(
    environ: Mapping[str, str] | None = None,
) -> SetupCheckResult | None:
    """Validate the ollama-bridge listen port when that Compose profile is active.

    Returns ``None`` when the optional ``ollama-bridge`` profile is *not* enabled
    -- ``awf start`` never appends the bridge stage, so there is nothing to
    validate and no readiness line is emitted (mirroring
    :func:`check_host_port_conflict`'s not-applicable ``None``). When the profile
    *is* active, the local-service Compose stack publishes the bridge via host
    networking, binding
    ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``;
    a set-but-unusable ``AWF_OLLAMA_BRIDGE_LISTEN_PORT`` is interpolated verbatim
    into the socat command and ``awf start`` fails to publish the bridge, so this
    blocks rather than letting ``awf setup --dry-run`` report a false success.

    This is deterministic, I/O-free validation only -- it does **not** bind-probe
    the port for occupancy. The bridge binds the docker0 gateway (``172.17.0.1``)
    via host networking, which does not exist on the host until Docker creates the
    bridge, so a first-run bind probe (``awf setup`` commonly runs before Docker
    is up) would fail with ``EADDRNOTAVAIL`` and emit misleading noise; occupancy
    is left to start time.
    """
    env = os.environ if environ is None else environ
    if not _ollama_bridge_profile_enabled(env):
        return None
    invalid = _invalid_ollama_bridge_listen_port_override(env)
    if invalid is not None:
        return SetupCheckResult(
            name="ollama_bridge_port",
            level=SetupCheckLevel.BLOCKED,
            summary=f"AWF_OLLAMA_BRIDGE_LISTEN_PORT={invalid!r} is not a valid TCP port.",
            detail="AWF_OLLAMA_BRIDGE_LISTEN_PORT must be an integer between 1 and 65535. With "
            "COMPOSE_PROFILES=ollama-bridge the local-service Compose stack interpolates it "
            "verbatim into the bridge's socat command "
            "(TCP-LISTEN:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434},bind=...), so this value makes "
            "awf start fail to publish the bridge.",
            fix="Set AWF_OLLAMA_BRIDGE_LISTEN_PORT to an integer between 1 and 65535, or unset it "
            "to use the default 11434, then re-run awf setup --dry-run.",
            data={"port": None, "available": False, "env_value": invalid},
        )
    resolved = _env_ollama_bridge_listen_port(env) or DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT
    return SetupCheckResult(
        name="ollama_bridge_port",
        level=SetupCheckLevel.OK,
        summary=f"Ollama bridge listen port {resolved} is a valid TCP port.",
        detail=f"COMPOSE_PROFILES enables ollama-bridge, which binds host port {resolved}; the "
        "configured AWF_OLLAMA_BRIDGE_LISTEN_PORT is a usable value (occupancy is checked at "
        "start time, not probed here).",
        data={"port": resolved, "available": True},
    )


def check_ollama_bridge_bind_address(
    environ: Mapping[str, str] | None = None,
) -> SetupCheckResult | None:
    """Validate the ollama-bridge bind address when that Compose profile is active.

    Companion to :func:`check_ollama_bridge_listen_port` for the address half of
    the bridge bind. Returns ``None`` when the ``ollama-bridge`` profile is
    inactive. When active, a set ``AWF_OLLAMA_BRIDGE_BIND_ADDRESS`` containing
    whitespace or a comma is interpolated verbatim into the socat option list
    ``...,bind=<addr>,fork,reuseaddr`` and corrupts the command, so readiness
    blocks instead of reporting a false success. The address is not parsed as an
    IP (a bare IP, another docker-bridge address, or a resolvable hostname are all
    valid); only the verbatim-interpolation hazards are rejected.
    """
    env = os.environ if environ is None else environ
    if not _ollama_bridge_profile_enabled(env):
        return None
    invalid = _invalid_ollama_bridge_bind_address(env)
    if invalid is not None:
        return SetupCheckResult(
            name="ollama_bridge_bind_address",
            level=SetupCheckLevel.BLOCKED,
            summary=f"AWF_OLLAMA_BRIDGE_BIND_ADDRESS={invalid!r} is not a usable bind address.",
            detail="With COMPOSE_PROFILES=ollama-bridge the local-service Compose stack "
            "interpolates AWF_OLLAMA_BRIDGE_BIND_ADDRESS verbatim into the bridge's socat option "
            "list (...,bind=${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1},...), so a value with "
            "whitespace or a comma corrupts the command and awf start fails to publish the bridge.",
            fix="Set AWF_OLLAMA_BRIDGE_BIND_ADDRESS to a whitespace- and comma-free host address "
            "(an IP such as 172.17.0.1 or a resolvable hostname), or unset it to use the default "
            "172.17.0.1, then re-run awf setup --dry-run.",
            data={"address": None, "available": False, "env_value": invalid},
        )
    resolved = env.get("AWF_OLLAMA_BRIDGE_BIND_ADDRESS") or DEFAULT_OLLAMA_BRIDGE_BIND_ADDRESS
    return SetupCheckResult(
        name="ollama_bridge_bind_address",
        level=SetupCheckLevel.OK,
        summary=f"Ollama bridge bind address {resolved!r} is a usable literal.",
        detail=f"COMPOSE_PROFILES enables ollama-bridge, which binds {resolved} via host "
        "networking; the configured AWF_OLLAMA_BRIDGE_BIND_ADDRESS has no whitespace or comma "
        "that would corrupt the socat command (reachability is left to start time).",
        data={"address": resolved, "available": True},
    )


def check_ollama_bridge_target_port(
    environ: Mapping[str, str] | None = None,
) -> SetupCheckResult | None:
    """Validate the ollama-bridge upstream target port when that profile is active.

    Companion to :func:`check_ollama_bridge_listen_port` for the *upstream* half
    of the bridge. Returns ``None`` when the optional ``ollama-bridge`` profile is
    inactive. When active, the local-service Compose stack passes socat a second
    endpoint
    ``TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}``;
    a set-but-unusable ``AWF_OLLAMA_BRIDGE_TARGET_PORT`` is interpolated verbatim
    into that TCP target and ``awf start`` fails to publish the bridge, so this
    blocks rather than letting ``awf setup --dry-run`` declare an enabled bridge
    ready when the container command cannot connect to the configured target.

    Like the listen-port check this is deterministic, I/O-free validation only --
    it does **not** probe whether anything is actually listening on the target
    (reachability is left to start time, not asserted here).
    """
    env = os.environ if environ is None else environ
    if not _ollama_bridge_profile_enabled(env):
        return None
    invalid = _invalid_ollama_bridge_target_port_override(env)
    if invalid is not None:
        return SetupCheckResult(
            name="ollama_bridge_target_port",
            level=SetupCheckLevel.BLOCKED,
            summary=f"AWF_OLLAMA_BRIDGE_TARGET_PORT={invalid!r} is not a valid TCP port.",
            detail="AWF_OLLAMA_BRIDGE_TARGET_PORT must be an integer between 1 and 65535. With "
            "COMPOSE_PROFILES=ollama-bridge the local-service Compose stack interpolates it "
            "verbatim into the bridge's socat TCP target "
            "(TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}), "
            "so this value makes awf start fail to publish the bridge.",
            fix="Set AWF_OLLAMA_BRIDGE_TARGET_PORT to an integer between 1 and 65535, or unset it "
            "to use the default 11434, then re-run awf setup --dry-run.",
            data={"port": None, "available": False, "env_value": invalid},
        )
    resolved = _env_ollama_bridge_target_port(env) or DEFAULT_OLLAMA_BRIDGE_TARGET_PORT
    return SetupCheckResult(
        name="ollama_bridge_target_port",
        level=SetupCheckLevel.OK,
        summary=f"Ollama bridge target port {resolved} is a valid TCP port.",
        detail=f"COMPOSE_PROFILES enables ollama-bridge, which forwards to upstream port "
        f"{resolved}; the configured AWF_OLLAMA_BRIDGE_TARGET_PORT is a usable value "
        "(reachability is checked at start time, not probed here).",
        data={"port": resolved, "available": True},
    )


def check_ollama_bridge_target_host(
    environ: Mapping[str, str] | None = None,
) -> SetupCheckResult | None:
    """Validate the ollama-bridge upstream target host when that profile is active.

    Companion to :func:`check_ollama_bridge_bind_address` for the *host* half of
    the bridge's socat TCP target. Returns ``None`` when the ``ollama-bridge``
    profile is inactive. When active, a set ``AWF_OLLAMA_BRIDGE_TARGET_HOST``
    containing whitespace or a comma is interpolated verbatim into socat's second
    endpoint ``TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:...`` and yields a
    target ``awf start`` cannot parse or connect to, so readiness blocks instead
    of reporting a false success. The host is not parsed as an IP (a bare IP, a
    loopback address, or a resolvable hostname are all valid); only the
    verbatim-interpolation hazards are rejected, and reachability is left to
    start time.
    """
    env = os.environ if environ is None else environ
    if not _ollama_bridge_profile_enabled(env):
        return None
    invalid = _invalid_ollama_bridge_target_host(env)
    if invalid is not None:
        return SetupCheckResult(
            name="ollama_bridge_target_host",
            level=SetupCheckLevel.BLOCKED,
            summary=f"AWF_OLLAMA_BRIDGE_TARGET_HOST={invalid!r} is not a usable target host.",
            detail="With COMPOSE_PROFILES=ollama-bridge the local-service Compose stack "
            "interpolates AWF_OLLAMA_BRIDGE_TARGET_HOST verbatim into the bridge's socat TCP "
            "target "
            "(TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}), "
            "so a value with whitespace or a comma corrupts the address and awf start fails to "
            "publish the bridge.",
            fix="Set AWF_OLLAMA_BRIDGE_TARGET_HOST to a whitespace- and comma-free host address "
            "(an IP such as 127.0.0.1 or a resolvable hostname), or unset it to use the default "
            "127.0.0.1, then re-run awf setup --dry-run.",
            data={"host": None, "available": False, "env_value": invalid},
        )
    resolved = env.get("AWF_OLLAMA_BRIDGE_TARGET_HOST") or DEFAULT_OLLAMA_BRIDGE_TARGET_HOST
    return SetupCheckResult(
        name="ollama_bridge_target_host",
        level=SetupCheckLevel.OK,
        summary=f"Ollama bridge target host {resolved!r} is a usable literal.",
        detail=f"COMPOSE_PROFILES enables ollama-bridge, which forwards to upstream host "
        f"{resolved} via socat; the configured AWF_OLLAMA_BRIDGE_TARGET_HOST has no whitespace "
        "or comma that would corrupt the socat target (reachability is left to start time).",
        data={"host": resolved, "available": True},
    )
