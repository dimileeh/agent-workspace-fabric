"""Local service status checks for the CLI."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import httpx
from sqlalchemy import select, text

from awf.common.audit import redact_audit_text
from awf.db.models import Workspace
from awf.db.repositories import EgressAuditRepository
from awf.db.resilience import db_connection_failure_reason
from awf.db.session import make_engine, make_session_factory
from awf.service.config import (
    COMPOSE_ENV_FILE_OMITTED,
    ComposeEnvFileInput,
    ServiceSettings,
    resolve_local_service_provider_environ,
)
from awf.service.disk import DiskCheck, DiskUsage, check_disk_space
from awf.service.gc import plan_terminal_workspace_gc
from awf.service.orphan_resources import (
    ORPHAN_REAPING_ACTION,
)
from awf.service.orphan_resources import (
    scan_docker_resources as scan_runtime_docker_resources,
)
from awf.service.orphans import (
    ACTIVE_WORKSPACE_STATUSES,
    KNOWN_WORKSPACE_STATUSES,
    TERMINAL_WORKSPACE_STATUSES,
    WorkspaceIdView,
    WorkspaceLifecycleSnapshot,
    detect_orphan_resources,
    workspace_id_from_project,
)
from awf.service.profile_metadata import (
    NetworkPosture,
    egress_allowlist_templates_from_profile_snapshot,
    network_posture_from_profile_snapshot,
)
from awf.service.provider_readiness import HttpGet as ProviderHttpGet
from awf.service.provider_readiness import collect_agent_readiness
from awf.service.workspace_runtime_health import (
    RuntimeWorkspace,
    summarize_runtime_health,
)

_CHECK_TIMEOUT_SECONDS = 5.0

CheckPayload = dict[str, object]
DbProbe = Callable[[str], Awaitable[CheckPayload]]
SocketExists = Callable[[Path], bool]


WorkspaceIdLookup = Callable[[str], Awaitable[WorkspaceIdView]]
EgressAuditSummaryLookup = Callable[[str], Awaitable[Mapping[str, int]]]


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...  # pragma: no cover - Protocol method declaration only.

    def raise_for_status(self) -> Any: ...  # pragma: no cover - Protocol method declaration only.


class HttpGet(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        url: str,
        *,
        timeout: float,
    ) -> Awaitable[HttpResponse]: ...


class SubprocessRun(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcessLike: ...


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


async def collect_service_status(
    settings: ServiceSettings,
    *,
    api_get: HttpGet | None = None,
    db_probe: DbProbe | None = None,
    run_subprocess: SubprocessRun | None = None,
    socket_exists: SocketExists | None = None,
    disk_usage: DiskUsage | None = None,
    workspace_id_lookup: WorkspaceIdLookup | None = None,
    strict_providers: Iterable[str] | None = None,
    provider_environ: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    compose_file: Path | None = None,
    compose_env_file: ComposeEnvFileInput = COMPOSE_ENV_FILE_OMITTED,
    provider_http_get: ProviderHttpGet | None = None,
    egress_audit_summary_lookup: EgressAuditSummaryLookup | None = None,
) -> dict[str, object]:
    """Collect service dependency status without requiring Docker in tests."""

    resolved_api_get = api_get or _http_get
    resolved_db_probe = db_probe or check_database
    resolved_run = run_subprocess or _run_subprocess
    resolved_socket_exists = socket_exists or Path.exists
    provider_environ = resolve_local_service_provider_environ(
        provider_environ=provider_environ,
        environ=os.environ if environ is None else environ,
        compose_file=compose_file,
        compose_env_file=compose_env_file,
    )
    resolved_workspace_lookup: WorkspaceIdLookup
    if workspace_id_lookup is None:

        async def _settings_workspace_id_lookup(database_url: str) -> WorkspaceIdView:
            return await _default_workspace_id_lookup(
                database_url,
                legacy_open_default_cutoff=settings.network_posture_open_legacy_cutoff,
            )

        resolved_workspace_lookup = _settings_workspace_id_lookup
    else:
        resolved_workspace_lookup = workspace_id_lookup

    async def _await_workspace_view() -> WorkspaceIdView:
        return await resolved_workspace_lookup(settings.database_url)

    workspace_lookup_task: asyncio.Task[WorkspaceIdView] = asyncio.create_task(
        _await_workspace_view()
    )
    provider_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(
        asyncio.to_thread(
            collect_agent_readiness,
            settings,
            environ=provider_environ,
            strict_providers=strict_providers,
            run_subprocess=resolved_run,
            http_get=provider_http_get,
        )
    )
    try:
        (
            api_check,
            db_check,
            docker_check,
            image_check,
            disk_check,
            agent_readiness,
        ) = await asyncio.gather(
            _check_api(settings, resolved_api_get),
            resolved_db_probe(settings.database_url),
            asyncio.to_thread(_check_docker, settings, resolved_run, resolved_socket_exists),
            asyncio.to_thread(_check_agent_runtime_image, settings, resolved_run),
            asyncio.to_thread(
                check_disk_space,
                settings.work_dir,
                min_free_bytes=settings.min_free_disk_bytes,
                disk_usage=disk_usage,
            ),
            provider_task,
        )
        workspace_view = await workspace_lookup_task
        workspace_cleanup_check = await collect_workspace_cleanup_status(settings)
        egress_audit_check = await collect_egress_audit_status(
            settings.database_url,
            summary_lookup=egress_audit_summary_lookup,
        )
    finally:
        for pending in (workspace_lookup_task, provider_task):
            if not pending.done():
                pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
    api_check = cast(CheckPayload, api_check)
    db_check = cast(CheckPayload, db_check)
    docker_check = cast(CheckPayload, docker_check)
    image_check = cast(CheckPayload, image_check)
    disk_check = cast(DiskCheck, disk_check)
    agent_readiness = cast(dict[str, Any], agent_readiness)
    orphan_summary = await asyncio.to_thread(
        detect_orphan_resources,
        work_dir=settings.work_dir,
        docker_host=settings.docker_host,
        workspace_view=workspace_view,
        run_subprocess=resolved_run,
    )
    runtime_docker_scan = await asyncio.to_thread(
        scan_runtime_docker_resources,
        docker_host=settings.docker_host,
        run_subprocess=resolved_run,
    )
    orphan_workspaces_check = orphan_summary.to_check_payload()
    orphan_resources_check = _orphan_resources_check_payload(
        orphan_workspaces_check,
        auto_cleanup_orphans=settings.auto_cleanup_orphans,
    )
    if settings.auto_cleanup_orphans and orphan_workspaces_check.get("orphan_count"):
        # The legacy orphan_workspaces payload otherwise keeps its manual-cleanup
        # action, which contradicts the reaping-aware orphan_resources action on the
        # same response. Align it so a single status never tells operators both
        # "the worker will reap this automatically" and "run manual cleanup".
        orphan_workspaces_check["action"] = ORPHAN_REAPING_ACTION
    stranded_workspaces_check = _stranded_workspaces_check_payload(
        workspace_view,
        runtime_docker_scan,
    )
    network_posture_check = _network_posture_check_payload(workspace_view)
    checks = {
        "api": api_check,
        "db": db_check,
        "docker": docker_check,
        "agent_runtime_image": image_check,
        "disk": disk_check.to_dict(),
        "network_posture": network_posture_check,
        "egress_audit": egress_audit_check,
        "stranded_workspaces": stranded_workspaces_check,
        "orphan_resources": orphan_resources_check,
        "orphan_workspaces": orphan_workspaces_check,
        "workspace_cleanup": workspace_cleanup_check,
    }
    overall_ok = (
        all(bool(check["ok"]) for check in checks.values()) and agent_readiness["status"] == "ok"
    )
    return {
        "service": settings.service_name,
        "status": "ok" if overall_ok else "fail",
        "checks": checks,
        "agent_readiness": agent_readiness,
    }


async def collect_egress_audit_status(
    database_url: str,
    *,
    summary_lookup: EgressAuditSummaryLookup | None = None,
) -> CheckPayload:
    lookup = summary_lookup or _default_egress_audit_summary_counts
    try:
        counts = await asyncio.wait_for(lookup(database_url), timeout=_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        return {
            "ok": True,
            "status": "unknown",
            "reason": "EGRESS_AUDIT_TIMEOUT",
            "detail": f"Egress audit summary_counts exceeded {_CHECK_TIMEOUT_SECONDS}s",
            "resource_count": 0,
            "egress_posture_counts": {},
        }
    except Exception as exc:
        return {
            "ok": True,
            "status": "unavailable",
            "reason": "EGRESS_AUDIT_UNAVAILABLE",
            "detail": _redacted_truncated_exception_detail(exc),
            "resource_count": 0,
            "egress_posture_counts": {},
        }

    posture_counts = {str(posture): int(count) for posture, count in counts.items()}
    return {
        "ok": True,
        "status": "ok",
        "reason": "EGRESS_AUDIT_AVAILABLE",
        "resource_count": sum(posture_counts.values()),
        "egress_posture_counts": posture_counts,
    }


async def _default_egress_audit_summary_counts(database_url: str) -> dict[str, int]:
    engine = None
    try:
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        async with factory() as session:
            return await EgressAuditRepository(session).summary_counts_by_posture()
    finally:
        if engine is not None:
            await engine.dispose()


async def collect_workspace_cleanup_status(settings: ServiceSettings) -> CheckPayload:
    """Report retention-cleanup readiness without making candidates unhealthy."""

    if not settings.workspace_cleanup_enabled:
        return {
            "ok": True,
            "status": "disabled",
            "reason": "WORKSPACE_CLEANUP_DISABLED",
            "retention_hours": settings.completed_workspace_retention_hours,
            "candidate_count": 0,
            "preserved_count": 0,
            "examples": [],
        }

    engine = None
    try:
        engine = make_engine(settings.database_url)
        plan = await plan_terminal_workspace_gc(
            make_session_factory(engine),
            work_dir=Path(settings.work_dir).expanduser(),
            min_age_hours=settings.completed_workspace_retention_hours,
            limit=settings.workspace_cleanup_batch_limit,
        )
    except Exception as exc:
        return {
            "ok": True,
            "status": "unavailable",
            "reason": "CLEANUP_PLAN_UNAVAILABLE",
            "detail": _truncate(f"{type(exc).__name__}: {exc}"),
            "retention_hours": settings.completed_workspace_retention_hours,
            "candidate_count": 0,
            "preserved_count": 0,
            "examples": [],
        }
    finally:
        if engine is not None:
            await engine.dispose()

    candidate_examples = [
        {
            "workspace_id": candidate.workspace_id,
            "status": candidate.status,
            "reason_code": candidate.reason_code,
            "age_hours": candidate.age_hours,
            "estimated_bytes": candidate.total_estimated_bytes,
        }
        for candidate in plan.candidates[:5]
    ]
    preserved_examples = [
        {
            "workspace_id": preserved.workspace_id,
            "status": preserved.status,
            "reason_code": preserved.reason_code,
            "age_hours": preserved.age_hours,
        }
        for preserved in plan.preserved[:5]
    ]
    candidate_count = len(plan.candidates)
    return {
        "ok": True,
        "status": "ready" if candidate_count else "ok",
        "reason": "CLEANUP_CANDIDATES_READY" if candidate_count else "NO_CLEANUP_CANDIDATES",
        "retention_hours": settings.completed_workspace_retention_hours,
        "candidate_count": candidate_count,
        "preserved_count": plan.preserved_count,
        "total_estimated_bytes": plan.total_estimated_bytes,
        "examples": [*candidate_examples, *preserved_examples][:10],
    }


async def check_database(database_url: str) -> CheckPayload:
    engine = None
    try:
        engine = make_engine(database_url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return _fail(
            db_connection_failure_reason(exc),
            _truncate(f"{type(exc).__name__}: {exc}"),
        )
    finally:
        if engine is not None:
            await engine.dispose()
    return _ok()


async def _check_api(settings: ServiceSettings, api_get: HttpGet) -> CheckPayload:
    url = f"{settings.api_base_url.rstrip('/')}/healthz"
    try:
        response = await api_get(url, timeout=_CHECK_TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception as exc:
        return _fail("API_UNREACHABLE", _truncate(f"{type(exc).__name__}: {exc}"))

    version: object | None = None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        version = body.get("version")
    return _ok(version=version if isinstance(version, str) else None)


def _check_docker(
    settings: ServiceSettings,
    run_subprocess: SubprocessRun,
    socket_exists: SocketExists,
) -> CheckPayload:
    socket_path = _docker_socket_path(settings.docker_host)
    if socket_path is not None and not socket_exists(socket_path):
        return _fail(
            "DOCKER_SOCKET_UNREACHABLE",
            f"Docker socket is not reachable at {socket_path}",
        )
    result = _run_docker_command(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        settings=settings,
        run_subprocess=run_subprocess,
    )
    return _docker_result_to_check(result, fail_reason="DOCKER_DAEMON_UNREACHABLE")


def _check_agent_runtime_image(
    settings: ServiceSettings,
    run_subprocess: SubprocessRun,
) -> CheckPayload:
    result = _run_docker_command(
        [
            "docker",
            "image",
            "inspect",
            settings.agent_runtime_image,
            "--format",
            "{{.Id}}",
        ],
        settings=settings,
        run_subprocess=run_subprocess,
    )
    return _docker_result_to_check(
        result,
        fail_reason="AGENT_RUNTIME_IMAGE_MISSING",
        detail_prefix=f"{settings.agent_runtime_image}: ",
    )


def _orphan_resources_check_payload(
    orphan_workspaces_check: Mapping[str, object],
    *,
    auto_cleanup_orphans: bool = False,
) -> CheckPayload:
    payload: CheckPayload = dict(orphan_workspaces_check)
    payload["reason"] = _orphan_resources_reason(payload.get("reason"))
    if "resource_counts" in payload:
        payload.setdefault("counts_by_kind", payload["resource_counts"])
    cleanup_readiness = _orphan_resources_cleanup_readiness(
        payload,
        auto_cleanup_orphans=auto_cleanup_orphans,
    )
    payload["cleanup_readiness"] = cleanup_readiness
    if auto_cleanup_orphans and payload.get("orphan_count"):
        # Keep the top-level action consistent with the reaping-aware readiness
        # action so the two never disagree on the same check payload.
        payload["action"] = cleanup_readiness["action"]
    return payload


def _stranded_workspaces_check_payload(
    workspace_view: WorkspaceIdView,
    docker_scan: Any,
) -> CheckPayload:
    if not workspace_view.available:
        return {
            "ok": True,
            "status": "unknown",
            "reason": "DB_UNAVAILABLE",
            "stranded_count": 0,
            "fail_candidate_count": 0,
            "recoverable_count": 0,
            "reason_counts": {},
            "examples": [],
        }

    summary = summarize_runtime_health(
        workspaces=_runtime_workspaces_from_view(workspace_view),
        resources=docker_scan.resources,
        scanner_available=bool(docker_scan.ok),
        scanner_reason=str(getattr(docker_scan, "reason", "RUNTIME_INSPECTION_UNAVAILABLE")),
        scanner_detail=getattr(docker_scan, "detail", None),
    )
    return summary.to_check_payload()


def _network_posture_check_payload(workspace_view: WorkspaceIdView) -> CheckPayload:
    if not workspace_view.available:
        return {
            "ok": True,
            "status": "unknown",
            "reason": "NETWORK_POSTURE_UNAVAILABLE",
            "active_counts_by_posture": {
                "restricted": 0,
                "offline": 0,
                "open": 0,
                "unknown": 0,
            },
            "open_examples": [],
        }

    active_ids = workspace_view.active_ids
    active_snapshots = [
        snapshot for snapshot in workspace_view.snapshots if snapshot.workspace_id in active_ids
    ]
    counts: Counter[str] = Counter()
    active_restricted_templates: list[str] = []
    open_examples: list[dict[str, object]] = []
    for snapshot in active_snapshots:
        posture = snapshot.network_posture
        counts[posture or "unknown"] += 1
        if posture == "restricted" and snapshot.allowlist_templates:
            active_restricted_templates.extend(snapshot.allowlist_templates)
        if posture == "open" and len(open_examples) < 5:
            open_examples.append(
                {
                    "workspace_id": snapshot.workspace_id,
                    "status": snapshot.status,
                    "pr_url": snapshot.pr_url,
                }
            )
    counts["unknown"] += len(active_ids - {snapshot.workspace_id for snapshot in active_snapshots})

    active_counts = {
        "restricted": counts.get("restricted", 0),
        "offline": counts.get("offline", 0),
        "open": counts.get("open", 0),
        "unknown": counts.get("unknown", 0),
    }
    open_count = active_counts["open"]
    return {
        "ok": True,
        "status": "warn" if open_count else "ok",
        "reason": (
            "NETWORK_POSTURE_OPEN_ACTIVE" if open_count else "NETWORK_POSTURE_NO_ACTIVE_OPEN"
        ),
        "active_counts_by_posture": active_counts,
        "active_restricted_templates": sorted(set(active_restricted_templates)),
        "deferred_enforcement_note": (
            "Destination-level filtering is deferred in the local Docker backend; "
            "allowlist_templates are metadata-only intent."
        ),
        "open_examples": open_examples,
    }


def _runtime_workspaces_from_view(
    workspace_view: WorkspaceIdView,
) -> tuple[RuntimeWorkspace, ...]:
    by_id = {
        snapshot.workspace_id: RuntimeWorkspace(
            workspace_id=snapshot.workspace_id,
            status=snapshot.status,
            compose_project_name=snapshot.compose_project_name,
            compose_file_path=snapshot.compose_file_path,
            pr_url=snapshot.pr_url,
        )
        for snapshot in workspace_view.snapshots
    }
    # Runtime recovery decisions require lifecycle metadata such as status and
    # PR URL; active_ids-only views are retained for orphan-resource checks.
    return tuple(by_id[workspace_id] for workspace_id in sorted(by_id))


def _orphan_resources_reason(reason: object) -> str:
    if reason == "ORPHANS_PRESENT":
        return "ORPHAN_RESOURCES_PRESENT"
    return str(reason or "UNKNOWN")


def _orphan_resources_cleanup_readiness(
    payload: Mapping[str, object],
    *,
    auto_cleanup_orphans: bool = False,
) -> dict[str, object]:
    reason = _orphan_resources_reason(payload.get("reason"))
    action = payload.get("action")
    # Only the orphan-reaping path actually deletes resources; readiness and the
    # idle/unavailable branches stay report-only so operators are not told the
    # worker will act when no orphans are present or detection degraded.
    if bool(payload.get("orphan_count")):
        if auto_cleanup_orphans:
            # Do not forward the legacy "run manual cleanup" action: with reaping
            # enabled the worker tears the orphans down itself, so a manual-cleanup
            # action would contradict ``dry_run_only`` being False.
            orphan_action: str = ORPHAN_REAPING_ACTION
        elif isinstance(action, str) and action:
            orphan_action = action
        else:
            orphan_action = "Review the listed AWF resources before running cleanup."
        return {
            "ready": False,
            "status": "blocked",
            "reason": reason,
            "action": orphan_action,
            "dry_run_only": not auto_cleanup_orphans,
        }
    if payload.get("status") == "ok":
        return {
            "ready": True,
            "status": "ready",
            "reason": reason,
            "action": "No orphan AWF resources were detected; no cleanup action is required.",
            "dry_run_only": True,
        }
    return {
        "ready": False,
        "status": str(payload.get("status") or "unknown"),
        "reason": reason,
        "action": action
        if isinstance(action, str) and action
        else "Restore detector dependencies and re-run orphan resource detection.",
        "dry_run_only": True,
    }


def _workspace_id_from_project(project: str) -> str | None:
    return workspace_id_from_project(project)


async def _default_workspace_id_lookup(
    database_url: str,
    *,
    legacy_open_default_cutoff: datetime | None = None,
) -> WorkspaceIdView:
    """Read live workspace ids from the control-plane DB.

    Failures (missing tables, unreachable host, auth errors, or even a
    malformed URL that trips engine construction) collapse to
    ``available=False`` so the orphan check can degrade gracefully instead
    of raising.
    """

    stmt = select(
        Workspace.id,
        Workspace.status,
        Workspace.updated_at,
        Workspace.compose_project_name,
        Workspace.compose_file_path,
        Workspace.pr_url,
        Workspace.resolved_profile,
        Workspace.created_at,
    ).where(Workspace.status.in_(KNOWN_WORKSPACE_STATUSES))
    engine = None
    try:
        engine = make_engine(database_url)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
    except Exception:
        return WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=False,
        )
    finally:
        if engine is not None:
            await engine.dispose()

    active: set[str] = set()
    terminal: set[str] = set()
    snapshots: list[WorkspaceLifecycleSnapshot] = []
    for (
        ws_id,
        status,
        updated_at,
        compose_project_name,
        compose_file_path,
        pr_url,
        resolved_profile,
        created_at,
    ) in rows:
        ws_id_str = str(ws_id)
        status_str = str(status)
        posture = _status_network_posture_from_profile_snapshot(
            resolved_profile,
            created_at,
            legacy_open_default_cutoff=legacy_open_default_cutoff,
        )
        templates = egress_allowlist_templates_from_profile_snapshot(resolved_profile)
        snapshots.append(
            WorkspaceLifecycleSnapshot(
                workspace_id=ws_id_str,
                status=status_str,
                updated_at=updated_at,
                compose_project_name=(
                    str(compose_project_name) if compose_project_name is not None else None
                ),
                compose_file_path=(
                    str(compose_file_path) if compose_file_path is not None else None
                ),
                pr_url=str(pr_url) if pr_url is not None else None,
                network_posture=posture,
                allowlist_templates=tuple(templates) if templates is not None else (),
            )
        )
        if status_str in ACTIVE_WORKSPACE_STATUSES:
            active.add(ws_id_str)
        elif status_str in TERMINAL_WORKSPACE_STATUSES:
            terminal.add(ws_id_str)
    return WorkspaceIdView(
        active_ids=frozenset(active),
        terminal_ids=frozenset(terminal),
        available=True,
        snapshots=tuple(snapshots),
    )


def _status_network_posture_from_profile_snapshot(
    resolved_profile: object,
    created_at: object,
    *,
    legacy_open_default_cutoff: datetime | None = None,
) -> NetworkPosture | None:
    posture = network_posture_from_profile_snapshot(resolved_profile)
    if posture == "open" and _is_legacy_open_default_workspace(
        created_at,
        legacy_open_default_cutoff=legacy_open_default_cutoff,
    ):
        return None
    return posture


def _is_legacy_open_default_workspace(
    created_at: object,
    *,
    legacy_open_default_cutoff: datetime | None,
) -> bool:
    if legacy_open_default_cutoff is None:
        return False
    if not isinstance(created_at, datetime):
        return True
    return _utc_datetime(created_at) < _utc_datetime(legacy_open_default_cutoff)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _http_get(url: str, *, timeout: float) -> HttpResponse:
    async with httpx.AsyncClient() as client:
        return await client.get(url, timeout=timeout)


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    timeout: float,
    env: Mapping[str, str],
) -> CompletedProcessLike:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )


def _run_docker_command(
    args: list[str],
    *,
    settings: ServiceSettings,
    run_subprocess: SubprocessRun,
) -> CompletedProcessLike | Exception:
    env = {**os.environ, "DOCKER_HOST": settings.docker_host}
    try:
        return run_subprocess(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT_SECONDS,
            env=env,
        )
    except Exception as exc:
        return exc


def _docker_result_to_check(
    result: CompletedProcessLike | Exception,
    *,
    fail_reason: str,
    detail_prefix: str = "",
) -> CheckPayload:
    if isinstance(result, FileNotFoundError):
        return _fail("DOCKER_CLI_NOT_FOUND", "docker binary not found on PATH")
    if isinstance(result, subprocess.TimeoutExpired):
        return _fail(fail_reason, _truncate(f"{detail_prefix}{result}"))
    if isinstance(result, Exception):
        return _fail(fail_reason, _truncate(f"{detail_prefix}{type(result).__name__}: {result}"))
    if result.returncode != 0:
        return _fail(fail_reason, _truncate(f"{detail_prefix}{result.stderr or result.stdout}"))
    return _ok(version=result.stdout.strip() or None)


def _docker_socket_path(docker_host: str) -> Path | None:
    prefix = "unix://"
    if not docker_host.startswith(prefix):
        return None
    return Path(docker_host.removeprefix(prefix))


def _ok(*, version: str | None = None) -> CheckPayload:
    payload: CheckPayload = {"ok": True, "status": "ok"}
    if version is not None:
        payload["version"] = version
    return payload


def _fail(reason: str, detail: str | None = None) -> CheckPayload:
    payload: CheckPayload = {"ok": False, "status": "fail", "reason": reason}
    if detail:
        payload["detail"] = detail
    return payload


def _truncate(value: str, *, limit: int = 240) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _redacted_truncated_exception_detail(exc: Exception, *, limit: int = 240) -> str:
    return _truncate(redact_audit_text(f"{type(exc).__name__}: {exc}"), limit=limit)
