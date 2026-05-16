"""Workspace profile resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from awf.profiles.lint import lint_workspace_profile
from awf.profiles.models import (
    ProfileLintFinding,
    ProfileLintSeverity,
    ProfileResolution,
    WorkspaceProfile,
)
from awf.profiles.registry import detect_profile, generic_profile, get_builtin_profile

_PROFILE_PATHS = (
    ".awf/workspace.yml",
    ".awf/workspace.yaml",
    "awf.workspace.yml",
    "awf.workspace.yaml",
)


class ProfileResolutionError(Exception):
    """Raised when a requested profile cannot be parsed or found."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str | None = None,
        findings: tuple[ProfileLintFinding, ...] = (),
    ) -> None:
        self.reason_code = reason_code
        self.findings = findings
        self.detail = _resolution_error_detail(
            reason_code=reason_code,
            findings=findings,
        )
        super().__init__(message)


class ProfileResolver:
    """Resolve the immutable profile snapshot for a workspace."""

    def resolve(
        self,
        *,
        worktree_path: Path | None,
        inline_profile: dict[str, Any] | WorkspaceProfile | None = None,
        profile_ref: str | None = None,
        validation_commands: list[str] | None = None,
    ) -> ProfileResolution:
        considered: list[str] = []
        profile: WorkspaceProfile | None = None
        reason = ""

        if inline_profile is not None:
            considered.append("inline")
            try:
                profile = (
                    inline_profile
                    if isinstance(inline_profile, WorkspaceProfile)
                    else WorkspaceProfile.model_validate(inline_profile)
                )
            except ValidationError as exc:
                raise ProfileResolutionError(
                    f"invalid inline workspace profile: {_validation_error_message(exc)}"
                ) from exc
            reason = "inline profile supplied by request"
        elif worktree_path is not None:
            repo_profile = self._load_repo_profile(worktree_path, considered)
            if repo_profile is not None:
                profile = repo_profile
                reason = "repo-local .awf/workspace.yml profile"

        if profile is None and profile_ref and profile_ref != "auto":
            considered.append(f"registry:{profile_ref}")
            profile = get_builtin_profile(profile_ref, worktree_path=worktree_path)
            if profile is None:
                raise ProfileResolutionError(f"unknown workspace profile_ref: {profile_ref}")
            reason = f"central registry profile {profile_ref}"

        if profile is None and worktree_path is not None:
            considered.append("detector:auto")
            profile = detect_profile(worktree_path)
            if profile is not None:
                reason = f"auto-detected {profile.name} profile"

        if profile is None:
            considered.append("fallback:generic")
            profile = generic_profile(source="fallback:generic")
            reason = "fallback generic profile"

        if validation_commands:
            profile = profile.with_validation_commands(validation_commands)
            reason = f"{reason}; request validation commands appended"

        lint_findings = lint_workspace_profile(profile)
        lint_errors = tuple(
            finding for finding in lint_findings if finding.severity is ProfileLintSeverity.error
        )
        if lint_errors:
            first = lint_errors[0]
            raise ProfileResolutionError(
                f"invalid workspace profile: {first.reason_code}: {first.message}",
                reason_code=first.reason_code,
                findings=lint_errors,
            )

        return ProfileResolution(
            profile=profile,
            network_posture=profile.security.egress.mode.value,
            reason=reason,
            candidates_considered=considered,
            lint_findings=list(lint_findings),
        )

    def _load_repo_profile(
        self, worktree_path: Path, considered: list[str]
    ) -> WorkspaceProfile | None:
        for rel in _PROFILE_PATHS:
            path = worktree_path / rel
            considered.append(f"repo:{rel}")
            if not path.is_file():
                continue
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise ProfileResolutionError(
                    f"could not read workspace profile {path}: {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise ProfileResolutionError(f"workspace profile {path} must be a mapping")
            profile = raw.get("awf", raw)
            if not isinstance(profile, dict):
                raise ProfileResolutionError(
                    f"workspace profile {path} awf section must be a mapping"
                )
            try:
                parsed = WorkspaceProfile.model_validate(profile)
            except ValidationError as exc:
                raise ProfileResolutionError(
                    f"invalid workspace profile {path}: {_validation_error_message(exc)}"
                ) from exc
            return parsed.model_copy(update={"source": f"repo:{rel}"})
        return None


def resolve_workspace_profile(
    *,
    worktree_path: Path | None,
    inline_profile: dict[str, Any] | WorkspaceProfile | None = None,
    profile_ref: str | None = None,
    validation_commands: list[str] | None = None,
) -> ProfileResolution:
    return ProfileResolver().resolve(
        worktree_path=worktree_path,
        inline_profile=inline_profile,
        profile_ref=profile_ref,
        validation_commands=validation_commands,
    )


def _validation_error_message(exc: ValidationError) -> str:
    errors = exc.errors(include_input=False)
    if not errors:
        return "schema validation failed"
    first = errors[0]
    loc = first.get("loc", ())
    path = ".".join(str(part) for part in loc) if isinstance(loc, tuple) else str(loc)
    message = first.get("msg", "schema validation failed")
    return f"{path or '<profile>'}: {message}"


def _resolution_error_detail(
    *,
    reason_code: str | None,
    findings: tuple[ProfileLintFinding, ...],
) -> dict[str, Any] | None:
    if reason_code is None and not findings:
        return None
    return {
        "reason_code": reason_code,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }
