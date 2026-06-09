"""Workspace profile resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from awf.common.forge import detect_forge_from_url
from awf.common.profile_paths import PROFILE_MARKER_PATHS
from awf.db.enums import ForgeKind
from awf.profiles.lint import lint_workspace_profile
from awf.profiles.models import (
    ProfileLintFinding,
    ProfileLintSeverity,
    ProfileResolution,
    WorkspaceProfile,
)
from awf.profiles.registry import detect_profile, generic_profile, get_builtin_profile

__all__ = (
    "ProfileResolution",
    "ProfileResolutionError",
    "ProfileResolver",
    "resolve_workspace_profile",
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
        """Create a profile resolution error with optional lint diagnostics."""
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
        repo_url: str | None = None,
    ) -> ProfileResolution:
        """Resolve a workspace profile using local, inline, and registry sources.

        ``repo_url`` drives forge detection when the profile's ``forge`` is
        ``"auto"``. Precedence is **explicit ``forge:`` > URL host > github**;
        the resolved concrete forge is stamped onto the profile so
        ``resolved_profile.forge`` is always ``github``/``bitbucket`` (never
        ``"auto"``) and later stages read it without re-resolving.
        """
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
                source = repo_profile.source or "repo:<unknown>"
                reason = f"repo-local {source.removeprefix('repo:')} profile"

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

        resolved_forge = _resolve_forge(profile.forge, repo_url)
        if profile.forge != resolved_forge:
            profile = profile.model_copy(update={"forge": resolved_forge})

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
        """Load the first valid workspace profile from known repository marker files."""
        for rel in PROFILE_MARKER_PATHS:
            path = worktree_path / rel
            considered.append(f"repo:{rel}")
            if not path.is_file():
                continue
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
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
    repo_url: str | None = None,
) -> ProfileResolution:
    """Resolve a workspace profile from local path, inline payload, or registry."""
    return ProfileResolver().resolve(
        worktree_path=worktree_path,
        inline_profile=inline_profile,
        profile_ref=profile_ref,
        validation_commands=validation_commands,
        repo_url=repo_url,
    )


def _resolve_forge(profile_forge: ForgeKind | Literal["auto"], repo_url: str | None) -> ForgeKind:
    """Resolve the concrete forge: explicit ``forge:`` > URL host > default github.

    ``profile_forge`` is the profile's declared value (``"auto"`` |
    ``"github"`` | ``"bitbucket"``). An explicit non-``auto`` value always wins.
    For ``"auto"``, URL-host detection decides; a missing/malformed URL falls
    through to the default ``"github"`` (detection is best-effort — the executor
    forge gate is the real fail-fast point).
    """
    if profile_forge != "auto":
        return profile_forge  # explicit forge wins over detection
    if repo_url:
        detected = detect_forge_from_url(repo_url)
        if detected is not None:
            return detected
    return "github"


def _validation_error_message(exc: ValidationError) -> str:
    """Extract a short path-aware validation error message from a Pydantic error."""
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
    """Assemble optional machine-readable reason-code details for failures."""
    if reason_code is None and not findings:
        return None
    return {
        "reason_code": reason_code,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }
