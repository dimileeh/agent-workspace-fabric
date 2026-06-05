"""Project and local-service initialization helpers for the AWF CLI."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import typer

from awf.cli.common import OutputFormat, _emit, _list_value, _mapping_value
from awf.service.smoke import _PROFILE_MARKER_PATHS as _PROJECT_PROFILE_MARKER_PATHS

_AWF_SOURCE_ROOT_ENV_MIGRATION_MARKERS = (
    "pyproject.toml",
    "src/awf/__init__.py",
    "compose.yaml",
    "docker/compose/local-service.yml",
)


class _EnvSeedMergeError(ValueError):
    """Raised when env seed merging cannot preserve dotenv semantics."""


_ENV_ASSIGNMENT_RE = re.compile(r"\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")

_ENV_COMMENT_WORD_RE = re.compile(r"[a-z0-9]+")

_ENV_COMMENT_KEY_IGNORE_WORDS = frozenset({"awf"})


def _run_init_project_onboarding(
    path: Path,
    *,
    include_smoke_request: bool,
    guided: bool | None,
    write_profile: bool,
    yes: bool,
    template: str,
    force: bool,
    fmt: OutputFormat,
) -> None:
    """Run repository onboarding checks and optionally write the workspace profile."""
    from awf.profiles.models import EgressMode
    from awf.profiles.onboarding import (
        customize_project_onboarding_preview,
        preview_project_onboarding,
        supported_onboarding_templates,
        write_workspace_profile,
    )
    from awf.service.config import (
        ServiceSettings,
        local_service_environ,
        resolve_service_settings,
    )
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty
    from awf.service.doctor.models import DoctorReport
    from awf.service.status import collect_service_status

    repository = path.expanduser().resolve()

    if not repository.exists():
        typer.echo(f"error: project path does not exist: {repository}", err=True)
        raise typer.Exit(code=2)
    if not repository.is_dir():
        typer.echo(f"error: project path is not a directory: {repository}", err=True)
        raise typer.Exit(code=2)

    existing_profile_path = _existing_project_profile_path(repository)
    if fmt == OutputFormat.json and guided is True:
        typer.echo("error: --guided cannot be used with --format json.", err=True)
        raise typer.Exit(code=2)
    if yes and not write_profile:
        typer.echo(
            "error: --yes requires --write-profile to approve a non-interactive profile write.",
            err=True,
        )
        raise typer.Exit(code=2)
    if write_profile and not yes and guided is not True:
        typer.echo(
            "error: --write-profile requires --yes for non-interactive writes "
            "or --guided for an interactive confirmation.",
            err=True,
        )
        raise typer.Exit(code=2)
    stdio_is_interactive = _stdio_is_interactive()
    if guided is True and not stdio_is_interactive:
        typer.echo(
            "error: --guided requires an interactive terminal; use "
            "--write-profile --yes for non-interactive writes.",
            err=True,
        )
        raise typer.Exit(code=2)

    service_status: dict[str, object]
    doctor_report: DoctorReport | None
    doctor_error: str | None = None
    try:
        settings = resolve_service_settings()
        service_env = local_service_environ()
        service_name = getattr(settings, "service_name", "unknown")

        async def _collect_reports() -> tuple[dict[str, object], DoctorReport | None, str | None]:
            """Collect service status and doctor diagnostics with one shared status read."""
            service_status_task = asyncio.create_task(
                collect_service_status(
                    settings,
                    strict_providers=frozenset(),
                    provider_environ=service_env,
                )
            )

            async def _collect_cached_service_status(
                settings: ServiceSettings,
                *,
                strict_providers: Iterable[str] | None = None,
                provider_environ: Mapping[str, str] | None = None,
                **_kwargs: object,
            ) -> dict[str, object]:
                """Serve the in-flight service status result to doctor diagnostics."""
                _ = settings, strict_providers, provider_environ, _kwargs
                return await service_status_task

            service_status_result, doctor_report = await asyncio.gather(
                service_status_task,
                collect_doctor_report(
                    settings,
                    strict_providers=frozenset(),
                    provider_environ=service_env,
                    environ=service_env,
                    status_collector=_collect_cached_service_status,
                ),
                return_exceptions=True,
            )

            service_status: dict[str, object]
            if isinstance(service_status_result, BaseException):
                service_status = {
                    "service": service_name,
                    "status": "fail",
                    "checks": {},
                    "agent_readiness": {"status": "fail"},
                    "detail": str(service_status_result),
                }
            else:
                service_status = service_status_result
            if isinstance(doctor_report, BaseException):
                return service_status, None, str(doctor_report)

            return service_status, doctor_report, None

        service_status, doctor_report, doctor_error = asyncio.run(_collect_reports())
    except Exception as exc:
        service_status = {
            "service": "awf",
            "status": "fail",
            "checks": {},
            "agent_readiness": {"status": "fail"},
            "detail": str(exc),
        }
        doctor_report = None
        doctor_error = str(exc)

    try:
        preview = preview_project_onboarding(
            repository,
            template=template,
            include_smoke_request=include_smoke_request,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        typer.echo(f"error: could not build onboarding preview: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    doctor_status = (
        getattr(doctor_report, "status", "fail") if doctor_report is not None else "fail"
    )
    service_ok = service_status.get("status") == "ok"
    doctor_ok = doctor_status != "fail"
    local_checks_ready = service_ok and doctor_ok
    auto_guided = (
        guided is None
        and fmt == OutputFormat.pretty
        and existing_profile_path is None
        and not write_profile
        and stdio_is_interactive
    )
    effective_guided = guided is True or auto_guided

    guided_wants_write = False
    if effective_guided:
        preview, guided_wants_write = _prompt_project_onboarding_choices(
            repository,
            preview=preview,
            include_smoke_request=include_smoke_request,
            supported_templates=supported_onboarding_templates(),
            egress_mode_type=EgressMode,
            preview_factory=preview_project_onboarding,
            customize_preview=customize_project_onboarding_preview,
        )

    should_write = guided_wants_write if effective_guided else write_profile
    written_path: Path | None = None
    if should_write:
        try:
            written_path = write_workspace_profile(preview, force=force)
        except FileExistsError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from None

    mode = "guided" if effective_guided else "write" if should_write else "preview"
    payload = _init_project_onboarding_payload(
        preview=preview,
        existing_profile_path=existing_profile_path,
        written_path=written_path,
        service_status=service_status,
        doctor_status=str(doctor_status),
        local_checks_ready=local_checks_ready,
        guided=effective_guided,
        mode=mode,
    )

    if fmt == OutputFormat.json:
        _emit(payload, fmt)
        return

    doctor_pretty = (
        render_doctor_pretty(doctor_report)
        if doctor_report is not None
        else f"AWF doctor: fail\n  detail: {doctor_error or 'doctor report unavailable'}\n"
    )
    _emit_init_project_onboarding_pretty(
        preview=preview,
        payload=payload,
        doctor_pretty=doctor_pretty,
        include_smoke_request=include_smoke_request,
    )


def _stdio_is_interactive() -> bool:
    """Return whether both stdin and stdout are attached to an interactive TTY."""
    stdin_isatty = getattr(sys.stdin, "isatty", lambda: False)
    stdout_isatty = getattr(sys.stdout, "isatty", lambda: False)
    return bool(stdin_isatty() and stdout_isatty())


def _prompt_project_onboarding_choices(
    repository: Path,
    *,
    preview: Any,
    include_smoke_request: bool,
    supported_templates: tuple[str, ...],
    egress_mode_type: Any,
    preview_factory: Any,
    customize_preview: Any,
) -> tuple[Any, bool]:
    """Prompt for template, egress, validation, and profile-write onboarding choices."""
    typer.echo("AWF init: guided project profile setup")
    typer.echo(f"Detected template: {preview.draft.template}")
    template_choice = typer.prompt("Template", default=preview.draft.template).strip()
    if template_choice not in supported_templates:
        typer.echo(
            "error: unsupported onboarding template "
            f"{template_choice!r}; choose one of {', '.join(supported_templates)}.",
            err=True,
        )
        raise typer.Exit(code=2)
    if template_choice != preview.draft.template:
        try:
            preview = preview_factory(
                repository,
                template=template_choice,
                include_smoke_request=include_smoke_request,
            )
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            typer.echo(f"error: could not build onboarding preview: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    egress_mode = (
        typer.prompt(
            "Egress mode",
            default=egress_mode_type.restricted.value,
        )
        .strip()
        .lower()
    )
    supported_egress = {mode.value for mode in egress_mode_type}
    if egress_mode not in supported_egress:
        typer.echo(
            "error: unsupported egress mode "
            f"{egress_mode!r}; choose one of {', '.join(sorted(supported_egress))}.",
            err=True,
        )
        raise typer.Exit(code=2)
    open_explanation = None
    if egress_mode == "open":
        open_explanation = typer.prompt(
            "Open egress explanation",
            default="Project setup or validation needs internet access.",
        ).strip()

    validation_commands: list[str] = []
    if not preview.draft.profile.phases.validate_commands and typer.confirm(
        "Add a validation command now?",
        default=False,
    ):
        while True:
            validation_command = typer.prompt("Validation command").strip()
            if validation_command:
                validation_commands.append(validation_command)
            if not typer.confirm("Add another validation command?", default=False):
                break

    preview = customize_preview(
        preview,
        egress_mode=egress_mode_type(egress_mode),
        open_explanation=open_explanation,
        validation_commands=validation_commands or None,
    )
    return preview, typer.confirm("Write .awf/workspace.yml?", default=True)


def _init_project_onboarding_payload(
    *,
    preview: Any,
    existing_profile_path: Path | None,
    written_path: Path | None,
    service_status: dict[str, object],
    doctor_status: str,
    local_checks_ready: bool,
    guided: bool,
    mode: str,
) -> dict[str, object]:
    """Build the structured project-onboarding payload for pretty and JSON output."""
    payload = cast(dict[str, object], preview.to_dict())
    profile_exists = existing_profile_path is not None or written_path is not None
    payload.update(
        {
            "mode": mode,
            "guided": guided,
            "selected_template": preview.draft.template,
            "profile_exists": profile_exists,
            "service_status": service_status,
            "doctor_status": doctor_status,
            "local_checks_ready": local_checks_ready,
            "next_steps": _init_project_next_steps(
                existing_profile_path=existing_profile_path,
                written_path=written_path,
            ),
        }
    )
    if existing_profile_path is not None:
        payload["existing_profile_path"] = str(existing_profile_path)
    if written_path is not None:
        payload["written_path"] = str(written_path)
    return payload


def _init_project_next_steps(
    *,
    existing_profile_path: Path | None,
    written_path: Path | None,
) -> list[str]:
    """Return context-aware follow-up commands for the onboarding result."""
    if written_path is not None:
        return [
            "Run `awf profile preview <path> --profile auto --format pretty` to inspect profile resolution.",
            "Run `awf smoke run --mocked-local --format pretty` for a local DX proof.",
        ]
    if existing_profile_path is not None:
        return [
            f"AWF profile already exists: {existing_profile_path}",
            "Run `awf profile preview <path> --profile auto --format pretty` to inspect profile resolution.",
            "Run `awf smoke run --mocked-local --format pretty` for a local DX proof.",
        ]
    return [
        "Run `awf init <path> --write-profile --yes` to create `.awf/workspace.yml` with detected defaults.",
        "Run `awf init <path> --guided` for a short interactive setup.",
        "Run `awf profile preview <path> --profile <name> --format pretty` to inspect profile resolution.",
    ]


def _emit_init_project_onboarding_pretty(
    *,
    preview: Any,
    payload: Mapping[str, object],
    doctor_pretty: str,
    include_smoke_request: bool,
) -> None:
    """Render the human-readable project-onboarding readiness report."""
    typer.echo("AWF init: local onboarding readiness check")
    typer.echo(f"  repository: {preview.path}")
    typer.echo(f"  detected profile template: {preview.draft.template}")
    service_status = _mapping_value(payload.get("service_status"))
    typer.echo(f"  service status: {service_status.get('status', 'unknown')}")
    typer.echo(f"  doctor status: {payload.get('doctor_status', 'unknown')}")
    typer.echo("")
    typer.echo(doctor_pretty, nl=False)

    written_path = payload.get("written_path")
    if written_path:
        typer.echo("")
        typer.echo(f"Wrote AWF profile: {written_path}")

    if include_smoke_request and preview.smoke_request is not None:
        typer.echo("")
        typer.echo("Smoke request payload (local-only, not submitted):")
        typer.echo(json.dumps(preview.smoke_request, indent=2, sort_keys=True, default=str))

    typer.echo("")
    typer.echo("Suggested next steps:")
    for step in _list_value(payload.get("next_steps")):
        typer.echo(f"  - {step}")
    typer.echo(
        "  - Optional: generate a smoke workspace request locally with "
        "`awf init <path> --include-smoke-request`; this prints the payload inline and does "
        "not submit a workspace."
    )

    if not bool(payload.get("local_checks_ready")):
        typer.echo(
            "\nLocal prerequisites are not fully ready yet; profile onboarding can "
            "continue, but fix the issues above before creating or retrying workspaces."
        )


def _existing_project_profile_path(repository: Path) -> Path | None:
    """Return the first supported project-local AWF profile path if one exists."""
    for relative_path in _PROJECT_PROFILE_MARKER_PATHS:
        candidate = repository / relative_path
        if candidate.is_file():
            return candidate
    return None


def _resolve_state_directory(
    env: Mapping[str, str],
    *,
    home_environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the AWF host state directory matching the Compose default."""
    raw = env.get("AWF_HOST_WORK_DIR") or ""
    if raw:
        return Path(raw).expanduser().resolve()
    home_env = os.environ if home_environ is None else home_environ
    home = home_env.get("HOME", "~")
    return (Path(home) / ".awf" / "service").expanduser().resolve()


def _resolve_service_compose_paths() -> tuple[Path, Path, Path]:
    """Return the compose, env, and env seed source files used by service commands.

    If the verified source checkout contains local Compose assets, return the
    public root Compose entrypoint and root `.env` as absolute paths. The legacy
    `docker/compose/.env` file is handled only by the migration helper.
    """
    from awf.service import bootstrap as bootstrap_mod
    from awf.service.config import LOCAL_SERVICE_COMPOSE_ENV_FILE, LOCAL_SERVICE_COMPOSE_FILE

    asset_root = bootstrap_mod.get_bootstrap_asset_root()
    if asset_root is not None:
        resolved_asset_root = asset_root.resolve()
        compose_file = resolved_asset_root / LOCAL_SERVICE_COMPOSE_FILE
        env_file = resolved_asset_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
        env_example = resolved_asset_root / ".env.example"
        if bootstrap_mod.is_packaged_bootstrap_asset_root(resolved_asset_root):
            return compose_file, Path(".env"), env_example
        return compose_file, env_file, env_example

    return LOCAL_SERVICE_COMPOSE_FILE, Path(".env"), Path(".env.example")


def _resolve_existing_service_env_file(env_file: Path) -> Path:
    """Return the existing env file service commands should read."""
    if env_file.exists():
        return env_file
    root_env = _compose_root_env_file(env_file)
    if root_env is not None and root_env.exists():
        return root_env
    return env_file


def _resolve_service_env_files(
    env_file: Path,
    *,
    trusted_compose_env_file: Path | None = None,
) -> tuple[Path, Path | None]:
    """Return the env read source and actual Compose env-file path."""
    active_env_file = _resolve_existing_service_env_file(env_file)
    return active_env_file, _service_compose_env_file(
        active_env_file,
        trusted_compose_env_file=trusted_compose_env_file,
    )


def _resolve_service_runtime_env_files(
    compose_file: Path,
    env_file: Path,
    *,
    paths_verified: bool = False,
) -> tuple[Path, Path | None]:
    """Return service env files using compose paths already verified upstream."""
    return _resolve_service_env_files(
        env_file,
        trusted_compose_env_file=(
            _trusted_service_compose_env_file_from_verified_paths(compose_file, env_file)
            if paths_verified
            else _trusted_service_compose_env_file(compose_file, env_file)
        ),
    )


# Public, intentionally-stable names for the Compose env-resolution helpers above.
# ``awf setup`` readiness must resolve the Compose env exactly as ``awf start``
# does (so its port/work-dir probes match the real startup environment), so it
# reuses these helpers across the module boundary. Exposing them without the
# leading underscore makes that an explicit public contract: a rename or
# signature change breaks at import/CI time rather than silently at the
# ``setup`` call site. Existing in-module and sibling callers keep the
# underscore-prefixed names as internal aliases.
resolve_service_compose_paths = _resolve_service_compose_paths
resolve_service_runtime_env_files = _resolve_service_runtime_env_files
resolve_existing_service_env_file = _resolve_existing_service_env_file


def _migrate_legacy_service_env_file(env_file: Path, env_example: Path) -> object | None:
    """Migrate legacy compose env values into the canonical root env file."""
    from awf.service.env_migration import (
        default_legacy_compose_env_file,
        migrate_legacy_compose_env_file,
    )

    canonical_env_file = env_file.expanduser()
    if not _legacy_service_env_migration_allowed(canonical_env_file):
        return None
    legacy_env_file = default_legacy_compose_env_file(canonical_env_file)
    if not legacy_env_file.exists():
        return None
    return migrate_legacy_compose_env_file(
        canonical_env_file=canonical_env_file,
        env_example_file=env_example,
        legacy_env_file=legacy_env_file,
    )


def _legacy_service_env_migration_allowed(canonical_env_file: Path) -> bool:
    """Return whether legacy Compose env migration is safe for this root."""

    if canonical_env_file.name != ".env":
        return False
    root = canonical_env_file.parent if canonical_env_file.is_absolute() else Path.cwd()
    return all((root / marker).is_file() for marker in _AWF_SOURCE_ROOT_ENV_MIGRATION_MARKERS)


def _add_env_migration_payload(
    payload: dict[str, object],
    env_migration: object | None,
) -> None:
    """Attach secret-free env migration metadata when a migration occurred."""
    if env_migration is None:
        return
    to_dict = getattr(env_migration, "to_dict", None)
    if callable(to_dict):
        payload["env_migration"] = to_dict()


def _trusted_service_compose_env_file_from_verified_paths(
    compose_file: Path,
    env_file: Path,
) -> Path | None:
    """Return the Compose env path from already verified local-service paths."""
    from awf.service.config import LOCAL_SERVICE_COMPOSE_FILE

    if env_file == Path(".env"):
        return env_file
    resolved_compose_file = compose_file.expanduser().resolve()
    resolved_env_file = env_file.expanduser().resolve()
    if resolved_compose_file.parent != resolved_env_file.parent:
        return None
    if resolved_compose_file.name != LOCAL_SERVICE_COMPOSE_FILE.name:
        return None
    return env_file


def _trusted_service_compose_env_file(compose_file: Path, env_file: Path) -> Path | None:
    """Return the Compose env path from `_resolve_service_compose_paths`, if present."""
    from awf.service.config import _is_local_service_compose_file_path

    if not _is_local_service_compose_file_path(compose_file):
        return None
    if env_file == Path(".env"):
        return env_file
    if compose_file.expanduser().resolve().parent != env_file.expanduser().resolve().parent:
        return None
    return env_file


def _service_compose_env_file(
    active_env_file: Path,
    *,
    trusted_compose_env_file: Path | None = None,
) -> Path | None:
    """Return the env file that should be passed to Docker Compose, if any."""
    if not active_env_file.exists():
        return None
    if trusted_compose_env_file is not None:
        if (
            active_env_file.expanduser().resolve()
            == trusted_compose_env_file.expanduser().resolve()
        ):
            return active_env_file
        return None
    if _is_local_service_compose_env_file(active_env_file):
        return active_env_file
    return None


def _is_local_service_compose_env_file(path: Path) -> bool:
    """Return true for the verified local-service Compose env file."""
    from awf.service.config import _is_local_service_compose_env_path

    return _is_local_service_compose_env_path(path)


def _init_env_error_payload(
    *,
    operation: str,
    path: Path,
    env_file: Path,
    env_example: Path,
    exc: Exception,
) -> dict[str, str]:
    """Return a machine-readable env seeding failure without env contents."""
    return {
        "operation": operation,
        "path": _init_display_path(path),
        "env_file": _init_display_path(env_file),
        "env_example": _init_display_path(env_example),
        "message": str(exc),
    }


def _compose_root_env_file(env_file: Path) -> Path | None:
    """Return the root `.env` paired with a verified local Compose env file.

    Relative env-file paths come from the no-asset-root fallback used outside an
    AWF source checkout. They are intentionally not treated as local Compose
    asset paths, which keeps root-env fallback and `--env-file` forwarding tied
    to absolute paths resolved from the verified bootstrap asset root.
    """
    env_file = env_file.expanduser()
    if not env_file.is_absolute():
        return None
    resolved_env_file = env_file.resolve()
    if (
        resolved_env_file.name == ".env"
        and resolved_env_file.parent.name == "compose"
        and resolved_env_file.parent.parent.name == "docker"
    ):
        return resolved_env_file.parent.parent.parent / ".env"
    return None


def _init_env_overlay_source(env_file: Path, env_example: Path) -> Path | None:
    """Return the root `.env` overlay used when seeding compose env files."""
    root_env = _compose_root_env_file(env_file)
    if root_env is None or env_example == root_env or not root_env.exists():
        return None
    compose_example = env_file.with_name(".env.example")
    root_example = root_env.with_name(".env.example")
    if env_example not in (compose_example, root_example):
        return None
    return root_env


def _env_assignment_key(line: str) -> str | None:
    """Return the key from an env assignment line, ignoring comments."""
    if line.lstrip().startswith("#"):
        return None
    match = _ENV_ASSIGNMENT_RE.match(line)
    if match is None:
        return None
    return match.group("key")


def _env_assignment_key_identity(key: str) -> str:
    """Return the canonical key identity used for seed/overlay matching."""
    return key.upper()


def _env_assignment_line_with_key(line: str, key: str) -> str:
    """Return a Compose env assignment line with the key spelling replaced."""
    match = _ENV_ASSIGNMENT_RE.match(line)
    if match is None:
        return line
    return f"{key}{line[match.end('key') :]}"


def _env_seed_has_meaningful_leading_context(lines: list[str]) -> bool:
    """Return whether the seed owns the merged file header."""
    for line in lines:
        if _env_assignment_key(line) is not None:
            return False
        if line.strip():
            return True
    return False


def _env_value_has_same_line_closing_quote(value: str, quote: str) -> bool:
    """Return whether a quoted dotenv value closes on its assignment line."""
    escaped = False
    for char in value[1:]:
        if quote == '"' and char == "\\" and not escaped:
            escaped = True
            continue
        if char == quote and not escaped:
            return True
        escaped = False
    return False


def _env_contents_have_multiline_values(text: str) -> bool:
    """Return true when dotenv assignments span physical lines."""
    for line in text.splitlines(keepends=True):
        key = _env_assignment_key(line)
        if key is None:
            continue
        _assignment, _separator, value = line.partition("=")
        stripped_value = value.lstrip()
        if not stripped_value:
            continue
        quote = stripped_value[0]
        line_without_newline = line.rstrip("\r\n")
        trailing_backslashes = len(line_without_newline) - len(line_without_newline.rstrip("\\"))
        if (
            quote not in {"'", '"'}
            and line.endswith(("\n", "\r"))
            and trailing_backslashes % 2 == 1
        ):
            return True
        if quote in {"'", '"'} and not _env_value_has_same_line_closing_quote(
            stripped_value,
            quote,
        ):
            return True
    return False


def _env_context_looks_like_file_header(lines: list[str]) -> bool:
    """Return whether leading non-assignment lines look like a file header."""
    comment_count = 0
    for line in lines:
        stripped = line.strip()
        if stripped == "":
            return True
        if stripped.startswith("#"):
            comment_count += 1
    return comment_count > 1


def _env_comment_looks_key_specific(line: str, key: str) -> bool:
    """Return whether a comment appears to document one dotenv assignment key."""
    stripped = line.strip()
    if not stripped.startswith("#"):
        return False
    comment = stripped.lstrip("#").strip().lower()
    if key.lower() in comment:
        return True
    key_words = [
        word
        for word in _ENV_COMMENT_WORD_RE.findall(key.lower())
        if word not in _ENV_COMMENT_KEY_IGNORE_WORDS
    ]
    if not key_words:
        return False
    comment_words = set(_ENV_COMMENT_WORD_RE.findall(comment))
    return all(word in comment_words for word in key_words)


def _env_context_has_key_specific_comment(lines: list[str], key: str) -> bool:
    """Return whether any context comment appears tied to the given key."""
    return any(_env_comment_looks_key_specific(line, key) for line in lines)


def _env_context_has_file_header_marker(lines: list[str]) -> bool:
    """Return whether comments explicitly describe dotenv-file-level context."""
    comments = [line.strip().lstrip("#").strip().lower() for line in lines]
    return any(
        ".env" in comment
        or "dotenv" in comment
        or "environment file" in comment
        or "settings here" in comment
        or "overrides here" in comment
        for comment in comments
    )


def _split_env_file_header_context(
    lines: list[str],
    key: str,
    *,
    seed_has_leading_context: bool,
) -> tuple[list[str], list[str]]:
    """Split leading overlay comments into file-header and assignment context."""
    if not _env_context_looks_like_file_header(lines):
        return [], lines
    last_blank_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == "":
            last_blank_index = index
    if last_blank_index is not None:
        return lines[: last_blank_index + 1], lines[last_blank_index + 1 :]

    split_at: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if not stripped.startswith("#"):
            break
        if _env_comment_looks_key_specific(lines[index], key):
            split_at = index
            break
    if split_at is None:
        if (
            seed_has_leading_context
            and len(lines) <= 2
            and not _env_context_has_file_header_marker(lines)
        ):
            return [], lines
        return lines, []
    return lines[:split_at], lines[split_at:]


def _env_context_looks_like_section_header(lines: list[str]) -> bool:
    """Return whether non-assignment lines look like reusable section documentation."""
    return sum(1 for line in lines if line.strip().startswith("#")) > 1


def _env_context_is_single_adjacent_comment(lines: list[str]) -> bool:
    """Return whether context is exactly one comment directly above an assignment."""
    return len(lines) == 1 and lines[0].strip().startswith("#")


def _env_context_has_non_comment_note(lines: list[str]) -> bool:
    """Return whether context contains an operator note outside dotenv comments."""
    return any(line.strip() and not line.strip().startswith("#") for line in lines)


def _merge_env_seed_contents_with_overlay_keys(
    seed_contents: bytes,
    overlay_contents: bytes,
) -> tuple[bytes, tuple[str, ...]]:
    """Return merged env contents plus root-only keys appended from the overlay."""
    try:
        seed_text = seed_contents.decode("utf-8")
        overlay_text = overlay_contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _EnvSeedMergeError("env seeding merge requires UTF-8 dotenv files") from exc

    # This merge is deliberately line-oriented to preserve comments and ordering.
    # Multi-line dotenv values are unsupported in seed and overlay files; keep
    # template entries single-line unless this is replaced with a dotenv parser.
    if _env_contents_have_multiline_values(seed_text) or _env_contents_have_multiline_values(
        overlay_text
    ):
        raise _EnvSeedMergeError(
            "unsupported multi-line dotenv values; env seeding merge only supports "
            "single-line assignments"
        )
    seed_lines = seed_text.splitlines(keepends=True)
    overlay_lines = overlay_text.splitlines(keepends=True)

    overlay_assignments: dict[str, str] = {}
    for line in overlay_lines:
        key = _env_assignment_key(line)
        if key is not None:
            normalized_line = line if line.endswith(("\n", "\r")) else f"{line}\n"
            overlay_assignments[_env_assignment_key_identity(key)] = _env_assignment_line_with_key(
                normalized_line, key
            )

    seed_keys: set[str] = set()
    for line in seed_lines:
        key = _env_assignment_key(line)
        if key is not None:
            seed_keys.add(_env_assignment_key_identity(key))
    seed_has_leading_context = _env_seed_has_meaningful_leading_context(seed_lines)

    overlay_last_assignment_index: dict[str, int] = {}
    for index, line in enumerate(overlay_lines):
        key = _env_assignment_key(line)
        if key is not None:
            overlay_last_assignment_index[_env_assignment_key_identity(key)] = index

    seed_leading_context: dict[str, list[str]] = {}
    seed_final_context: list[str] = []
    file_header_context: list[str] = []
    overlay_only_lines: list[str] = []
    overlay_only_keys: list[str] = []
    pending_context: list[str] = []
    duplicate_context: dict[str, list[str]] = {}
    seen_overlay_assignment_keys: set[str] = set()
    last_assignment_key: str | None = None
    for index, line in enumerate(overlay_lines):
        key = _env_assignment_key(line)
        if key is None:
            pending_context.append(line)
            continue
        key_identity = _env_assignment_key_identity(key)
        first_overlay_assignment = last_assignment_key is None
        if first_overlay_assignment and pending_context:
            file_header_context, pending_context = _split_env_file_header_context(
                pending_context,
                key,
                seed_has_leading_context=seed_has_leading_context,
            )
        context = [*duplicate_context.pop(key_identity, []), *pending_context]
        context_has_key_specific_comment = _env_context_has_key_specific_comment(context, key)
        if overlay_last_assignment_index.get(key_identity) == index:
            if key_identity in seed_keys:
                if (
                    first_overlay_assignment
                    and seed_has_leading_context
                    and not context_has_key_specific_comment
                    and (file_header_context or _env_context_is_single_adjacent_comment(context))
                ):
                    context = []
                seed_leading_context[key_identity] = context
            else:
                overlay_only_lines.extend(context)
                normalized_only_line = line if line.endswith(("\n", "\r")) else f"{line}\n"
                overlay_only_lines.append(_env_assignment_line_with_key(normalized_only_line, key))
                overlay_only_keys.append(key)
        else:
            if (
                key_identity in seed_keys
                or key_identity in seen_overlay_assignment_keys
                or _env_context_looks_like_section_header(context)
                or _env_context_is_single_adjacent_comment(context)
                or _env_context_has_non_comment_note(context)
            ):
                duplicate_context.setdefault(key_identity, []).extend(context)
        pending_context = []
        seen_overlay_assignment_keys.add(key_identity)
        last_assignment_key = key_identity
    if pending_context and last_assignment_key is not None and last_assignment_key in seed_keys:
        seed_final_context = pending_context
    elif pending_context:
        overlay_only_lines.extend(pending_context)

    # Overlay file headers are kept only when the seed starts with assignments.
    # If the seed has its own leading context, it owns the top of the merged file
    # and overlay header comments are omitted to avoid duplicate file headers.
    merged_lines: list[str] = [] if seed_has_leading_context else list(file_header_context)
    emitted_seed_leading_context: set[str] = set()
    for line in seed_lines:
        key = _env_assignment_key(line)
        if key is None:
            merged_lines.append(line)
            continue
        key_identity = _env_assignment_key_identity(key)
        if (
            key_identity in seed_leading_context
            and key_identity not in emitted_seed_leading_context
        ):
            merged_lines.extend(seed_leading_context[key_identity])
            emitted_seed_leading_context.add(key_identity)
        overlay_assignment = overlay_assignments.get(key_identity)
        if overlay_assignment is None:
            merged_lines.append(line)
        else:
            merged_lines.append(_env_assignment_line_with_key(overlay_assignment, key))

    tail_lines = [*overlay_only_lines, *seed_final_context]
    if tail_lines and merged_lines and not merged_lines[-1].endswith(("\n", "\r")):
        merged_lines[-1] = f"{merged_lines[-1]}\n"
    merged_lines.extend(tail_lines)

    return "".join(merged_lines).encode("utf-8"), tuple(overlay_only_keys)


def _merge_env_seed_contents(seed_contents: bytes, overlay_contents: bytes) -> bytes:
    """Return compose-template env contents with root env assignments overlaid."""
    merged_contents, _overlay_only_keys = _merge_env_seed_contents_with_overlay_keys(
        seed_contents,
        overlay_contents,
    )
    return merged_contents


def _seed_env_file(
    env_file: Path,
    env_example: Path,
    *,
    env_overlay: Path | None = None,
) -> tuple[str, dict[str, str] | None, tuple[str, ...]]:
    """Seed an env file and return action, failure payload, and copied overlay keys."""
    if env_file.exists():
        return "kept_existing", None, ()

    if not env_example.exists():
        return "no_example", None, ()

    try:
        env_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (
            "write_failed",
            _init_env_error_payload(
                operation="create_parent_directory",
                path=env_file.parent,
                env_file=env_file,
                env_example=env_example,
                exc=exc,
            ),
            (),
        )

    try:
        env_contents = env_example.read_bytes()
    except OSError as exc:
        return (
            "write_failed",
            _init_env_error_payload(
                operation="read_example",
                path=env_example,
                env_file=env_file,
                env_example=env_example,
                exc=exc,
            ),
            (),
        )

    env_overlay_keys: tuple[str, ...] = ()
    if env_overlay is not None and env_overlay.exists():
        try:
            overlay_contents = env_overlay.read_bytes()
        except OSError as exc:
            return (
                "write_failed",
                _init_env_error_payload(
                    operation="read_overlay",
                    path=env_overlay,
                    env_file=env_file,
                    env_example=env_example,
                    exc=exc,
                ),
                (),
            )
        try:
            env_contents, env_overlay_keys = _merge_env_seed_contents_with_overlay_keys(
                env_contents,
                overlay_contents,
            )
        except _EnvSeedMergeError as exc:
            return (
                "write_failed",
                _init_env_error_payload(
                    operation="merge_overlay",
                    path=env_overlay,
                    env_file=env_file,
                    env_example=env_example,
                    exc=exc,
                ),
                (),
            )

    env_file_created = False
    try:
        with env_file.open("xb") as handle:
            env_file_created = True
            handle.write(env_contents)
    except FileExistsError:
        return "kept_existing", None, ()
    except OSError as exc:
        if env_file_created:
            try:
                env_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return (
            "write_failed",
            _init_env_error_payload(
                operation="write_env",
                path=env_file,
                env_file=env_file,
                env_example=env_example,
                exc=exc,
            ),
            (),
        )

    return "wrote_from_example", None, env_overlay_keys


def _init_display_path(path: Path | str) -> str:
    """Return a stable human-readable init path from the launch directory."""
    candidate = Path(path)
    if not candidate.is_absolute():
        return str(candidate)
    try:
        return os.path.relpath(candidate, Path.cwd())
    except ValueError:
        return str(candidate)


def _init_env_warning(env_error: Mapping[str, str]) -> str:
    """Return the pretty warning for an env seeding failure payload."""
    operation = env_error["operation"]
    message = env_error["message"]
    env_file = env_error["env_file"]
    env_example = env_error["env_example"]
    if operation == "create_parent_directory":
        parent = env_error["path"]
        return f"  warning: could not create parent directory {parent} for {env_file}: {message}"
    if operation == "read_example":
        return f"  warning: could not read {env_example} while seeding {env_file}: {message}"
    if operation == "read_overlay":
        overlay = env_error["path"]
        return (
            f"  warning: could not read {overlay} while seeding {env_file} "
            f"from {env_example}: {message}"
        )
    if operation == "merge_overlay":
        overlay = env_error["path"]
        return (
            f"  warning: could not merge {overlay} while seeding {env_file} "
            f"from {env_example}: {message}"
        )
    return f"  warning: could not write {env_file} from {env_example}: {message}"


def _add_init_env_overlay_keys(
    payload: dict[str, object],
    env_overlay_keys: tuple[str, ...],
) -> None:
    """Add non-secret env overlay audit metadata to a JSON init payload."""
    if env_overlay_keys:
        payload["env_overlay_keys"] = list(env_overlay_keys)


def _init_env_example_search_paths(env_file: Path, env_example: Path) -> tuple[Path, ...]:
    """Return the env template paths that explain why init skipped seeding."""
    search_paths: list[Path] = []
    candidates = [env_file.with_name(".env.example"), env_example]
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        search_paths.append(candidate)
    return tuple(search_paths)


def _docker_diagnostic_from_report(report: object) -> object | None:
    """Return the docker diagnostic entry from a readiness report if present."""
    from typing import cast

    diagnostics = getattr(report, "diagnostics", ())
    for diagnostic in diagnostics:
        if getattr(diagnostic, "id", None) == "docker":
            return cast(object, diagnostic)
    return None


def _init_preflight_environ(
    environ: Mapping[str, str],
    *,
    provider_secret_keys: frozenset[str],
) -> dict[str, str]:
    """Return init preflight env without provider credentials."""
    secret_keys = {key.upper() for key in provider_secret_keys}
    return {key: value for key, value in environ.items() if key.upper() not in secret_keys}


def _run_init_service_bootstrap(
    *,
    write_env: bool,
    timeout_seconds: float,
    poll_interval_seconds: float,
    skip_agent_runtime_build: bool,
    providers: list[str],
    fmt: OutputFormat,
) -> None:
    """Run local-service bootstrap with environment seeding and status output."""
    from awf.common.config import Settings
    from awf.service.bootstrap import (
        ServiceBootstrapError,
        ServiceBootstrapOptions,
        run_service_bootstrap,
    )
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.doctor import collect_doctor_report
    from awf.service.provider_readiness import (
        KNOWN_SECRET_ENV_KEYS,
        ProviderReadinessError,
        validate_provider_names,
    )

    try:
        strict_providers = validate_provider_names(providers)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    pretty = fmt == OutputFormat.pretty
    compose_file, env_file, env_example = _resolve_service_compose_paths()
    env_migration = _migrate_legacy_service_env_file(env_file, env_example) if write_env else None
    env_action = "skipped"
    env_error: dict[str, str] | None = None
    env_overlay_keys: tuple[str, ...] = ()
    if write_env:
        env_action, env_error, env_overlay_keys = _seed_env_file(
            env_file,
            env_example,
            env_overlay=_init_env_overlay_source(env_file, env_example),
        )
    active_env_file, compose_env_file = _resolve_service_runtime_env_files(
        compose_file,
        env_file,
        paths_verified=True,
    )

    if pretty:
        typer.echo("AWF init: local service bootstrap")
        if write_env:
            if env_migration is not None:
                typer.echo(
                    f"  migrated legacy docker/compose/.env into {_init_display_path(env_file)}"
                )
            if env_action == "write_failed" and env_error is not None:
                typer.echo(_init_env_warning(env_error))
            elif env_action == "kept_existing":
                typer.echo(f"  kept existing {_init_display_path(env_file)}")
            elif env_action == "wrote_from_example":
                typer.echo(
                    f"  wrote {_init_display_path(env_file)} from {_init_display_path(env_example)}"
                )
                if env_overlay_keys:
                    typer.echo(
                        "  added root .env keys to "
                        f"{_init_display_path(env_file)}: {', '.join(env_overlay_keys)}"
                    )
            elif env_action == "no_example":
                search_paths = ", ".join(
                    _init_display_path(path)
                    for path in _init_env_example_search_paths(env_file, env_example)
                )
                typer.echo(
                    "  no env template found; skipped "
                    f"{_init_display_path(env_file)} "
                    f"creation (looked for {search_paths}; run `awf init` from "
                    "the AWF repository root if you expected one)"
                )

    try:
        service_env = local_service_environ(env_file=active_env_file)
        preflight_env = _init_preflight_environ(
            service_env,
            provider_secret_keys=KNOWN_SECRET_ENV_KEYS,
        )
        settings = resolve_service_settings(
            Settings(_env_file=active_env_file, github_token=None),
            environ=preflight_env,
        )

        docker_report = asyncio.run(
            collect_doctor_report(
                settings,
                strict_providers=frozenset(),
                provider_environ=preflight_env,
                environ=preflight_env,
                compose_file=compose_file,
                compose_env_file=compose_env_file,
            )
        )
        docker_diag = _docker_diagnostic_from_report(docker_report)
        docker_status = getattr(docker_diag, "status", None)
        docker_unknown = docker_diag is None or docker_status is None
        if docker_unknown or docker_status == "fail":
            if docker_unknown:
                message = "Docker availability could not be determined from the doctor report."
                action = "Run `awf service doctor` to investigate the local environment."
                reason = "DOCKER_DIAGNOSTIC_MISSING"
            else:
                message = getattr(docker_diag, "message", "Docker is not available.")
                action = getattr(docker_diag, "action", "")
                reason = getattr(docker_diag, "reason", "DOCKER_DAEMON_UNREACHABLE")
            if pretty:
                typer.echo(f"  docker: {message}")
                if action:
                    typer.echo(f"  action: {action}")
                typer.echo("")
                typer.echo("Docker is not available; cannot bootstrap local service.")
            else:
                docker_payload: dict[str, object] = {
                    "status": "failed",
                    "reason_code": reason,
                    "message": message,
                    "action": action,
                    "env_action": env_action,
                }
                if env_error is not None:
                    docker_payload["env_error"] = env_error
                _add_init_env_overlay_keys(docker_payload, env_overlay_keys)
                _add_env_migration_payload(docker_payload, env_migration)
                _emit(docker_payload, fmt)
            raise typer.Exit(code=1)

        state_dir = _resolve_state_directory(service_env)
        created = not state_dir.exists()
        state_dir.mkdir(parents=True, exist_ok=True)
    except typer.Exit:
        raise
    except Exception as exc:
        if pretty:
            typer.echo(f"error: could not collect local checks: {exc}", err=True)
        else:
            local_checks_payload: dict[str, object] = {
                "status": "failed",
                "reason_code": "BOOTSTRAP_LOCAL_CHECKS_FAILED",
                "message": str(exc),
                "env_action": env_action,
            }
            if env_error is not None:
                local_checks_payload["env_error"] = env_error
            _add_init_env_overlay_keys(local_checks_payload, env_overlay_keys)
            _add_env_migration_payload(local_checks_payload, env_migration)
            _emit(local_checks_payload, fmt)
        raise typer.Exit(code=1) from exc

    if pretty:
        typer.echo(f"  state directory: {state_dir}")
        typer.echo(f"  created: {'true' if created else 'false'}")

    options = ServiceBootstrapOptions(
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        skip_agent_runtime_build=skip_agent_runtime_build,
        strict_providers=frozenset(strict_providers),
    )
    try:
        result = asyncio.run(
            run_service_bootstrap(
                settings,
                options=options,
                compose_file=compose_file,
                env_file=compose_env_file,
                service_environ=service_env,
            ),
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except ServiceBootstrapError as exc:
        payload = exc.to_dict()
        if env_error is not None:
            payload["env_error"] = env_error
        _add_init_env_overlay_keys(payload, env_overlay_keys)
        _add_env_migration_payload(payload, env_migration)
        _emit(payload, fmt)
        raise typer.Exit(code=1) from None

    if pretty:
        typer.echo("  bootstrap status: ok")
        typer.echo("")
        typer.echo("Next steps:")
        typer.echo('  - export AWF_GITHUB_TOKEN="$(gh auth token)" so the worker can create PRs.')
        typer.echo("  - Run `awf service status --format pretty` to verify readiness.")
        typer.echo(
            "  - Optional console: `npm --prefix apps/console run dev`, then open http://localhost:3000."
        )
        typer.echo("  - Run `awf init <path>` to onboard a project repository.")
    else:
        payload = result.to_dict()
        payload["state_directory"] = str(state_dir)
        payload["state_directory_created"] = created
        payload["env_action"] = env_action
        if env_error is not None:
            payload["env_error"] = env_error
        _add_init_env_overlay_keys(payload, env_overlay_keys)
        _add_env_migration_payload(payload, env_migration)
        _emit(payload, fmt)
