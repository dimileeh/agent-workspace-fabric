"""Repeatable local service bootstrap orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict

from awf.common.audit import redact_audit_text
from awf.node.auth_mounts import force_copy_isolation_requested
from awf.service.config import (
    LOCAL_SERVICE_COMPOSE_ENV_FILE,
    LOCAL_SERVICE_COMPOSE_FILE,
    LOCAL_SERVICE_INCLUDED_COMPOSE_FILE,
    ServiceSettings,
    local_service_environ,
    resolve_local_service_compose_env_file,
)
from awf.service.environment import (
    cleared_docker_cli_client_keys,
    compose_env_file_values,
    env_lookup,
    non_empty_env_value,
)
from awf.service.status import collect_service_status

DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 180.0
DEFAULT_BOOTSTRAP_POLL_INTERVAL_SECONDS = 2.0
AGENT_RUNTIME_DOCKERFILE = Path("docker/agent-runtime.Dockerfile")
PACKAGED_BOOTSTRAP_ASSET_ROOT = Path("bootstrap_assets")

# Bootstrap failure reason codes. Exposed as named constants so consumers (e.g.
# ``awf start`` failure classification) reference one source of truth instead of
# duplicating the literal strings, which would silently drift on a rename.
SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND = "SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND"
SERVICE_BOOTSTRAP_STAGE_FAILED = "SERVICE_BOOTSTRAP_STAGE_FAILED"
SERVICE_BOOTSTRAP_TIMEOUT = "SERVICE_BOOTSTRAP_TIMEOUT"
# Work-dir mount-propagation preflight (#376/#388) reason codes.
SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED = "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED"
SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE = (
    "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"
)

# Compose interpolates these to gate the worker's ``:rshared`` work-dir bind and
# the per-workspace overlay vs copy posture (see docker/compose/local-service.yml
# and src/awf/node/auth_mounts.py).
AWF_WORK_DIR_BIND_PROPAGATION_ENV = "AWF_WORK_DIR_BIND_PROPAGATION"
AWF_CLAUDE_AUTH_FORCE_COPY_ENV = "AWF_CLAUDE_AUTH_FORCE_COPY"
AWF_WORK_DIR_PROPAGATION_TIMESTAMP_ENV = "AWF_WORK_DIR_PROPAGATION_TIMESTAMP"

# Compose binds ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` (see
# docker/compose/local-service.yml), so this is the deterministic default host
# work dir the preflight must inspect when the operator pins nothing.
DEFAULT_HOST_WORK_DIR_SUBPATH = ".awf/service"

# Filesystems where a worker-mounted overlay never propagates into the sibling
# agent container even when the bind is flagged ``:rshared`` (Docker Desktop's
# gRPC/virtio bridges, Plan 9). On these the copy fallback is the only correct
# posture, so the preflight forces it rather than provisioning an empty overlay.
_NON_PROPAGATING_FS_TYPES = frozenset({"virtiofs", "grpcfuse", "fuse.grpcfuse", "9p"})

DEFAULT_MOUNTINFO_PATH = Path("/proc/self/mountinfo")


class CompletedProcessLike(Protocol):
    @property
    def returncode(self) -> int: ...  # pragma: no cover

    @property
    def stdout(self) -> str | None: ...  # pragma: no cover

    @property
    def stderr(self) -> str | None: ...  # pragma: no cover


class SubprocessRun(Protocol):
    """Callable protocol for running bootstrap subprocess commands."""

    def __call__(
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        env: Mapping[str, str] | None = None,
    ) -> CompletedProcessLike:
        """Run a command and return a completed-process-like object."""
        ...  # pragma: no cover


class StatusCollector(Protocol):
    def __call__(
        self,
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
        compose_file: Path | None = None,
        compose_env_file: Path | None = None,
    ) -> Awaitable[dict[str, object]]: ...  # pragma: no cover


Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


class _SubprocessRunKwargs(TypedDict):
    """Keyword arguments forwarded to the injectable subprocess runner."""

    check: bool
    capture_output: bool
    text: Literal[True]
    env: NotRequired[Mapping[str, str]]


@dataclass(frozen=True, kw_only=True)
class ServiceBootstrapOptions:
    """Operator-tunable bootstrap settings."""

    timeout_seconds: float = DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_BOOTSTRAP_POLL_INTERVAL_SECONDS
    skip_agent_runtime_build: bool = False
    force_rebuild: bool = False
    strict_providers: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, kw_only=True)
class ServiceBootstrapStageResult:
    """Recorded result for one bootstrap stage."""

    stage: str
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True, kw_only=True)
class ServiceBootstrapResult:
    """Successful bootstrap payload."""

    stages: tuple[ServiceBootstrapStageResult, ...]
    service_status: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "reason_code": "SERVICE_BOOTSTRAP_SUCCEEDED",
            "stages": [stage.to_dict() for stage in self.stages],
            "service_status": self.service_status,
        }


class ServiceBootstrapError(RuntimeError):
    """Structured bootstrap failure for clean CLI rendering."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        stage: str | None = None,
        command: Sequence[str] | None = None,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        last_status: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.stage = stage
        self.command = tuple(command or ())
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.last_status = dict(last_status) if last_status is not None else None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "failed",
            "reason_code": self.reason_code,
            "message": self.message,
        }
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.command:
            payload["command"] = list(self.command)
        if self.returncode is not None:
            payload["returncode"] = self.returncode
        if self.stdout:
            payload["stdout"] = self.stdout
        if self.stderr:
            payload["stderr"] = self.stderr
        if self.last_status is not None:
            payload["last_status"] = self.last_status
        return payload


@dataclass(frozen=True)
class _BootstrapStage:
    name: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class _BootstrapAssets:
    root: Path | None
    agent_runtime_dockerfile: Path | None
    compose_file: Path
    compose_env_file: Path | None


@dataclass(frozen=True)
class WorkDirPropagationResult:
    """Outcome of the host work-dir mount-propagation preflight.

    ``propagation`` is the bind-propagation flag to gate the worker's work-dir
    bind with (``rshared`` / ``rprivate``); ``force_copy`` requests the
    per-workspace ``~/.claude`` copy fallback when an overlay would never reach
    the agent container.
    """

    propagation: str
    force_copy: bool
    reason_code: str
    detail: str

    def to_stage_result(self) -> ServiceBootstrapStageResult:
        """Render the preflight outcome as a recorded bootstrap stage."""

        return ServiceBootstrapStageResult(
            stage="work_dir_propagation",
            command=("awf", "preflight", "work-dir-mount-propagation"),
            returncode=0,
            stdout=self.reason_code,
            stderr=self.detail,
        )


@dataclass(frozen=True)
class PersistedPropagationPosture:
    propagation: str
    force_copy: str
    timestamp: str


def _persist_work_dir_propagation_result(
    env_file: Path,
    result: WorkDirPropagationResult,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Persist the propagation posture into the compose env-file (#398).

    Writes ``AWF_WORK_DIR_BIND_PROPAGATION``, ``AWF_CLAUDE_AUTH_FORCE_COPY``,
    and ``AWF_WORK_DIR_PROPAGATION_TIMESTAMP`` into the compose env-file so a
    later non-bootstrap ``docker compose up`` recreates containers with the
    same posture the preflight decided.

    When *environ* is supplied it is the in-process environment that
    ``_apply_work_dir_propagation_env`` already folded the operator override
    into.  The effective ``AWF_CLAUDE_AUTH_FORCE_COPY`` for the env-file is
    the preflight result OR any operator request found in either the
    existing env-file **or** the in-process environment — matching the
    monotonic-raise guarantee of ``_apply_work_dir_propagation_env``.

    When the pre-existing env-file carries a co-persisted
    ``AWF_WORK_DIR_PROPAGATION_TIMESTAMP`` alongside
    ``AWF_CLAUDE_AUTH_FORCE_COPY``, the force-copy value was generated by a
    prior bootstrap run, not set by the operator.  Such stale generated
    values are ignored so a fresh preflight can correct the posture (for
    example when the work dir moves to a shared mount).  Only a
    ``AWF_CLAUDE_AUTH_FORCE_COPY`` in the env-file **without** a
    co-persisted timestamp is preserved as an operator override.

    Best-effort: ``OSError`` on write is caught and logged (redacted), never
    fatal. The env-file is written atomically via a temp file in the same
    directory and ``os.replace()``.
    """
    now_iso = datetime.now(tz=UTC).isoformat()
    effective_force_copy = result.force_copy
    try:
        pre_existing_env = compose_env_file_values(env_file) if env_file.exists() else {}
        stale_generated = AWF_WORK_DIR_PROPAGATION_TIMESTAMP_ENV in pre_existing_env
        env_file_force_copy_is_operator_override = (
            _force_copy_already_requested(pre_existing_env) and not stale_generated
        )
        effective_force_copy = (
            result.force_copy
            or env_file_force_copy_is_operator_override
            or (environ is not None and _force_copy_already_requested(environ))
        )
    except (OSError, UnicodeDecodeError):
        pre_existing_env = {}
    new_posture = PersistedPropagationPosture(
        propagation=result.propagation,
        force_copy="true" if effective_force_copy else "false",
        timestamp=now_iso,
    )
    try:
        lines: list[str] = []
        if env_file.exists():
            for raw_line in env_file.read_text(encoding="utf-8").splitlines():
                stripped = raw_line.lstrip()
                if not stripped or stripped.startswith("#"):
                    lines.append(raw_line)
                    continue
                eq_pos = stripped.find("=")
                if eq_pos < 1:
                    lines.append(raw_line)
                    continue
                key = stripped[:eq_pos].strip()
                if key.startswith("export"):
                    parts = key.split(None, 1)
                    if len(parts) == 2 and parts[0] == "export":
                        key = parts[1]
                if key in (
                    AWF_WORK_DIR_BIND_PROPAGATION_ENV,
                    AWF_CLAUDE_AUTH_FORCE_COPY_ENV,
                    AWF_WORK_DIR_PROPAGATION_TIMESTAMP_ENV,
                ):
                    continue
                lines.append(raw_line)
        for key, value in (
            (AWF_WORK_DIR_BIND_PROPAGATION_ENV, new_posture.propagation),
            (AWF_CLAUDE_AUTH_FORCE_COPY_ENV, new_posture.force_copy),
            (AWF_WORK_DIR_PROPAGATION_TIMESTAMP_ENV, new_posture.timestamp),
        ):
            lines.append(f"{key}={value}")
        env_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(env_file.parent),
            prefix=".awf-env-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
                fh.write("\n")
            Path(tmp_path).replace(env_file)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except Exception as exc:
        redacted = redact_audit_text(str(exc))
        logging.getLogger(__name__).warning(
            "best-effort persist of work-dir propagation posture failed: %s",
            redacted,
        )


@dataclass(frozen=True)
class _MountInfoEntry:
    mount_point: str
    shared: bool
    fs_type: str


def _host_is_linux() -> bool:
    """Return whether bootstrap runs on a Linux host (mount(8) propagation works)."""

    return sys.platform.startswith("linux")


def _mount_binary_available() -> bool:
    """Return whether ``mount(8)`` is on PATH for the make-rshared attempt."""

    return shutil.which("mount") is not None


def _unescape_mountinfo_field(field: str) -> str:
    """Decode the octal escapes ``/proc/self/mountinfo`` uses in path fields.

    The kernel escapes space (``\\040``), tab (``\\011``), newline (``\\012``)
    and backslash (``\\134``). A single regex pass over each ``\\NNN`` token is
    order-independent, so a literal backslash followed by octal-escape-like
    digits (encoded as e.g. ``\\134040``) decodes to ``\\040`` rather than being
    mangled into a space by sequential replacements.
    """

    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def _parse_mountinfo(text: str) -> list[_MountInfoEntry]:
    """Parse ``/proc/self/mountinfo`` into mount-point / propagation / fs-type rows.

    A mount is propagation-shared iff one of its optional fields (between field 6
    and the ``-`` separator) is a ``shared:N`` tag.
    """

    entries: list[_MountInfoEntry] = []
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) < 7:
            continue
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator + 1 >= len(fields):
            continue
        optional = fields[6:separator]
        entries.append(
            _MountInfoEntry(
                mount_point=_unescape_mountinfo_field(fields[4]),
                shared=any(option.startswith("shared:") for option in optional),
                fs_type=fields[separator + 1],
            )
        )
    return entries


def _path_within_mount(mount_point: str, path: str) -> bool:
    """Return whether ``path`` lies on the mount rooted at ``mount_point``."""

    if mount_point == "/":
        return True
    normalized = mount_point.rstrip("/")
    return path == normalized or path.startswith(normalized + "/")


def _work_dir_mount_entry(mountinfo_path: Path, target: Path) -> _MountInfoEntry | None:
    """Return the longest-prefix mount entry backing ``target``, or ``None``.

    Resolves symlinks in ``target`` (``realpath``) before prefix-matching: the
    kernel records mount points in ``/proc/self/mountinfo`` as fully
    symlink-resolved canonical paths, so a work dir reached through a symlink
    (e.g. a symlinked ``$HOME``) would otherwise fail to match and silently force
    the copy fallback even where propagation is fine. ``realpath`` does not
    require the path to exist (it resolves the symlinked prefix and keeps any
    non-existent tail) and is a no-op on plain, symlink-free paths.
    """

    try:
        text = mountinfo_path.read_text()
    except OSError:
        return None
    resolved = os.path.realpath(os.fspath(target))  # noqa: PTH100 - match kernel canonical mount paths
    best: _MountInfoEntry | None = None
    for entry in _parse_mountinfo(text):
        if not _path_within_mount(entry.mount_point, resolved):
            continue
        if best is None or len(entry.mount_point) > len(best.mount_point):
            best = entry
    return best


def _try_make_work_dir_rshared(
    target: Path,
    *,
    run_subprocess: SubprocessRun,
    environ: Mapping[str, str] | None,
) -> bool:
    """Best-effort ``mount --bind`` + ``mount --make-rshared`` on ``target``.

    Returns whether both commands succeeded (exit 0). Any non-zero exit or
    ``OSError`` (e.g. ``mount`` missing, not root) means we could not make the
    work dir shared and the caller falls back to the copy posture.

    If the ``--bind`` succeeds but ``--make-rshared`` fails (e.g. the mount
    namespace disallows propagation-mode changes), the self-referential bind is
    unwound with ``umount`` before returning ``False`` — otherwise repeated
    bootstrap runs that hit this edge would accumulate one stale entry per
    attempt in the host mount table. The unwind is itself best-effort.

    ``mount --bind`` requires the target directory to exist. On a first
    bootstrap the host work dir (``${HOME}/.awf/service`` by default) has not
    been created yet — Compose would auto-create the bind source, but this
    preflight runs before any Compose stage. The directory is therefore created
    (best effort) before the bind so the bind is not spuriously denied on a host
    where ``--make-rshared`` would otherwise succeed; a failed creation just
    leaves the bind to fail and the caller falls back to the copy posture.
    """

    env = dict(environ) if environ is not None else None

    # Best effort: if the bind source cannot be created, the bind below fails
    # and the caller falls back to the per-workspace copy posture.
    with contextlib.suppress(OSError):
        target.mkdir(parents=True, exist_ok=True)

    def _run(command: list[str]) -> bool:
        try:
            result = run_subprocess(
                command,
                **_subprocess_run_kwargs(
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                ),
            )
        except OSError:
            return False
        return result.returncode == 0

    if not _run(["mount", "--bind", str(target), str(target)]):
        return False
    if not _run(["mount", "--make-rshared", str(target)]):
        # The bind landed but the propagation-mode change did not; unwind the
        # self-referential bind so repeated bootstraps don't leak mount entries.
        _run(["umount", str(target)])
        return False
    return True


def ensure_work_dir_mount_propagation(
    host_work_dir: str,
    *,
    run_subprocess: SubprocessRun,
    environ: Mapping[str, str] | None = None,
    mountinfo_path: Path = DEFAULT_MOUNTINFO_PATH,
) -> WorkDirPropagationResult:
    """Ensure ``host_work_dir`` is an ``rshared`` host mount, or force the copy fallback.

    The worker binds the work dir ``:rshared`` so an overlay it mounts under it is
    visible to the sibling agent container. That requires the host-side work dir
    to be a shared mount. This preflight:

    - Reads ``/proc/self/mountinfo`` to find the mount backing ``host_work_dir``.
      Shared on a propagating fs → ``rshared`` (no copy fallback). A
      non-propagating fs (Docker Desktop / virtiofs / grpcfuse / Plan 9) forces
      the copy fallback even when flagged shared, since the overlay never reaches
      the sibling regardless of propagation mode.
    - Private but Linux with ``mount(8)`` available → create the work dir if it
      does not yet exist (the bind source), then attempt ``mount --bind`` +
      ``mount --make-rshared`` (idempotent), then report ``rshared`` on success.
    - Non-propagating (Docker Desktop / virtiofs / grpcfuse / Plan 9), non-Linux,
      no ``mount(8)``, an unreadable mountinfo, or a failed ``--make-rshared`` →
      ``rprivate`` + ``force_copy=True`` so the worker uses the per-workspace copy
      instead of silently provisioning an empty ``~/.claude``.

    Best effort and never fatal: the worst case is the (correct) copy fallback.
    """

    target = Path(host_work_dir).expanduser()
    entry = _work_dir_mount_entry(mountinfo_path, target)
    if entry is None:
        return WorkDirPropagationResult(
            propagation="rprivate",
            force_copy=True,
            reason_code=SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE,
            detail=(
                f"could not resolve a host mount backing {target} via {mountinfo_path}; "
                "forcing the per-workspace copy fallback"
            ),
        )
    if entry.fs_type in _NON_PROPAGATING_FS_TYPES:
        # Checked before ``entry.shared``: on these filesystems an overlay never
        # reaches the sibling agent even when the mount is flagged shared/rshared
        # (Docker Desktop marks its virtiofs/grpcfuse mounts ``shared:N``), so a
        # shared flag here is a false reassurance. Force the copy fallback rather
        # than provisioning an empty overlay.
        return WorkDirPropagationResult(
            propagation="rprivate",
            force_copy=True,
            reason_code=SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE,
            detail=(
                f"{target} is on a non-propagating mount "
                f"(fs={entry.fs_type}, mount={entry.mount_point}); forcing the "
                "per-workspace copy fallback"
            ),
        )
    if entry.shared:
        return WorkDirPropagationResult(
            propagation="rshared",
            force_copy=False,
            reason_code=SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED,
            detail=f"{target} is backed by a shared mount at {entry.mount_point}",
        )
    if not _host_is_linux() or not _mount_binary_available():
        return WorkDirPropagationResult(
            propagation="rprivate",
            force_copy=True,
            reason_code=SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE,
            detail=(
                f"{target} is on a non-propagating mount "
                f"(fs={entry.fs_type}, mount={entry.mount_point}); forcing the "
                "per-workspace copy fallback"
            ),
        )
    if _try_make_work_dir_rshared(target, run_subprocess=run_subprocess, environ=environ):
        return WorkDirPropagationResult(
            propagation="rshared",
            force_copy=False,
            reason_code=SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED,
            detail=f"made {target} an rshared mount via mount --make-rshared",
        )
    return WorkDirPropagationResult(
        propagation="rprivate",
        force_copy=True,
        reason_code=SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE,
        detail=(
            f"could not make {target} an rshared mount (mount --make-rshared failed); "
            "forcing the per-workspace copy fallback"
        ),
    )


def _resolve_bootstrap_host_work_dir(environ: Mapping[str, str]) -> str | None:
    """Return the host work dir to preflight, or ``None`` when unknowable.

    Mirrors exactly what compose binds for the worker:
    ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` (see
    docker/compose/local-service.yml). Only ``AWF_HOST_WORK_DIR`` pins the host
    bind; when it is unset, falls back to compose's deterministic default
    ``${HOME}/.awf/service`` so the propagation preflight still runs on the common
    bootstrap path — exactly the Docker Desktop/virtiofs case this change must
    detect, where the compose ``:rshared`` default would otherwise stand and leave
    the worker provisioning an empty overlay. ``AWF_WORK_DIR`` is deliberately not
    consulted: it is the (often relative, default ``.awf``) in-container CLI/API
    state root, which compose sets *from* the host bind path rather than reads, so
    preflighting it would inspect the wrong path and leave the actual
    ``${HOME}/.awf/service`` bind on its default posture. Returns ``None`` only
    when ``HOME`` is also absent, leaving today's compose defaults untouched.
    """

    value = environ.get("AWF_HOST_WORK_DIR")
    if value and value.strip():
        return value.strip()
    home = environ.get("HOME")
    if home and home.strip():
        return str(Path(home.strip()) / DEFAULT_HOST_WORK_DIR_SUBPATH)
    return None


def _force_copy_already_requested(environ: Mapping[str, str]) -> bool:
    """Return whether ``environ`` already carries an operator force-copy request.

    Delegates to ``auth_mounts.force_copy_isolation_requested`` so the preflight
    and the per-workspace overlay gate share a single source of truth for what
    counts as an operator override — rather than re-encoding the truthiness set
    here, where the two could silently drift.
    """

    return force_copy_isolation_requested(environ)


def _apply_work_dir_propagation_env(
    environ: Mapping[str, str],
    result: WorkDirPropagationResult,
) -> dict[str, str]:
    """Return ``environ`` with the propagation + force-copy vars folded in.

    The preflight can only *raise* the force-copy posture: when it concludes copy
    is unnecessary it must not overwrite an operator's explicit
    ``AWF_CLAUDE_AUTH_FORCE_COPY`` override (set in the environment or the compose
    env file), since ``auth_mounts`` treats that variable as an operator
    force-copy request that wins over overlay capability. So the effective value
    is the preflight's result OR any pre-existing operator request.
    """

    updated = dict(environ)
    updated[AWF_WORK_DIR_BIND_PROPAGATION_ENV] = result.propagation
    force_copy = result.force_copy or _force_copy_already_requested(environ)
    updated[AWF_CLAUDE_AUTH_FORCE_COPY_ENV] = "true" if force_copy else "false"
    return updated


async def run_service_bootstrap(
    settings: ServiceSettings,
    *,
    options: ServiceBootstrapOptions | None = None,
    compose_file: Path = LOCAL_SERVICE_COMPOSE_FILE,
    env_file: Path | None = None,
    asset_root: Path | None = None,
    run_subprocess: SubprocessRun | None = None,
    status_collector: StatusCollector | None = None,
    sleep: Sleep = asyncio.sleep,
    monotonic: Monotonic = time.monotonic,
    service_environ: Mapping[str, str] | None = None,
    provider_environ: Mapping[str, str] | None = None,
) -> ServiceBootstrapResult:
    """Start local service dependencies and wait for healthy status.

    ``asset_root`` pins compose, agent-runtime, and env-file resolution to an
    explicitly selected bootstrap asset root (e.g. a verified source checkout)
    instead of running discovery. ``None`` preserves discovery behavior.
    """

    resolved_options = options or ServiceBootstrapOptions()
    runner = run_subprocess or _run_subprocess
    collector = status_collector or collect_service_status
    completed: list[ServiceBootstrapStageResult] = []
    assets = _resolve_bootstrap_assets(
        compose_file,
        require_agent_runtime=not resolved_options.skip_agent_runtime_build,
        asset_root=asset_root,
    )
    if env_file is not None:
        assets = replace(assets, compose_env_file=env_file if env_file.exists() else None)
    resolved_env_file = (
        env_file
        if env_file is not None and env_file.exists()
        else _bootstrap_environment_file(assets)
    )
    raw_service_env = (
        dict(service_environ)
        if service_environ is not None
        else local_service_environ(env_file=resolved_env_file)
    )
    if provider_environ is not None:
        raw_service_env.update(provider_environ)
    service_env = _docker_cli_environ(raw_service_env)

    subprocess_env = _docker_cli_environ({**os.environ, **raw_service_env})

    # Work-dir mount-propagation preflight (#376/#388). Runs for the explicitly
    # configured host work dir, and otherwise for compose's deterministic default
    # ``${HOME}/.awf/service``: detect whether it propagates so the worker's
    # ``:rshared`` bind is gated and the overlay/copy posture is chosen before the
    # api/worker containers start. Skipped only when even ``HOME`` is unknowable,
    # leaving today's compose defaults (``:rshared`` / overlay) untouched. Best
    # effort: never fatal.
    host_work_dir = _resolve_bootstrap_host_work_dir(subprocess_env)
    if host_work_dir is not None:
        propagation = await asyncio.to_thread(
            ensure_work_dir_mount_propagation,
            host_work_dir,
            run_subprocess=runner,
            environ=subprocess_env,
        )
        completed.append(propagation.to_stage_result())
        subprocess_env = _apply_work_dir_propagation_env(subprocess_env, propagation)
        service_env = _apply_work_dir_propagation_env(service_env, propagation)
        if resolved_env_file is not None:
            await asyncio.to_thread(
                _persist_work_dir_propagation_result, resolved_env_file, propagation, subprocess_env
            )

    for stage in _bootstrap_stages(
        settings,
        options=resolved_options,
        compose_file=compose_file,
        assets=assets,
        environ=subprocess_env,
    ):
        completed.append(
            await asyncio.to_thread(
                _run_stage,
                stage,
                run_subprocess=runner,
                environ=subprocess_env,
            )
        )

    service_status = await _poll_status(
        settings,
        options=resolved_options,
        status_collector=collector,
        sleep=sleep,
        monotonic=monotonic,
        provider_environ=service_env,
        environ=service_env,
        compose_file=assets.compose_file,
        compose_env_file=assets.compose_env_file,
    )
    return ServiceBootstrapResult(
        stages=tuple(completed),
        service_status=service_status,
    )


def _bootstrap_stages(
    settings: ServiceSettings,
    *,
    options: ServiceBootstrapOptions,
    compose_file: Path,
    assets: _BootstrapAssets | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[_BootstrapStage, ...]:
    """Return the ordered Docker stages required for service bootstrap."""

    resolved_assets = assets or _resolve_bootstrap_assets(
        compose_file,
        require_agent_runtime=not options.skip_agent_runtime_build,
    )
    stages: list[_BootstrapStage] = []
    if not options.skip_agent_runtime_build:
        if (
            resolved_assets.root is None or resolved_assets.agent_runtime_dockerfile is None
        ):  # pragma: no cover
            raise _bootstrap_assets_not_found_error(compose_file)
        stages.append(
            _BootstrapStage(
                "agent_runtime_build",
                (
                    "docker",
                    "build",
                    *(("--no-cache",) if options.force_rebuild else ()),
                    "-t",
                    settings.agent_runtime_image,
                    "-f",
                    str(resolved_assets.agent_runtime_dockerfile),
                    str(resolved_assets.root),
                ),
            )
        )

    compose = _compose_command(
        resolved_assets.compose_file,
        compose_env_file=resolved_assets.compose_env_file,
    )
    stages.extend(
        [
            _BootstrapStage(
                "postgres",
                (*compose, "up", "-d", "--build", "postgres"),
            ),
            *(
                [
                    _BootstrapStage(
                        "ollama_bridge",
                        (*compose, "up", "-d", "--build", "ollama-bridge"),
                    )
                ]
                if _compose_profile_enabled(environ or {}, "ollama-bridge")
                else []
            ),
            _BootstrapStage(
                "migrate",
                (*compose, "up", "--build", "--force-recreate", "migrate"),
            ),
            _BootstrapStage(
                "api_worker",
                (*compose, "up", "-d", "--build", "--no-deps", "api", "worker"),
            ),
        ]
    )
    return tuple(stages)


def _compose_profile_enabled(environ: Mapping[str, str], profile: str) -> bool:
    """Return whether a Compose profile is enabled in the service env."""

    _, raw = env_lookup(environ, "COMPOSE_PROFILES")
    return profile in {
        item.strip() for chunk in raw.split(",") for item in chunk.split() if item.strip()
    }


def _docker_cli_environ(
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Return subprocess env with Docker host selection from service settings."""

    resolved = dict(environ)
    # Keep Docker CLI host selection scoped to the resolved service environment;
    # falling back to ServiceSettings would reintroduce process-environment drift.
    docker_host = non_empty_env_value(resolved, "AWF_DOCKER_HOST") or non_empty_env_value(
        resolved, "DOCKER_HOST"
    )
    scrubbed_keys = {"AWF_DOCKER_HOST", *cleared_docker_cli_client_keys(environ)}
    caller_docker_host_found, caller_docker_host_value = env_lookup(os.environ, "DOCKER_HOST")
    docker_host_found, docker_host_value = env_lookup(resolved, "DOCKER_HOST")
    clears_docker_host = (
        docker_host_found
        and not docker_host_value
        and caller_docker_host_found
        and bool(caller_docker_host_value)
    )
    if docker_host or clears_docker_host:
        scrubbed_keys.update({"DOCKER_CONTEXT", "DOCKER_HOST"})
    for key in list(resolved):
        if key.upper() in scrubbed_keys:
            del resolved[key]
    if docker_host:
        resolved["DOCKER_HOST"] = docker_host
    return resolved


def _resolve_bootstrap_assets(
    compose_file: Path,
    *,
    require_agent_runtime: bool,
    asset_root: Path | None = None,
) -> _BootstrapAssets:
    """Resolve compose, runtime Dockerfile, and env-file assets for bootstrap.

    When ``asset_root`` is provided, resolution is pinned to that root instead of
    running discovery, so explicitly selected source-checkout assets are used.
    """

    if asset_root is not None:
        return _resolve_pinned_bootstrap_assets(
            compose_file,
            require_agent_runtime=require_agent_runtime,
            asset_root=asset_root,
        )

    asset_root = _resolve_bootstrap_asset_root()
    default_compose = compose_file == LOCAL_SERVICE_COMPOSE_FILE or (
        compose_file.is_absolute()
        and asset_root is not None
        and compose_file.resolve() == (asset_root / LOCAL_SERVICE_COMPOSE_FILE).resolve()
    )

    if default_compose:
        if asset_root is None:
            raise _bootstrap_assets_not_found_error(compose_file)
        resolved_compose_file = asset_root / LOCAL_SERVICE_COMPOSE_FILE
    else:
        resolved_compose_file = _resolve_user_path(compose_file)

    agent_runtime_dockerfile: Path | None = None
    if require_agent_runtime:
        if asset_root is None:
            raise _bootstrap_assets_not_found_error(compose_file)
        agent_runtime_dockerfile = asset_root / AGENT_RUNTIME_DOCKERFILE

    return _BootstrapAssets(
        root=asset_root,
        agent_runtime_dockerfile=agent_runtime_dockerfile,
        compose_file=resolved_compose_file,
        compose_env_file=_resolve_compose_env_file(asset_root),
    )


def _resolve_pinned_bootstrap_assets(
    compose_file: Path,
    *,
    require_agent_runtime: bool,
    asset_root: Path,
) -> _BootstrapAssets:
    """Resolve bootstrap assets pinned to an explicitly selected asset root.

    Used when start selects verified source-checkout assets. The root must be a
    valid bootstrap asset root, otherwise the existing not-found error is raised
    (start maps it to ``START_COMPOSE_ASSETS_MISSING``).
    """

    if not _is_bootstrap_asset_root(asset_root):
        raise _bootstrap_assets_not_found_error(compose_file)

    default_compose = compose_file == LOCAL_SERVICE_COMPOSE_FILE or (
        compose_file.is_absolute()
        and compose_file.resolve() == (asset_root / LOCAL_SERVICE_COMPOSE_FILE).resolve()
    )
    resolved_compose_file = (
        asset_root / LOCAL_SERVICE_COMPOSE_FILE
        if default_compose
        else _resolve_user_path(compose_file)
    )
    agent_runtime_dockerfile = (
        asset_root / AGENT_RUNTIME_DOCKERFILE if require_agent_runtime else None
    )
    return _BootstrapAssets(
        root=asset_root,
        agent_runtime_dockerfile=agent_runtime_dockerfile,
        compose_file=resolved_compose_file,
        compose_env_file=_resolve_compose_env_file(asset_root),
    )


def _bootstrap_environment_file(assets: _BootstrapAssets) -> Path:
    """Return the env file path bootstrap should use as its base environment."""

    if assets.compose_env_file is not None:
        return assets.compose_env_file
    if assets.root is not None and is_packaged_bootstrap_asset_root(assets.root):
        return LOCAL_SERVICE_COMPOSE_ENV_FILE
    if assets.root is not None:
        return assets.root / LOCAL_SERVICE_COMPOSE_ENV_FILE
    return LOCAL_SERVICE_COMPOSE_ENV_FILE


def get_bootstrap_asset_root() -> Path | None:
    """Return the verified source root that contains local bootstrap assets."""

    return _resolve_bootstrap_asset_root()


def is_packaged_bootstrap_asset_root(path: Path) -> bool:
    """Return true when ``path`` is AWF's bundled wheel bootstrap asset root."""

    packaged_root = _packaged_bootstrap_asset_root()
    return packaged_root is not None and path.resolve() == packaged_root.resolve()


def _resolve_bootstrap_asset_root() -> Path | None:
    for candidate in _bootstrap_asset_root_candidates():
        if _is_bootstrap_asset_root(candidate):
            return candidate
    packaged_root = _packaged_bootstrap_asset_root()
    if packaged_root is not None:
        return packaged_root
    return None


def _packaged_bootstrap_asset_root() -> Path | None:
    """Return the bundled bootstrap asset root from an installed package."""

    try:
        candidate = files("awf").joinpath(PACKAGED_BOOTSTRAP_ASSET_ROOT.as_posix())
    except (ModuleNotFoundError, TypeError):
        return None
    # files("awf") yields a Traversable that is only a real Path for
    # filesystem installs (the wheel/editable case). Zip-imported packages
    # return a non-Path Traversable lacking Path APIs such as .resolve() that
    # callers (e.g. is_packaged_bootstrap_asset_root) rely on, so the guard
    # must stay this strict — do not broaden it to accept any Traversable.
    if not isinstance(candidate, Path):
        return None
    return candidate if _is_bootstrap_asset_root(candidate) else None


def _bootstrap_asset_root_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    cwd = Path.cwd().resolve()
    candidates.extend((cwd, *cwd.parents))
    module_file = Path(__file__).resolve()
    candidates.extend(module_file.parents)

    deduplicated: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduplicated.append(candidate)
    return tuple(deduplicated)


def _is_bootstrap_asset_root(candidate: Path) -> bool:
    return (
        candidate.is_dir()
        and (candidate / AGENT_RUNTIME_DOCKERFILE).is_file()
        and (candidate / LOCAL_SERVICE_COMPOSE_FILE).is_file()
        and (candidate / LOCAL_SERVICE_INCLUDED_COMPOSE_FILE).is_file()
        and (candidate / "docker/control-plane.Dockerfile").is_file()
        and (candidate / "pyproject.toml").is_file()
        and (candidate / "src/awf/__init__.py").is_file()
    )


def _resolve_user_path(path: Path) -> Path:
    """Resolve a user-provided path after expanding the home directory."""

    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return expanded.resolve()


def _resolve_compose_env_file(asset_root: Path | None) -> Path | None:
    """Return the canonical local service env file when it exists."""

    if asset_root is not None:
        candidate = asset_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
        return candidate if candidate.exists() else None

    return resolve_local_service_compose_env_file(
        LOCAL_SERVICE_COMPOSE_ENV_FILE,
    )


def _bootstrap_assets_not_found_error(compose_file: Path) -> ServiceBootstrapError:
    return ServiceBootstrapError(
        reason_code=SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND,
        message=(
            "Cannot resolve AWF bootstrap assets for local service startup. "
            "Run awf service bootstrap from an AWF source checkout that contains "
            f"{AGENT_RUNTIME_DOCKERFILE} and {LOCAL_SERVICE_COMPOSE_FILE}, or install "
            "an AWF package that explicitly supports bundled bootstrap assets. "
            f"Required default compose file: {compose_file}."
        ),
    )


def _compose_command(
    compose_file: Path,
    *,
    compose_env_file: Path | None = None,
) -> tuple[str, ...]:
    """Build the Docker Compose command prefix for a compose file."""

    args = ["docker", "compose"]
    if compose_env_file is not None:
        args.extend(["--env-file", str(compose_env_file)])
    args.extend(["-f", str(compose_file)])
    return tuple(args)


def _bootstrap_subprocess_env(environ: Mapping[str, str]) -> dict[str, str] | None:
    """Return ``environ`` as a dict, or ``None`` when it adds nothing beyond current env."""
    env_dict = dict(environ)
    if env_dict == dict(os.environ):
        return None
    return env_dict


def _run_stage(
    stage: _BootstrapStage,
    *,
    run_subprocess: SubprocessRun,
    environ: Mapping[str, str],
) -> ServiceBootstrapStageResult:
    """Run one bootstrap stage and normalize subprocess failures."""

    try:
        result = run_subprocess(
            list(stage.command),
            **_subprocess_run_kwargs(
                check=False,
                capture_output=True,
                text=True,
                env=_bootstrap_subprocess_env(environ),
            ),
        )
    except FileNotFoundError as exc:
        raise ServiceBootstrapError(
            reason_code=SERVICE_BOOTSTRAP_STAGE_FAILED,
            message=f"{stage.name} failed: docker binary not found on PATH",
            stage=stage.name,
            command=stage.command,
            returncode=127,
            stderr="docker binary not found on PATH",
        ) from exc
    except OSError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        raise ServiceBootstrapError(
            reason_code=SERVICE_BOOTSTRAP_STAGE_FAILED,
            message=f"{stage.name} failed: {detail}",
            stage=stage.name,
            command=stage.command,
            returncode=1,
            stderr=detail,
        ) from exc

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0:
        raise ServiceBootstrapError(
            reason_code=SERVICE_BOOTSTRAP_STAGE_FAILED,
            message=f"{stage.name} failed with exit code {result.returncode}",
            stage=stage.name,
            command=stage.command,
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return ServiceBootstrapStageResult(
        stage=stage.name,
        command=stage.command,
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
    )


async def _poll_status(
    settings: ServiceSettings,
    *,
    options: ServiceBootstrapOptions,
    status_collector: StatusCollector,
    sleep: Sleep,
    monotonic: Monotonic,
    provider_environ: Mapping[str, str],
    environ: Mapping[str, str],
    compose_file: Path,
    compose_env_file: Path | None,
) -> dict[str, object]:
    timeout_seconds = max(0.0, options.timeout_seconds)
    poll_interval_seconds = max(0.01, options.poll_interval_seconds)
    deadline = monotonic() + timeout_seconds
    last_status: dict[str, object] | None = None
    last_error: Exception | None = None

    while True:
        try:
            last_status = await status_collector(
                settings,
                strict_providers=options.strict_providers,
                provider_environ=provider_environ,
                environ=environ,
                compose_file=compose_file,
                compose_env_file=compose_env_file,
            )
            last_error = None
        except Exception as exc:
            last_error = exc
            last_status = _status_collection_failed_status(settings, exc)

        if last_status.get("status") == "ok":
            return last_status

        remaining = deadline - monotonic()
        if remaining <= 0:
            error = ServiceBootstrapError(
                reason_code=SERVICE_BOOTSTRAP_TIMEOUT,
                message="timed out waiting for local service readiness",
                last_status=last_status,
            )
            if last_error is not None:
                raise error from last_error
            raise error
        await sleep(min(poll_interval_seconds, remaining))


def _status_collection_failed_status(
    settings: ServiceSettings,
    exc: Exception,
) -> dict[str, object]:
    """Return a failed status payload for readiness collection errors."""

    return {
        "service": settings.service_name,
        "status": "fail",
        "checks": {
            "status_collector": {
                "ok": False,
                "status": "fail",
                "reason": "STATUS_COLLECTION_FAILED",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        },
    }


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    env: Mapping[str, str] | None = None,
) -> CompletedProcessLike:
    """Run a subprocess using the same keyword filtering as test doubles."""

    return subprocess.run(
        args,
        **_subprocess_run_kwargs(
            check=check,
            capture_output=capture_output,
            text=text,
            env=env,
        ),
    )


def _subprocess_run_kwargs(
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    env: Mapping[str, str] | None,
) -> _SubprocessRunKwargs:
    """Build subprocess runner kwargs while omitting absent env overrides."""

    kwargs: _SubprocessRunKwargs = {
        "check": check,
        "capture_output": capture_output,
        "text": text,
    }
    if env is not None:
        kwargs["env"] = dict(env)
    return kwargs
