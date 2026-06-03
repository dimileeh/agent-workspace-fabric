"""DX smoke proof: validate local service, profile, PR path, and console links."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from awf.common.profile_paths import PROFILE_MARKER_PATHS
from awf.service.config import ServiceSettings

ServiceCollector = Callable[[ServiceSettings], Awaitable[dict[str, Any]]]
AuthCollector = Callable[..., dict[str, Any]]
ProfilePreview = Callable[..., Any]
ConfigResolver = Callable[[ServiceSettings], dict[str, Any]]
ConsoleChecker = Callable[[str], Awaitable[bool]]
DEFAULT_LOCAL_CONSOLE_URL = "http://localhost:3000"
_PROFILE_MARKER_PATHS: tuple[str, ...] = PROFILE_MARKER_PATHS


def _project_has_awf_profile(path: Path) -> bool:
    """Check whether the project root contains a workspace profile marker file."""
    return any((path / rel).is_file() for rel in _PROFILE_MARKER_PATHS)


async def collect_smoke_report(
    *,
    project: Path,
    settings: ServiceSettings,
    mocked_local: bool = False,
    demo_path: Path | None = None,
    service_collector: ServiceCollector | None = None,
    auth_collector: AuthCollector | None = None,
    profile_preview: ProfilePreview | None = None,
    config_resolver: ConfigResolver | None = None,
    console_checker: ConsoleChecker | None = None,
) -> dict[str, Any]:
    """Run smoke checks for a project and return a phase-by-phase status report.

    Args:
        project: Path to the project root under inspection.
        settings: Service settings for resolving service/API/console URLs.
        mocked_local: If true, marks expected-live failures as warnings where safe.
        demo_path: Optional fallback project root used when ``project`` is missing.
        service_collector: Optional override for service health checks.
        auth_collector: Optional override for provider readiness checks.
        profile_preview: Optional override for profile inspection behavior.
        config_resolver: Optional override for API/console configuration.
        console_checker: Optional override for console URL reachability checks.

    Returns:
        Structured smoke report including phase statuses, evidence, and next actions.

    """
    mode = "mocked_local" if mocked_local else "live"
    phases: list[dict[str, Any]] = []
    overall: list[str] = []

    phases.append(await _phase_service_readiness(settings, service_collector))
    overall.append(phases[-1]["status"])

    phases.append(_phase_auth_readiness(settings, mocked_local, auth_collector))
    overall.append(phases[-1]["status"])

    if project.exists() and (demo_path is None or _project_has_awf_profile(project)):
        effective_project = project
    elif demo_path is not None and demo_path.exists():
        effective_project = demo_path
    else:
        missing = demo_path if demo_path is not None else project
        profile_phase = {
            "name": "profile_preview",
            "status": "fail",
            "reason_code": "SMOKE_DEMO_PROJECT_MISSING",
            "message": f"Project path does not resolve: {missing}",
            "evidence": {
                "project": str(project),
                "demo_path": str(demo_path) if demo_path else None,
            },
            "action": "Provide a valid --project path or --demo-path pointing to a valid project.",
        }
        phases.append(profile_phase)
        overall.append("fail")
        phases.append(_phase_validation([]))
        overall.append("warn")
        phases.append(
            {
                "name": "workspace_request",
                "status": "fail",
                "reason_code": "SMOKE_WORKSPACE_REQUEST_FAILED",
                "message": "No valid project to generate workspace request from.",
                "evidence": {},
                "action": "Provide a valid --project path or --demo-path.",
            }
        )
        overall.append("fail")
        phases.append(_phase_pr_monitor(mocked_local, settings))
        overall.append(phases[-1]["status"])
        resolved_config = _resolve_config(settings, config_resolver)
        console_phase, console_links = await _phase_console_links(
            resolved_config,
            console_checker=console_checker,
        )
        phases.append(console_phase)
        overall.append(console_phase["status"])
        status = _compute_overall_status(overall)
        next_actions = _collect_next_actions(phases)
        return {
            "status": status,
            "project": str(project),
            "mode": mode,
            "phases": phases,
            "console_links": console_links,
            "next_actions": next_actions,
        }

    profile_preview_obj, profile_phase = _phase_profile_preview(
        effective_project, mocked_local, profile_preview
    )
    phases.append(profile_phase)
    overall.append(str(profile_phase["status"]))

    validation_commands = _extract_validation_commands(profile_preview_obj)
    phases.append(_phase_validation(validation_commands))
    overall.append(phases[-1]["status"])

    phases.append(_phase_workspace_request(profile_preview_obj, effective_project))
    overall.append(phases[-1]["status"])

    phases.append(_phase_pr_monitor(mocked_local, settings))
    overall.append(phases[-1]["status"])

    resolved_config = _resolve_config(settings, config_resolver)
    console_phase, console_links = await _phase_console_links(
        resolved_config,
        console_checker=console_checker,
    )
    phases.append(console_phase)
    overall.append(console_phase["status"])

    status = _compute_overall_status(overall)
    next_actions = _collect_next_actions(phases)

    return {
        "status": status,
        "project": str(effective_project),
        "mode": mode,
        "phases": phases,
        "console_links": console_links,
        "next_actions": next_actions,
    }


async def _phase_service_readiness(
    settings: ServiceSettings,
    service_collector: ServiceCollector | None,
) -> dict[str, Any]:
    """Probe AWF local Core health for smoke readiness.

    Local Core health is a *hard* signal even in ``mocked_local`` mode: the
    no-token proof only relaxes the provider/PR (token + GitHub) requirements, so
    an unreachable or unhealthy Core always fails. A richer collector may report
    ``api`` and ``worker_db_substrate`` sub-signals; ``status == "ok"`` means
    API-up, and a ``worker_db_substrate`` value that is not healthy fails with a
    substrate-specific reason so the report proves Core health rather than merely
    echoing a URL.

    ``worker_db_substrate`` is the worker's *required dependency* (the
    control-plane DB the poll/claim loop reads), **not** worker-process liveness:
    a token-free HTTP probe cannot observe the worker container directly, and a
    real worker-container health signal (``awf service doctor``'s
    ``docker compose ps worker``) is Docker-dependent and outside this provider-
    free proof. So this phase proves the worker's substrate is reachable, not that
    a worker is currently polling. A plain ``{"status": "ok"}`` (no
    ``worker_db_substrate`` key) stays ``ok`` for backward compatibility with
    injected collectors.
    """
    try:
        collector = service_collector or _default_service_collector
        result = await collector(settings)
    except Exception as exc:
        return {
            "name": "service_readiness",
            "status": "fail",
            "reason_code": "SMOKE_SERVICE_UNREACHABLE",
            "message": f"AWF local service is unreachable: {exc}",
            "evidence": {"api_url": settings.api_base_url, "error": str(exc)},
            "action": "Run `awf service bootstrap` to start the local service stack.",
        }

    svc_status = result.get("status", "unreachable")
    api_status = result.get("api", svc_status)
    worker_db_substrate = result.get("worker_db_substrate")
    api_up = svc_status == "ok" or api_status == "ok"
    if not api_up:
        return {
            "name": "service_readiness",
            "status": "fail",
            "reason_code": "SMOKE_SERVICE_UNREACHABLE",
            "message": "AWF local service health check did not pass.",
            "evidence": {
                "api_url": settings.api_base_url,
                "status": svc_status,
                "api": api_status,
                "worker_db_substrate": (
                    worker_db_substrate if worker_db_substrate is not None else "unknown"
                ),
            },
            "action": "Run `awf service bootstrap` or inspect service logs.",
        }

    # API is up. If the collector also reports a worker-substrate signal, it must
    # be healthy — a reachable API with an unreachable worker DB substrate is not a
    # healthy Core, even in mocked-local mode (the no-token proof keeps Core health
    # hard). This proves the worker's DB *dependency*, not worker-process liveness.
    if worker_db_substrate is not None and worker_db_substrate != "ok":
        return {
            "name": "service_readiness",
            "status": "fail",
            "reason_code": "SMOKE_WORKER_UNAVAILABLE",
            "message": "AWF API is reachable but the worker DB substrate is not healthy.",
            "evidence": {
                "api_url": settings.api_base_url,
                "status": svc_status,
                "api": api_status,
                "worker_db_substrate": worker_db_substrate,
            },
            "action": (
                "Run `awf service bootstrap` or inspect worker/DB logs; the worker "
                "poll/claim loop needs a reachable control-plane database."
            ),
        }
    return {
        "name": "service_readiness",
        "status": "ok",
        "reason_code": "SMOKE_SERVICE_READY",
        "message": (
            "AWF local Core health check passed (API up; worker DB substrate reachable). "
            "Worker-process liveness is not probed by this provider-free check."
        ),
        "evidence": {
            "api_url": settings.api_base_url,
            "status": "ok",
            "api": api_status,
            "worker_db_substrate": (
                worker_db_substrate if worker_db_substrate is not None else "ok"
            ),
        },
        "action": "No action required.",
    }


def _phase_auth_readiness(
    settings: ServiceSettings,
    mocked_local: bool,
    auth_collector: AuthCollector | None,
) -> dict[str, Any]:
    """Evaluate provider readiness and derive auth phase status."""
    try:
        collector = auth_collector or _default_auth_collector
        result = collector(settings)
    except Exception as exc:
        return {
            "name": "auth_readiness",
            "status": "warn" if mocked_local else "fail",
            "reason_code": "SMOKE_AUTH_UNAVAILABLE",
            "message": f"Auth/provider readiness check failed: {exc}",
            "evidence": {"error": str(exc)},
            "action": "Verify provider credentials are configured.",
        }

    auth_status = result.get("status", "fail")
    providers = result.get("providers", {})
    agent_providers = {
        name: p
        for name, p in providers.items()
        if name != "docker" and p.get("credential_scope") != "not_observed"
    }
    usable = sum(1 for p in agent_providers.values() if p.get("ok", False))
    total = len(agent_providers)

    if auth_status == "ok" and usable == total and total > 0:
        return {
            "name": "auth_readiness",
            "status": "ok",
            "reason_code": "SMOKE_AUTH_READY",
            "message": f"All {total} configured provider(s) are ready.",
            "evidence": {"providers_ready": usable, "providers_total": total},
            "action": "No action required.",
        }
    if usable > 0:
        return {
            "name": "auth_readiness",
            "status": "warn",
            "reason_code": "SMOKE_AUTH_PARTIAL",
            "message": f"{usable}/{total} provider(s) ready; at least one is usable.",
            "evidence": {
                "providers_ready": usable,
                "providers_total": total,
                "providers": {
                    name: {
                        "ok": p.get("ok", False),
                        "status": p.get("status", "unknown"),
                        "reason": p.get("reason", ""),
                    }
                    for name, p in providers.items()
                },
            },
            "action": "Some providers are not ready. In mocked-local mode this does not block the smoke run.",
        }
    return {
        "name": "auth_readiness",
        "status": "warn" if mocked_local else "fail",
        "reason_code": "SMOKE_AUTH_UNAVAILABLE",
        "message": "No usable provider found.",
        "evidence": {"providers_ready": 0, "providers_total": total},
        "action": "Configure at least one agent provider (Codex, Claude, Cursor, Gemini, OpenCode, Grok).",
    }


def _phase_profile_preview(
    project: Path,
    mocked_local: bool,
    profile_preview: ProfilePreview | None,
) -> tuple[Any, dict[str, Any]]:
    """Collect the smoke profile preview phase result and metadata."""
    has_disk_profile = profile_preview is None and _project_has_awf_profile(project)
    try:
        if profile_preview is not None:
            preview_fn = profile_preview
        elif has_disk_profile:
            preview_fn = _default_disk_profile_preview
        else:
            preview_fn = _default_profile_preview
        preview = preview_fn(project)
    except Exception as exc:
        if has_disk_profile:
            action = (
                "Fix the on-disk workspace profile (.awf/workspace.yml, .awf/workspace.yaml, "
                "awf.workspace.yml, or awf.workspace.yaml) so it passes schema validation and lint "
                "checks."
            )
        else:
            action = "Verify the project is a valid AWF workspace project."
        return None, {
            "name": "profile_preview",
            "status": "fail",
            "reason_code": "SMOKE_PROFILE_PREVIEW_FAILED",
            "message": f"Project profile preview failed: {exc}",
            "evidence": {"project": str(project), "error": str(exc)},
            "action": action,
        }

    detected_template = getattr(getattr(preview, "draft", None), "template", "unknown")
    if detected_template == "unknown":
        return preview, {
            "name": "profile_preview",
            "status": "warn" if mocked_local else "fail",
            "reason_code": "SMOKE_PROFILE_NOT_DETECTED",
            "message": "No project template was detected.",
            "evidence": {"project": str(project)},
            "action": "Add project structure files (pyproject.toml, package.json, etc.) or create .awf/workspace.yml manually.",
        }

    confidence = getattr(getattr(preview, "inspection", None), "confidence", "unknown")
    return preview, {
        "name": "profile_preview",
        "status": "ok",
        "reason_code": "SMOKE_PROFILE_READY",
        "message": f"Detected template '{detected_template}' with confidence '{confidence}'.",
        "evidence": {
            "project": str(project),
            "template": detected_template,
            "confidence": confidence,
        },
        "action": "No action required.",
    }


def _extract_validation_commands(preview: Any) -> list[str]:
    """Extract ordered validate commands from a preview profile payload."""
    try:
        profile_phases = getattr(getattr(preview, "draft", None), "profile", None)
        if profile_phases is None:
            return []
        validate_commands = getattr(
            getattr(profile_phases, "phases", None),
            "validate_commands",
            None,
        )
        if validate_commands is None:
            return []
        return [cmd.command if hasattr(cmd, "command") else str(cmd) for cmd in validate_commands]
    except Exception:
        return []


def _phase_validation(validation_commands: list[str]) -> dict[str, Any]:
    """Build validation phase status from discovered commands."""
    if validation_commands:
        return {
            "name": "validation",
            "status": "ok",
            "reason_code": "SMOKE_VALIDATION_READY",
            "message": f"{len(validation_commands)} validation command(s) detected in profile.",
            "evidence": {"commands": validation_commands},
            "action": "No action required.",
        }
    return {
        "name": "validation",
        "status": "warn",
        "reason_code": "SMOKE_VALIDATION_MISSING",
        "message": "No validation commands found in the project profile.",
        "evidence": {"commands": []},
        "action": "Add validation commands to .awf/workspace.yml or project profile.",
    }


def _phase_workspace_request(
    preview: Any,
    project: Path,
) -> dict[str, Any]:
    """Build the workspace-request phase from the preview profile."""
    try:
        from awf.profiles.onboarding import _smoke_request

        draft_profile = getattr(getattr(preview, "draft", None), "profile", None)
        if draft_profile is not None:
            smoke_request = _smoke_request(project, draft_profile)
            if isinstance(smoke_request, dict):
                return {
                    "name": "workspace_request",
                    "status": "ok",
                    "reason_code": "SMOKE_WORKSPACE_REQUEST_READY",
                    "message": "Workspace smoke request generated successfully.",
                    "evidence": {
                        "has_repo": "repo" in smoke_request,
                        "has_task": "task" in smoke_request,
                        "has_workspace": "workspace" in smoke_request,
                        "has_validation": "validation" in smoke_request,
                    },
                    "action": "No action required.",
                }
    except Exception as exc:
        return {
            "name": "workspace_request",
            "status": "fail",
            "reason_code": "SMOKE_WORKSPACE_REQUEST_FAILED",
            "message": f"Could not generate a workspace smoke request from the profile: {exc}",
            "evidence": {"error": str(exc)},
            "action": "Verify .awf/workspace.yml is valid and the project profile can be loaded.",
        }

    return {
        "name": "workspace_request",
        "status": "fail",
        "reason_code": "SMOKE_WORKSPACE_REQUEST_FAILED",
        "message": "Could not generate a workspace smoke request from the profile.",
        "evidence": {},
        "action": "Verify .awf/workspace.yml is valid and the project profile can be loaded.",
    }


def _phase_pr_monitor(
    mocked_local: bool,
    settings: ServiceSettings,
) -> dict[str, Any]:
    """Report PR monitor readiness for mocked-local versus live mode."""
    if mocked_local:
        return {
            "name": "pr_monitor",
            "status": "ok",
            "reason_code": "SMOKE_PR_MOCKED_LOCAL",
            "message": "PR creation and monitoring are mocked in local mode.",
            "evidence": {"mode": "mocked_local"},
            "action": "For live PR creation, omit --mocked-local and ensure GitHub credentials are configured.",
        }

    token = settings.github_token
    if token is not None and isinstance(token, str) and len(token.strip()) > 0:
        return {
            "name": "pr_monitor",
            "status": "ok",
            "reason_code": "SMOKE_PR_READY",
            "message": "PR creation and monitoring path is available with configured GitHub credentials.",
            "evidence": {"mode": "live", "github_token_configured": True},
            "action": "No action required.",
        }

    return {
        "name": "pr_monitor",
        "status": "warn",
        "reason_code": "SMOKE_PR_UNAVAILABLE",
        "message": "Live PR path not verified; use --mocked-local for a deterministic local smoke run.",
        "evidence": {"mode": "live"},
        "action": "Ensure service is running and GitHub credentials are configured for live PR testing.",
    }


def _resolve_config(
    settings: ServiceSettings,
    config_resolver: ConfigResolver | None,
) -> dict[str, Any]:
    """Resolve smoke runtime config, with optional override support."""
    resolver = config_resolver or _default_config_resolver
    return resolver(settings)


async def _phase_console_links(
    resolved_config: dict[str, Any],
    *,
    console_checker: ConsoleChecker | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve configured or local console URL and build smoke console-link evidence."""
    api_base = resolved_config.get("api_base_url", "http://localhost:8000")
    console_url = resolved_config.get("console_url")
    links: dict[str, str] = {
        "api_docs": f"{api_base.rstrip('/')}/docs",
    }

    if console_url:
        configured_console_url = str(console_url)
        links["ui"] = configured_console_url
        checker = console_checker or _default_console_checker
        if not await checker(configured_console_url):
            return {
                "name": "console_links",
                "status": "warn",
                "reason_code": "SMOKE_CONSOLE_UNAVAILABLE",
                "message": "Configured console URL was not reachable.",
                "evidence": {
                    "ui": configured_console_url,
                    "api_docs": f"{api_base.rstrip('/')}/docs",
                    "source": "configured",
                },
                "action": ("Start the console or set AWF_CONSOLE_URL to a reachable console URL."),
            }, links
        return {
            "name": "console_links",
            "status": "ok",
            "reason_code": "SMOKE_CONSOLE_READY",
            "message": (
                "Configured console is reachable: "
                f"UI={configured_console_url}, API docs={api_base}/docs"
            ),
            "evidence": {
                "ui": configured_console_url,
                "api_docs": f"{api_base.rstrip('/')}/docs",
                "source": "configured",
            },
            "action": "No action required.",
        }, links

    inferred_console_url = DEFAULT_LOCAL_CONSOLE_URL
    links["ui"] = inferred_console_url
    checker = console_checker or _default_console_checker
    if await checker(inferred_console_url):
        return {
            "name": "console_links",
            "status": "ok",
            "reason_code": "SMOKE_CONSOLE_READY",
            "message": (
                "Default local console is reachable: "
                f"UI={inferred_console_url}, API docs={api_base}/docs"
            ),
            "evidence": {
                "ui": inferred_console_url,
                "api_docs": f"{api_base.rstrip('/')}/docs",
                "source": "default_local_probe",
            },
            "action": "No action required.",
        }, links

    return {
        "name": "console_links",
        "status": "warn",
        "reason_code": "SMOKE_CONSOLE_UNAVAILABLE",
        "message": "Default local console was not reachable.",
        "evidence": {
            "ui": inferred_console_url,
            "api_docs": f"{api_base.rstrip('/')}/docs",
            "source": "default_local_probe",
        },
        "action": (
            "Start the console with `npm --prefix apps/console run dev` or set "
            "AWF_CONSOLE_URL to a reachable console URL."
        ),
    }, links


def _compute_overall_status(phase_statuses: list[str]) -> str:
    """Collapse per-phase status values into the overall smoke status."""
    has_fail = any(s == "fail" for s in phase_statuses)
    has_warn = any(s == "warn" for s in phase_statuses)
    if has_fail:
        return "fail"
    if has_warn:
        return "warn"
    return "ok"


def _collect_next_actions(phases: list[dict[str, Any]]) -> list[str]:
    """Collect non-no-op actionable hints from smoke phases."""
    actions: list[str] = []
    for phase in phases:
        action = phase.get("action", "")
        if action and action != "No action required.":
            actions.append(action)
    return actions


async def _default_service_collector(settings: ServiceSettings) -> dict[str, Any]:
    """Probe AWF local Core health and return an api/worker-substrate status map.

    ``/healthz`` is dependency-free liveness ("the API process answered"). To
    prove Core health rather than mere liveness, also consult ``/readyz`` and read
    its ``checks.db.ok`` sub-check — the worker is a DB poll/claim loop, so DB
    reachability is the token-free signal that the worker's *substrate dependency*
    is up. ``/readyz`` returns its JSON body even on 503 (its *overall* status is
    503 whenever a provider token is unconfigured), so the DB sub-check is readable
    without provider tokens; the provider/``agent_readiness`` parts are
    intentionally ignored to keep the probe provider-free. Docker-dependent
    ``/readyz`` checks are *not* required for the no-token proof — the substrate
    proven here is the DB; the live smoke path covers docker/provisioning.
    ``/readyz`` failures degrade the signal to ``fail`` rather than crashing.

    The returned ``worker_db_substrate`` value is deliberately *not* worker-process
    liveness: a provider-free HTTP probe cannot observe the worker container. A
    real worker-container health signal exists in ``awf service doctor``
    (``docker compose ps worker``) but is Docker-dependent and outside this proof.
    """
    import httpx

    base = settings.api_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=5.0) as client:
        health = await client.get(f"{base}/healthz")
        if health.status_code != 200:
            return {"status": "unreachable", "api": "fail", "worker_db_substrate": "unknown"}
        substrate_status = await _probe_worker_db_substrate(client, base)

    if substrate_status == "ok":
        return {"status": "ok", "api": "ok", "worker_db_substrate": "ok"}
    return {"status": "degraded", "api": "ok", "worker_db_substrate": substrate_status}


async def _probe_worker_db_substrate(client: Any, base: str) -> str:
    """Return the worker-DB-substrate signal from ``/readyz`` ``checks.db.ok``.

    Reads the DB sub-check regardless of the ``/readyz`` HTTP status (the body is
    returned on 503 too). Any failure to reach or parse ``/readyz`` degrades the
    signal to ``fail`` so the proof never reports a false-green. This proves the
    worker's DB dependency is reachable, not that a worker process is polling.
    """
    import httpx

    try:
        ready = await client.get(f"{base}/readyz")
        db_ok = bool(ready.json()["checks"]["db"]["ok"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        # Transport failure, non-JSON body, or a malformed/missing db sub-check:
        # treat the worker substrate as unproven (fail), never a crash.
        return "fail"
    return "ok" if db_ok else "fail"


async def _default_console_checker(url: str) -> bool:
    """Return whether a URL responds as reachable console endpoint."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(url)
    except (httpx.HTTPError, httpx.InvalidURL):
        return False
    return 200 <= response.status_code < 400


def _default_auth_collector(settings: ServiceSettings) -> dict[str, Any]:
    """Resolve current provider readiness from the auth provider subsystem."""
    from awf.service.provider_readiness import collect_agent_readiness

    return collect_agent_readiness(settings)


def _default_profile_preview(project: Path) -> Any:
    """Return an onboarding-generated preview for projects without an on-disk profile."""
    from awf.profiles.onboarding import preview_project_onboarding

    return preview_project_onboarding(project)


def _default_disk_profile_preview(project: Path) -> Any:
    """Resolve a workspace profile from disk and return a smoke preview object."""
    from awf.common.git_remote import detect_repo_url_from_checkout
    from awf.profiles import resolve_workspace_profile
    from awf.profiles.onboarding import preview_workspace_profile

    if not _project_has_awf_profile(project):
        raise FileNotFoundError(f"no on-disk workspace profile marker file found in {project}")

    resolution = resolve_workspace_profile(
        worktree_path=project,
        repo_url=detect_repo_url_from_checkout(project),
    )
    return preview_workspace_profile(project, resolution)


def _default_config_resolver(settings: ServiceSettings) -> dict[str, Any]:
    """Build smoke config values consumed by console-link reporting."""
    result: dict[str, Any] = {
        "api_base_url": settings.api_base_url,
    }
    if settings.console_url is not None:
        result["console_url"] = settings.console_url
    return result
