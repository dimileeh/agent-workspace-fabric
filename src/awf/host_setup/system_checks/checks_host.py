"""Work-dir, host-home, auth-mount, and required-service-env readiness checks.

``AWF_HOST_WORK_DIR`` and ``AWF_HOST_HOME`` (and their ``${HOME}`` fall-backs)
feed Compose's verbatim ``${VAR}`` interpolation, so a relative, ``~``-prefixed,
or whitespace-padded value passes an isolated probe yet makes ``awf start`` fail
to mount. These checks block on those traps before declaring the machine ready.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from awf.host_setup.system_checks.primitives import (
    MIN_FREE_DISK_BYTES,
    SetupCheckLevel,
    SetupCheckResult,
    _safe_expanduser,
)


def _env_host_work_dir(environ: Mapping[str, str]) -> str | None:
    """Return a usable ``AWF_HOST_WORK_DIR`` override, or ``None`` when unusable.

    A missing, empty, whitespace-only, *surrounding-whitespace* (padded), *or
    non-absolute* (relative or ``~``-prefixed) value yields ``None``. The
    override is "usable" only when AWF can honor it identically across every
    layer, and these values cannot be:

    * A value with leading/trailing whitespace: the readiness probe would
      ``strip`` it, but Compose interpolates ``${AWF_HOST_WORK_DIR}`` verbatim
      and ``awf service``'s ``_resolve_service_work_dir`` returns it
      *unstripped*, so a padded ``" /data/awf"`` would pass disk readiness for
      the stripped ``/data/awf`` while ``awf start`` mounts (and the service
      resolves) the spaced path.
    * A non-absolute value such as ``data/awf`` or ``~/.awf/service``: the
      local-service Compose file uses ``${AWF_HOST_WORK_DIR}`` as *both* the bind
      source and the mount target (``docker/compose/local-service.yml``), and
      Docker's mount target must be an absolute path. Neither Compose nor
      ``_resolve_service_work_dir`` expands a leading ``~`` or resolves a
      relative path, so the readiness probe — which *does* expand ``~`` and reads
      a relative path against the current process — would report readiness for a
      directory ``awf start`` can never mount.

    Whether that ``None`` means a legitimate fall-back to Compose's
    ``${HOME}/.awf/service`` default (only an *unset or empty* override,
    mirroring ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}``) or a startup
    blocker (a whitespace-only, padded, or non-absolute value, which Compose
    keeps as a non-empty literal and interpolates verbatim into the bind path) is
    decided by :func:`_invalid_host_work_dir_override`, which the readiness probe
    surfaces as a blocker instead of silently probing the stripped, expanded, or
    default work dir.
    """
    raw = environ.get("AWF_HOST_WORK_DIR")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    if not Path(candidate).is_absolute():
        return None
    return candidate


def _default_compose_work_dir(environ: Mapping[str, str]) -> Path:
    """Return Compose's ``${HOME}/.awf/service`` work-dir bind default.

    Mirrors the no-override side of the local-service bind source
    ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}``: Compose interpolates
    ``${HOME}`` from the same merged environment the readiness probe sees.

    Reached only after :func:`_invalid_work_dir_home_fallback` has confirmed the
    ``${HOME}`` fall-back is a usable absolute path -- a relative, ``~``-prefixed,
    whitespace-padded, *or* empty/unset ``HOME`` already blocks upstream -- so
    ``HOME`` is a non-empty absolute string here and the default resolves directly
    from it (no ``~`` expansion or normalization is left to do).

    The lookup uses ``environ.get("HOME", "")`` rather than ``environ["HOME"]`` so
    that the upstream-guard precondition is explicit, not implicit: a direct
    internal or test call via :func:`_resolve_work_dir` with a ``HOME``-less
    mapping resolves to the relative ``.awf/service`` (the same empty-``HOME``
    treatment :func:`_invalid_home_fallback` applies) instead of raising an
    unguarded ``KeyError`` outside the structured-error path.
    """
    return Path(environ.get("HOME", "")) / ".awf" / "service"


def _invalid_home_fallback(environ: Mapping[str, str]) -> str | None:
    """Return ``HOME`` when Compose would interpolate it as a non-mountable fallback.

    Both ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` (the work-dir bind) and
    ``${AWF_HOST_HOME:-${HOME}}`` (every auth mount) fall back to ``${HOME}``
    *verbatim* when their override is unset or empty. Compose neither strips
    surrounding whitespace nor expands a leading ``~`` nor resolves a relative
    ``HOME``, and Docker's bind-mount target must be absolute, so a relative,
    ``~``-prefixed, or whitespace-padded ``HOME`` (for example ``HOME=tmp``) makes
    ``awf start`` mount a non-absolute path it can never bind -- even though the
    readiness probe would expand or normalize it before declaring the machine
    ready.

    Unlike the ``AWF_HOST_*`` overrides, ``${HOME}`` itself has *no* ``:-``
    default, so an unset or empty ``HOME`` is **not** a legitimate fall-back:
    Compose substitutes nothing, anchoring the bind at the filesystem root
    (``${HOME}/.awf/service`` -> ``/.awf/service``, ``${HOME}/.config/gh`` ->
    ``/.config/gh``) while the readiness probe expands ``~`` to the account home.
    That divergence is the same dry-run-passes / start-mounts-the-wrong-directory
    trap the other ``HOME`` shapes hit, so the empty/unset case must block too.

    Returns ``None`` only when ``HOME`` is an absolute path with no surrounding
    whitespace (the sole usable fall-back). For an unset or empty ``HOME`` it
    returns the empty string ``""`` (the root-anchored marker the ``check_*``
    fallbacks render distinctly); for a relative, ``~``-prefixed, or
    whitespace-padded ``HOME`` it returns the raw value.
    """
    raw = environ.get("HOME")
    if not raw:
        return ""
    candidate = raw.strip()
    if candidate and candidate == raw and Path(candidate).is_absolute():
        return None
    return raw


def _resolve_work_dir(
    *,
    work_dir: Path | None,
    environ: Mapping[str, str] | None,
) -> Path:
    """Resolve which directory the disk readiness probe should inspect.

    Precedence mirrors the path the local-service Compose stack actually mounts
    (``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}``): an explicit caller override
    wins, then the ``AWF_HOST_WORK_DIR`` environment override that Compose
    bind-mounts and that the running service resolves as its work_dir, and
    finally Compose's built-in ``${HOME}/.awf/service`` default when no override
    is set. Honoring the env override keeps ``awf setup`` from reporting disk
    readiness for the wrong directory when an operator points the stack at a
    custom host work dir via the shell or root ``.env``.

    The persisted ``config.work_dir`` is deliberately *not* consulted here.
    ``awf start`` bind-mounts ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` from
    the resolved Compose env and never reads ``HostSetupConfig``; nothing
    propagates ``config.work_dir`` into the Compose env. Probing it would report
    disk readiness for a directory ``awf start`` would never mount whenever an
    operator set a non-default ``config.work_dir`` without also exporting
    ``AWF_HOST_WORK_DIR`` (the same divergence already fixed for the API port).
    """
    if work_dir is not None:
        return work_dir
    env = os.environ if environ is None else environ
    override = _env_host_work_dir(env)
    if override is not None:
        return _safe_expanduser(override)
    return _default_compose_work_dir(env)


def _invalid_host_work_dir_override(
    *,
    work_dir: Path | None,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return the raw ``AWF_HOST_WORK_DIR`` when it is set to an unusable value.

    Returns ``None`` (no configuration error) when an explicit caller
    ``work_dir`` wins, when the override is unset, or when it is *genuinely
    empty* (a zero-length string) — the empty case is a legitimate fall-back to
    Compose's ``${HOME}/.awf/service`` default because
    ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` substitutes the default only
    when the variable is unset or empty. A whitespace-only, *surrounding-
    whitespace* (padded), *or non-absolute* (relative or ``~``-prefixed) value is
    returned instead: Compose treats ``"   "``, ``" /data/awf"``, ``data/awf``,
    and ``~/.awf/service`` as non-empty literals and interpolates them verbatim
    into the bind source/target, and ``awf service`` resolves the same override
    as its ``work_dir`` (``_resolve_service_work_dir`` returns it unstripped and
    unexpanded). Docker's mount target must be absolute, so ``awf start`` mounts
    (or fails on) that exact path rather than the stripped, expanded, or default
    one. The readiness probe must block on it instead of silently probing the
    stripped/expanded/default work dir and reporting readiness for the wrong
    directory. The ``not raw`` guard mirrors the same empty-vs-whitespace split
    as the API-port override so the two layers agree.
    """
    if work_dir is not None:
        return None
    env = os.environ if environ is None else environ
    raw = env.get("AWF_HOST_WORK_DIR")
    if not raw:
        return None
    if _env_host_work_dir(env) is not None:
        return None
    return raw


def check_host_work_dir_override(raw: str) -> SetupCheckResult:
    """Report a set-but-unusable ``AWF_HOST_WORK_DIR`` as a startup blocker.

    The local-service Compose stack uses ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}``
    as *both* the bind source and the mount target and ``awf service`` resolves
    the same override as its work_dir, both verbatim, so two classes of value are
    used as the bind path exactly as written rather than as the readiness probe
    would normalize them:

    * whitespace-only or surrounding-whitespace values, which Compose keeps
      unstripped; and
    * non-absolute values (a relative path or a leading ``~``), which Docker
      rejects because a mount target must be absolute and neither Compose nor
      ``awf service`` expands ``~`` or resolves a relative path.

    Either way ``awf start`` mounts (or fails on) the literal value instead of
    the stripped/expanded path the readiness probe would otherwise report, so the
    probe blocks rather than reporting readiness for a directory that is never
    mounted.
    """
    candidate = raw.strip()
    if candidate and candidate == raw:
        # Non-empty with no surrounding whitespace, but not an absolute path: a
        # relative path or a leading ``~`` Compose/awf service keep verbatim.
        summary = f"AWF_HOST_WORK_DIR={raw!r} is not an absolute path, not a usable work directory."
        detail = (
            "AWF_HOST_WORK_DIR must be an absolute directory path. The local-service Compose "
            "stack uses ${AWF_HOST_WORK_DIR:-${HOME}/.awf/service} as both the bind source and "
            "the mount target, and awf service resolves the same override as its work_dir — all "
            "verbatim. Docker's bind mount target must be absolute and neither Compose nor awf "
            "service expands a leading ~ or resolves a relative path, so awf start fails to mount "
            "this value even though the readiness probe could resolve it (expanding ~ or reading "
            "it relative to the current process)."
        )
        fix = (
            "Set AWF_HOST_WORK_DIR to an absolute directory path (for example "
            "/home/you/.awf/service rather than ~/.awf/service or data/awf), or unset it to use "
            "the default ${HOME}/.awf/service, then re-run awf setup --dry-run."
        )
    else:
        summary = (
            f"AWF_HOST_WORK_DIR={raw!r} has leading or trailing whitespace, "
            "not a usable work directory."
        )
        detail = (
            "AWF_HOST_WORK_DIR must be a real directory path with no surrounding whitespace. "
            "The local-service Compose stack bind-mounts ${AWF_HOST_WORK_DIR:-${HOME}/.awf/service} "
            "and awf service resolves the same override as its work_dir, so this value is used "
            "verbatim — with its surrounding whitespace — as the bind path and awf start mounts (or "
            "fails on) it instead of the stripped path the readiness probe would otherwise report."
        )
        fix = (
            "Set AWF_HOST_WORK_DIR to a real directory path with no leading or trailing "
            "whitespace, or unset it to use the default ${HOME}/.awf/service, then re-run "
            "awf setup --dry-run."
        )
    return SetupCheckResult(
        name="disk",
        level=SetupCheckLevel.BLOCKED,
        summary=summary,
        detail=detail,
        fix=fix,
        data={
            "path": None,
            "free_bytes": None,
            "minimum_bytes": MIN_FREE_DISK_BYTES,
            "env_value": raw,
        },
    )


def _invalid_work_dir_home_fallback(
    *,
    work_dir: Path | None,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return ``HOME`` when the work-dir default would fall back to an unusable ``HOME``.

    Only relevant when neither an explicit ``work_dir`` nor a usable
    ``AWF_HOST_WORK_DIR`` override wins, so the local-service Compose stack
    resolves the bind from ``${HOME}/.awf/service``. A set-but-unusable
    ``AWF_HOST_WORK_DIR`` is already surfaced by
    :func:`_invalid_host_work_dir_override`, which runs first; reaching here with
    no usable override (``_env_host_work_dir`` returns ``None``) therefore means
    the variable is unset or empty and Compose interpolates ``${HOME}`` verbatim,
    so an unusable ``HOME`` must block instead of probing the normalized default.
    """
    if work_dir is not None:
        return None
    env = os.environ if environ is None else environ
    if _env_host_work_dir(env) is not None:
        return None
    return _invalid_home_fallback(env)


def check_work_dir_home_fallback(raw_home: str) -> SetupCheckResult:
    """Report an unusable ``${HOME}`` work-dir fallback as a startup blocker.

    With ``AWF_HOST_WORK_DIR`` unset, the local-service Compose stack binds
    ``${HOME}/.awf/service`` as *both* the bind source and the (absolute-required)
    mount target, interpolating ``${HOME}`` verbatim. A relative or ``~``-prefixed
    ``HOME`` yields a non-absolute bind path Docker rejects, a
    surrounding-whitespace ``HOME`` reaches Docker unstripped, and an unset or
    empty ``HOME`` (which has no ``:-`` default of its own) makes Compose
    substitute nothing and bind ``/.awf/service`` at the filesystem root -- so in
    every case ``awf start`` mounts (or fails on) a path the readiness probe would
    otherwise normalize or expand. The probe blocks rather than reporting disk
    readiness for a directory ``awf start`` never mounts.
    """
    candidate = raw_home.strip()
    if not raw_home:
        # Unset or empty HOME: ${HOME} has no ``:-`` default, so Compose
        # substitutes nothing and anchors the bind at the filesystem root.
        summary = (
            "HOME is unset or empty, so the ${HOME}/.awf/service work dir "
            "resolves to /.awf/service at the filesystem root."
        )
        detail = (
            "With AWF_HOST_WORK_DIR unset and HOME unset or empty, the local-service Compose "
            "stack interpolates ${AWF_HOST_WORK_DIR:-${HOME}/.awf/service} as /.awf/service -- "
            "anchored at the filesystem root, not your account home. ${HOME} has no :- default "
            "of its own, so Compose substitutes nothing for an unset or empty HOME and awf start "
            "binds /.awf/service, while the readiness probe expands ~ to your account home. The "
            "probe must block rather than report disk readiness for a directory awf start never "
            "mounts."
        )
        fix = (
            "Set HOME to your absolute home directory (for example /home/you), or set "
            "AWF_HOST_WORK_DIR to an absolute work directory, then re-run awf setup --dry-run."
        )
    elif candidate and candidate == raw_home:
        # Non-empty with no surrounding whitespace, but not absolute: a relative
        # path or a leading ``~`` Compose keeps verbatim as the bind path.
        summary = (
            f"HOME={raw_home!r} is not an absolute path, so the "
            "${HOME}/.awf/service work dir is not a usable bind path."
        )
        detail = (
            "HOME must be an absolute directory path. With AWF_HOST_WORK_DIR unset, the "
            "local-service Compose stack binds ${HOME}/.awf/service as both the source and the "
            "container target, verbatim. Docker's bind mount target must be absolute and Compose "
            "does not expand a leading ~ or resolve a relative path, so awf start fails to mount "
            "the work dir even though the readiness probe could resolve it (expanding ~ or "
            "reading it relative to the current process)."
        )
        fix = (
            "Set HOME to an absolute directory path (for example /home/you rather than ~ or "
            "home/you), or set AWF_HOST_WORK_DIR to an absolute work directory, then re-run "
            "awf setup --dry-run."
        )
    else:
        summary = (
            f"HOME={raw_home!r} has leading or trailing whitespace, so the "
            "${HOME}/.awf/service work dir is not a usable bind path."
        )
        detail = (
            "HOME must be a real directory path with no surrounding whitespace. With "
            "AWF_HOST_WORK_DIR unset, the local-service Compose stack binds ${HOME}/.awf/service "
            "verbatim — with its surrounding whitespace — so awf start mounts (or fails on) the "
            "spaced path instead of the stripped path the readiness probe would otherwise report."
        )
        fix = (
            "Set HOME to a real directory path with no leading or trailing whitespace, or set "
            "AWF_HOST_WORK_DIR to an absolute work directory, then re-run awf setup --dry-run."
        )
    return SetupCheckResult(
        name="disk",
        level=SetupCheckLevel.BLOCKED,
        summary=summary,
        detail=detail,
        fix=fix,
        data={
            "path": None,
            "free_bytes": None,
            "minimum_bytes": MIN_FREE_DISK_BYTES,
            "env_value": raw_home,
        },
    )


def _invalid_host_home_override(
    *,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return the raw ``AWF_HOST_HOME`` when it is set to a value Compose can't mount.

    Returns ``None`` (no configuration error) when the override is unset or
    *genuinely empty* (a zero-length string) — ``${AWF_HOST_HOME:-${HOME}}``
    substitutes the ``${HOME}`` default only when the variable is unset or empty —
    or when it is an absolute path with no surrounding whitespace. A
    whitespace-only, *surrounding-whitespace* (padded), *or non-absolute*
    (relative or ``~``-prefixed) value is returned instead.

    The local-service Compose stack uses ``${AWF_HOST_HOME:-${HOME}}`` as *both*
    the host source and the container target for every auth mount (for example
    ``${AWF_HOST_HOME:-${HOME}}/.config/gh:${AWF_HOST_HOME:-${HOME}}/.config/gh:ro``
    in ``docker/compose/local-service.yml``). Docker requires the mount target to
    be absolute and Compose interpolates the value verbatim — no ``~`` expansion,
    no relative resolution, no stripping — so ``awf start`` fails to mount the
    auth directories even though the readiness probe could resolve the value
    (expanding ``~`` or reading it relative to the current process). The probe
    must block on it instead of reporting readiness for auth mounts that
    ``awf start`` can never bind. The ``not raw`` guard mirrors the same
    empty-vs-whitespace split as ``_invalid_host_work_dir_override`` so the two
    host-path overrides agree.
    """
    env = os.environ if environ is None else environ
    raw = env.get("AWF_HOST_HOME")
    if not raw:
        return None
    candidate = raw.strip()
    if candidate and candidate == raw and Path(candidate).is_absolute():
        return None
    return raw


def check_host_home_override(raw: str) -> SetupCheckResult:
    """Report a set-but-unusable ``AWF_HOST_HOME`` as a startup blocker.

    The local-service Compose stack uses ``${AWF_HOST_HOME:-${HOME}}`` as *both*
    the host source and the container target for every auth mount (gh, gcloud,
    git, ssh, and the agent CLI directories), verbatim. Docker requires the mount
    target to be absolute and Compose neither strips surrounding whitespace nor
    expands ``~`` nor resolves a relative path, so two classes of value reach
    Docker exactly as written rather than as the readiness probe would normalize
    them:

    * whitespace-only or surrounding-whitespace values, which Compose keeps
      unstripped; and
    * non-absolute values (a relative path or a leading ``~``), which Docker
      rejects because a mount target must be absolute.

    Either way ``awf start`` mounts (or fails on) the literal value, so the probe
    blocks rather than reporting readiness for auth mounts that are never bound.
    """
    candidate = raw.strip()
    if candidate and candidate == raw:
        # Non-empty with no surrounding whitespace, but not an absolute path: a
        # relative path or a leading ``~`` Compose mounts verbatim.
        summary = f"AWF_HOST_HOME={raw!r} is not an absolute path, not a usable auth-mount root."
        detail = (
            "AWF_HOST_HOME must be an absolute directory path. The local-service Compose "
            "stack uses ${AWF_HOST_HOME:-${HOME}} as both the host source and the container "
            "target for the auth mounts (for example "
            "${AWF_HOST_HOME:-${HOME}}/.config/gh:${AWF_HOST_HOME:-${HOME}}/.config/gh:ro), "
            "verbatim. Docker's bind mount target must be absolute and Compose does not "
            "expand a leading ~ or resolve a relative path, so awf start fails to mount the "
            "auth directories even though the readiness probe could resolve it (expanding ~ "
            "or reading it relative to the current process)."
        )
        fix = (
            "Set AWF_HOST_HOME to an absolute directory path (for example /home/you rather "
            "than ~ or home/you), or unset it to use the default ${HOME}, then re-run "
            "awf setup --dry-run."
        )
    else:
        summary = (
            f"AWF_HOST_HOME={raw!r} has leading or trailing whitespace, "
            "not a usable auth-mount root."
        )
        detail = (
            "AWF_HOST_HOME must be a real directory path with no surrounding whitespace. "
            "The local-service Compose stack bind-mounts ${AWF_HOST_HOME:-${HOME}} as both "
            "the host source and the container target for the auth mounts and interpolates "
            "it verbatim — with its surrounding whitespace — so awf start mounts (or fails "
            "on) the spaced path instead of the stripped path the readiness probe would "
            "otherwise report."
        )
        fix = (
            "Set AWF_HOST_HOME to a real directory path with no leading or trailing "
            "whitespace, or unset it to use the default ${HOME}, then re-run "
            "awf setup --dry-run."
        )
    return SetupCheckResult(
        name="host_home",
        level=SetupCheckLevel.BLOCKED,
        summary=summary,
        detail=detail,
        fix=fix,
        data={"env_value": raw},
    )


def _invalid_auth_mount_home_fallback(
    *,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return ``HOME`` when the auth mounts would fall back to an unusable ``HOME``.

    Only relevant when ``AWF_HOST_HOME`` is unset or empty, so every
    ``${AWF_HOST_HOME:-${HOME}}`` auth mount resolves to ``${HOME}`` verbatim. A
    set-but-unusable ``AWF_HOST_HOME`` is already surfaced by
    :func:`_invalid_host_home_override`, which runs first, and a set-and-usable
    one makes ``${HOME}`` irrelevant; either way a non-empty ``AWF_HOST_HOME``
    short-circuits to ``None`` here so only the genuine ``${HOME}`` fall-back is
    validated.
    """
    env = os.environ if environ is None else environ
    if env.get("AWF_HOST_HOME"):
        return None
    return _invalid_home_fallback(env)


def check_auth_mount_home_fallback(raw_home: str) -> SetupCheckResult:
    """Report an unusable ``${HOME}`` auth-mount fallback as a startup blocker.

    With ``AWF_HOST_HOME`` unset, the local-service Compose stack uses ``${HOME}``
    as *both* the host source and the (absolute-required) container target for
    every auth mount (gh, gcloud, git, ssh, and the agent CLI directories),
    verbatim. A relative or ``~``-prefixed ``HOME`` yields a non-absolute mount
    target Docker rejects, a surrounding-whitespace ``HOME`` reaches Docker
    unstripped, and an unset or empty ``HOME`` (which has no ``:-`` default of its
    own) makes Compose substitute nothing and anchor the auth mounts at the
    filesystem root (``/.config/gh``, ``/.ssh``, ...) -- so in every case
    ``awf start`` fails to mount (or binds the wrong) auth directories the
    readiness probe would otherwise normalize or expand. The probe blocks rather
    than reporting auth mounts that are never bound.
    """
    candidate = raw_home.strip()
    if not raw_home:
        # Unset or empty HOME: ${AWF_HOST_HOME:-${HOME}} resolves to nothing, so
        # every auth mount anchors at the filesystem root instead of the home dir.
        summary = (
            "HOME is unset or empty, so the ${HOME} auth mounts resolve to "
            "/.config/gh, /.ssh, ... at the filesystem root."
        )
        detail = (
            "With AWF_HOST_HOME unset and HOME unset or empty, the local-service Compose stack "
            "interpolates ${AWF_HOST_HOME:-${HOME}} as nothing, so every auth mount (for example "
            "${AWF_HOST_HOME:-${HOME}}/.config/gh) resolves to a root-anchored path such as "
            "/.config/gh -- not the directories under your account home. ${HOME} has no :- "
            "default of its own, so awf start binds those root paths while the readiness probe "
            "expands ~ to your account home. The probe must block rather than report auth mounts "
            "awf start never binds."
        )
        fix = (
            "Set HOME to your absolute home directory (for example /home/you), or set "
            "AWF_HOST_HOME to an absolute directory path, then re-run awf setup --dry-run."
        )
    elif candidate and candidate == raw_home:
        # Non-empty with no surrounding whitespace, but not absolute: a relative
        # path or a leading ``~`` Compose mounts verbatim.
        summary = (
            f"HOME={raw_home!r} is not an absolute path, not a usable ${{HOME}} auth-mount root."
        )
        detail = (
            "HOME must be an absolute directory path. With AWF_HOST_HOME unset, the "
            "local-service Compose stack uses ${HOME} as both the host source and the container "
            "target for the auth mounts (for example ${HOME}/.config/gh:${HOME}/.config/gh:ro), "
            "verbatim. Docker's bind mount target must be absolute and Compose does not expand a "
            "leading ~ or resolve a relative path, so awf start fails to mount the auth "
            "directories even though the readiness probe could resolve it."
        )
        fix = (
            "Set HOME to an absolute directory path (for example /home/you rather than ~ or "
            "home/you), or set AWF_HOST_HOME to an absolute directory path, then re-run "
            "awf setup --dry-run."
        )
    else:
        summary = (
            f"HOME={raw_home!r} has leading or trailing whitespace, "
            "not a usable ${HOME} auth-mount root."
        )
        detail = (
            "HOME must be a real directory path with no surrounding whitespace. With "
            "AWF_HOST_HOME unset, the local-service Compose stack bind-mounts ${HOME} as both "
            "the host source and the container target for the auth mounts and interpolates it "
            "verbatim — with its surrounding whitespace — so awf start mounts (or fails on) the "
            "spaced path instead of the stripped path the readiness probe would otherwise report."
        )
        fix = (
            "Set HOME to a real directory path with no leading or trailing whitespace, or set "
            "AWF_HOST_HOME to an absolute directory path, then re-run awf setup --dry-run."
        )
    return SetupCheckResult(
        name="host_home",
        level=SetupCheckLevel.BLOCKED,
        summary=summary,
        detail=detail,
        fix=fix,
        data={"env_value": raw_home},
    )


def check_host_home(*, environ: Mapping[str, str] | None = None) -> SetupCheckResult:
    """Confirm the Compose auth-mount root resolves to an absolute path.

    Reached only when neither :func:`_invalid_host_home_override` nor
    :func:`_invalid_auth_mount_home_fallback` finds a blocker: ``AWF_HOST_HOME``
    is an absolute path with no surrounding whitespace, or it is unset/empty and
    the ``${HOME}`` fall-back is itself an absolute path with no surrounding
    whitespace (an unset/empty ``HOME`` now blocks, because Compose would
    substitute nothing and anchor the auth mounts at the filesystem root). Every
    ``${AWF_HOST_HOME:-${HOME}}`` auth mount therefore resolves to an absolute
    target ``awf start`` can bind.

    Like every other ``check_*`` OK result, ``data`` records the concrete value
    that was validated so JSON consumers and readiness UIs can see *which*
    auth-mount root was confirmed ready: the raw ``AWF_HOST_HOME`` override, the
    ``${HOME}`` fall-back, and the effective ``${AWF_HOST_HOME:-${HOME}}`` root
    every auth mount resolves to. The values are read from the same ``environ``
    the upstream guards consult (the resolved service env, falling back to the
    process env only when ``environ`` is ``None``), so the reported root cannot
    diverge from the one the block/OK decision was made against.
    """
    env = os.environ if environ is None else environ
    env_value = env.get("AWF_HOST_HOME")
    home = env.get("HOME")
    # ${AWF_HOST_HOME:-${HOME}}: Compose substitutes the override only when it is
    # non-empty, exactly as _invalid_host_home_override /
    # _invalid_auth_mount_home_fallback decide which value they validated.
    resolved_root = env_value if env_value else home
    # Describe the case that actually applies so the readiness summary/detail name
    # the auth-mount root that was validated rather than a static "unset or
    # absolute" disjunction. Reaching this OK result already proves resolved_root
    # is an absolute, unpadded path: a set override is the root verbatim, while an
    # unset/empty override falls back to ${HOME} (Compose treats both the same).
    if env_value:
        summary = f"AWF_HOST_HOME={env_value!r} is an absolute auth-mount root."
        detail = (
            f"AWF_HOST_HOME is set to the absolute path {env_value!r}, so the auth mounts "
            f"({env_value}/.config/gh, {env_value}/.ssh, the agent CLI directories, ...) resolve "
            "to absolute targets awf start can bind."
        )
    else:
        summary = (
            "AWF_HOST_HOME is unset or empty; the ${HOME} fallback is an absolute auth-mount root."
        )
        detail = (
            "AWF_HOST_HOME is unset or empty, so the local-service Compose stack falls back "
            f"to ${{HOME}}={home!r} (an absolute path) as the auth-mount root; the auth mounts "
            f"({home}/.config/gh, {home}/.ssh, the agent CLI directories, ...) resolve to absolute "
            "targets awf start can bind."
        )
    return SetupCheckResult(
        name="host_home",
        level=SetupCheckLevel.OK,
        summary=summary,
        detail=detail,
        data={"env_value": env_value, "home": home, "resolved_root": resolved_root},
    )


# The local-service Compose stack supplies loopback-only local defaults for
# ``AWF_API_TOKEN`` and ``AWF_POSTGRES_PASSWORD`` so a fresh source checkout can
# start with ``docker compose up --build``. This check still reports whether the
# resolved service environment carries explicit values or uses those defaults,
# without logging either value.
REQUIRED_LOCAL_SERVICE_ENV_VARS: tuple[str, ...] = (
    "AWF_API_TOKEN",
    "AWF_POSTGRES_PASSWORD",
)


def check_required_service_env(*, environ: Mapping[str, str] | None = None) -> SetupCheckResult:
    """Check local-service auth/database env without leaking secret values."""
    env = os.environ if environ is None else environ
    defaults_applied = [name for name in REQUIRED_LOCAL_SERVICE_ENV_VARS if not env.get(name)]
    if not defaults_applied:
        return SetupCheckResult(
            name="required_service_env",
            level=SetupCheckLevel.OK,
            summary="Local-service auth/database variables are set.",
            detail=(
                "AWF_API_TOKEN and AWF_POSTGRES_PASSWORD are present and non-empty in the "
                "resolved service env. Values are not read or logged."
            ),
            data={
                "checked": list(REQUIRED_LOCAL_SERVICE_ENV_VARS),
                "defaults_applied": [],
            },
        )
    return SetupCheckResult(
        name="required_service_env",
        level=SetupCheckLevel.OK,
        summary="Local-service Compose will use safe loopback-only defaults.",
        detail=(
            "Missing or empty AWF_API_TOKEN / AWF_POSTGRES_PASSWORD values are valid for the "
            "local root Compose cold-start path because the Compose stack defaults them to a "
            "local bearer token and development Postgres password while binding services to "
            "127.0.0.1. Set explicit values in root .env when you want a non-default local token "
            "or password. Values are not read or logged."
        ),
        data={
            "checked": list(REQUIRED_LOCAL_SERVICE_ENV_VARS),
            "defaults_applied": defaults_applied,
        },
    )
