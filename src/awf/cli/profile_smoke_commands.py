"""Profile and smoke-test CLI command groups."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from awf.cli.common import (
    OutputFormat,
    _emit,
    _emit_profile_preview_pretty,
    _emit_smoke_pretty,
)

_DX_HELP = (
    "DX smoke proof: validate local Core health, profile, and PR path. "
    "Use --mocked-local for a no-token local proof that demonstrates API health "
    "and worker heartbeat liveness without a provider token or GitHub access."
)
profile_app = typer.Typer(help="Workspace profile inspection.")
smoke_app = typer.Typer(help=_DX_HELP)


@profile_app.command("preview")
def profile_preview(
    path: str = typer.Argument(..., help="Path to a checked-out repository."),
    profile_ref: str = typer.Option("auto", "--profile"),
    validation_command: list[str] = typer.Option([], "--validation-command", help="Repeatable."),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Preview the resolved workspace profile for a local checkout."""
    from pathlib import Path

    from awf.common.git_remote import detect_repo_url_from_checkout
    from awf.profiles.resolver import resolve_workspace_profile

    resolved_path = Path(path).expanduser().resolve()
    resolution = resolve_workspace_profile(
        worktree_path=resolved_path,
        profile_ref=profile_ref,
        validation_commands=validation_command,
        repo_url=detect_repo_url_from_checkout(resolved_path),
    )
    payload = resolution.model_dump(mode="json", by_alias=True)
    if fmt == OutputFormat.pretty:
        _emit_profile_preview_pretty(payload)
    else:
        _emit(payload, fmt)


@profile_app.command("doctor")
def profile_doctor(
    repo: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        help="Path to a checked-out repository.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Run a real profile-readiness preflight (resolve, lint, secrets, egress, images).

    Unlike ``awf smoke`` (which can run mocked), this resolves the RESOLVED profile
    and runs the *same* probes provisioning uses, from the same host context — no
    agent run, no PR, no workspace creation. Use it before onboarding to catch
    profile/runtime gaps (notably ``SECRET_LEASE_SOURCE_MISSING``).
    """
    from awf.common.git_remote import detect_repo_url_from_checkout
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.profile_doctor import collect_profile_doctor_report

    resolved = repo.expanduser().resolve()
    # Probe secret leases against the SAME host_home the worker uses
    # (build_worker_runtime: Path(settings.host_home)), not Path.home(). When
    # AWF_HOST_HOME points the service at a different credential home than the
    # shell's HOME, falling back to Path.home() would check the wrong directory
    # and produce false passes/failures despite advertising the worker's context.
    settings = resolve_service_settings()
    # Probe secret leases against the SAME effective env the worker uses. The
    # worker constructs its LocalSecretLeaseMountResolver with host_env=os.environ
    # from INSIDE the service container, where Compose forwards every provider
    # credential (e.g. BITBUCKET_API_TOKEN/BITBUCKET_EMAIL) from
    # docker/compose/.env. Source host_env from that same merged Compose view
    # (local_service_environ) rather than the bare caller shell, so a provider:
    # bitbucket (or any env-backed) lease provisioning would satisfy is not
    # falsely reported as SECRET_LEASE_SOURCE_MISSING when the credential lives in
    # the service env file but is not exported in the current shell.
    host_env = dict(local_service_environ())
    # The worker additionally exports settings.github_token into
    # GH_TOKEN/GITHUB_TOKEN before constructing the resolver (build_worker_runtime
    # -> _service_git_environment + _apply_service_git_environment). Mirror that
    # explicit forward so a token that resolves only through service settings (not
    # the env file) still satisfies a provider: github lease.
    if settings.github_token:
        host_env["GH_TOKEN"] = settings.github_token
        host_env["GITHUB_TOKEN"] = settings.github_token
    # Run the image probes against the SAME Docker daemon/config the worker's
    # compose pulls target. The worker selects its daemon from the resolved service
    # environment (AWF_DOCKER_HOST, materialised as DOCKER_HOST -- settings.docker_host
    # -- with DOCKER_CONFIG and other client controls from docker/compose/.env). A
    # bare image probe would inherit the caller shell instead and inspect the wrong
    # daemon, so a green doctor would not match the worker's actual pulls. Thread the
    # merged service env with DOCKER_HOST forced to the resolved daemon so a stray
    # caller DOCKER_HOST/DOCKER_CONTEXT cannot redirect the probe.
    docker_environ = {**host_env, "DOCKER_HOST": settings.docker_host}
    report = collect_profile_doctor_report(
        resolved,
        repo_url=detect_repo_url_from_checkout(resolved),
        host_home=Path(settings.host_home).expanduser().resolve(),
        host_env=host_env,
        # Probe the SAME agent runtime image the worker renders into every stack
        # (build_worker_runtime -> ComposeStackLauncher(agent_runtime_image=...)),
        # so a missing/private custom AWF_AGENT_RUNTIME_IMAGE fails preflight here
        # rather than at provision time.
        agent_runtime_image=settings.agent_runtime_image,
        docker_environ=docker_environ,
    )
    if fmt == OutputFormat.pretty:
        _emit_profile_doctor_pretty(report)
    else:
        _emit(report, fmt)
    if report["status"] == "fail":
        raise typer.Exit(code=1)


def _emit_profile_doctor_pretty(report: dict[str, object]) -> None:
    """Render a human-readable profile-doctor report (status + per-phase lines)."""
    from awf.service.report_shape import NO_ACTION

    status = report.get("status", "unknown")
    repo = report.get("repo", "unknown")
    typer.echo(f"AWF profile doctor: {status}")
    typer.echo(f"Repo: {repo}")

    phases = report.get("phases")
    if isinstance(phases, list) and phases:
        typer.echo("")
        typer.echo("Phases:")
        for phase in phases:
            if not isinstance(phase, dict):
                continue
            phase_status = phase.get("status", "unknown")
            name = phase.get("name", "unknown")
            message = phase.get("message", "")
            header = f"  [{phase_status}] {name}"
            typer.echo(f"{header}: {message}" if message else header)
            reason = phase.get("reason_code", "")
            if reason:
                typer.echo(f"        reason: {reason}")
            action = phase.get("action", "")
            if action and action not in {NO_ACTION, "none"}:
                typer.echo(f"        action: {action}")

    next_actions = report.get("next_actions")
    if isinstance(next_actions, list) and next_actions:
        typer.echo("")
        typer.echo("Next actions:")
        for action in next_actions:
            typer.echo(f"  - {action}")


@profile_app.command("init")
def profile_init(
    path: Path = typer.Argument(..., help="Path to the repository to inspect."),
    template: str = typer.Option("auto", "--template", help="Template override or auto."),
    write: bool = typer.Option(
        False,
        "--write",
        help="Write .awf/workspace.yml. Defaults to preview only.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing profile."),
    include_smoke_request: bool = typer.Option(
        False,
        "--include-smoke-request",
        help="Include an example workspace request body without launching it.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Inspect a project and preview or create a draft .awf/workspace.yml."""
    from awf.profiles.onboarding import preview_project_onboarding, write_workspace_profile

    try:
        preview = preview_project_onboarding(
            path.expanduser().resolve(),
            template=template,
            include_smoke_request=include_smoke_request,
        )
        payload = preview.to_dict()
        if write:
            written_path = write_workspace_profile(preview, force=force)
            payload["written_path"] = str(written_path)
    except FileExistsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    _emit(payload, fmt)


@smoke_app.command("run", help=_DX_HELP)
def smoke_run(
    project: Path = typer.Option(
        Path(),
        "--project",
        help="Path to the project to smoke.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
    mocked_local: bool = typer.Option(
        False,
        "--mocked-local",
        help=(
            "Run the no-token local proof: relax provider/PR requirements while "
            "keeping local Core (API + worker heartbeat) health a hard signal. "
            "Needs no provider token or GitHub access."
        ),
    ),
    demo_path: Path | None = typer.Option(
        None,
        "--demo-path",
        help="Fallback project path when --project has no profile.",
    ),
) -> None:
    """Run AWF smoke."""
    from awf.service.config import resolve_service_settings
    from awf.service.smoke import collect_smoke_report

    resolved = project.expanduser().resolve()
    resolved_demo = demo_path.expanduser().resolve() if demo_path is not None else None
    settings = resolve_service_settings()

    report = asyncio.run(
        collect_smoke_report(
            project=resolved,
            settings=settings,
            mocked_local=mocked_local,
            demo_path=resolved_demo,
        )
    )
    if fmt == OutputFormat.pretty:
        _emit_smoke_pretty(report)
    else:
        _emit(report, fmt)
    if report["status"] == "fail":
        raise typer.Exit(code=1)
