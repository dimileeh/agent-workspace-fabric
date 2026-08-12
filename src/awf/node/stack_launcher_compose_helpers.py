"""Compose launch helper functions split from the stack launcher."""

from __future__ import annotations

import re
from dataclasses import replace

from awf.node.companion_services import MaterializedCompanionService, WorkspaceCompanionSpec
from awf.node.compose_manager import ComposeOperationError, WorkspaceComposeSpec
from awf.profiles.models import WorkspaceProfile


class WorkspaceServiceExecutionError(Exception):
    """Raised when profile-declared workspace services fail to start."""

    pass


def _prebuilt_companion_image_count(spec: WorkspaceComposeSpec) -> int:
    """Return the number of companions still pinned to pre-built image tags."""
    return sum(1 for companion in spec.companions if companion.image is not None)


def _raise_workspace_service_error_if_docker_unavailable(
    exc: ComposeOperationError,
    *,
    spec: WorkspaceComposeSpec,
) -> None:
    """Map Docker availability failures to the workspace service error shape."""
    if exc.reason_code != "DOCKER_UNAVAILABLE":
        return
    required_services = [s.name for s in spec.services if s.required]
    if spec.docker_mode == "dind":
        required_services.append("docker")
    msg = "DOCKER_UNAVAILABLE: Cannot start workspace agent container"
    if required_services:
        msg = f"{msg} and required services: {required_services}"
    detail = exc.stderr.strip() or exc.stdout.strip()
    if detail:
        msg = f"{msg}: {detail}"
    raise WorkspaceServiceExecutionError(msg) from exc


def _missing_prebuilt_companion_image_retry_spec(
    spec: WorkspaceComposeSpec,
    exc: ComposeOperationError,
) -> WorkspaceComposeSpec | None:
    """Clear missing pre-built companion images after a compose-up race."""
    missing_images = frozenset(
        companion.image
        for companion in spec.companions
        if companion.image is not None and _compose_up_reports_missing_image(exc, companion.image)
    )
    if not missing_images:
        return None
    companions = tuple(
        replace(companion, image=None) if companion.image in missing_images else companion
        for companion in spec.companions
    )
    return replace(spec, companions=companions)


def _compose_up_reports_missing_image(exc: ComposeOperationError, image: str) -> bool:
    """Return whether Compose reported that a specific local image tag is absent."""
    detail = f"{exc.stderr}\n{exc.stdout}"
    image_ref = _compose_image_ref_regex(image)
    image_ref_before_colon = _compose_image_ref_before_colon_regex(image)
    patterns = (
        rf"no such image:\s*{image_ref}",
        rf"{image_ref_before_colon}\s*:\s*no such image",
        rf"pull access denied for\s+{image_ref}",
        rf"(?:repository\s+)?{image_ref}\s+does not exist",
    )
    return any(re.search(pattern, detail, flags=re.IGNORECASE) for pattern in patterns)


def _compose_image_ref_regex(image: str) -> str:
    """Return a regex fragment matching an exact Compose image reference."""
    image_ref_chars = r"A-Za-z0-9_.:/-"
    escaped_image = re.escape(image)
    return rf"(?<![{image_ref_chars}])['\"]?{escaped_image}['\"]?(?![{image_ref_chars}])"


def _compose_image_ref_before_colon_regex(image: str) -> str:
    """Return an exact image reference fragment followed by a colon separator."""
    image_ref_chars = r"A-Za-z0-9_.:/-"
    escaped_image = re.escape(image)
    return rf"(?<![{image_ref_chars}])['\"]?{escaped_image}['\"]?(?=\s*:)"


def effective_compose_up_timeout_seconds(
    *,
    profile: WorkspaceProfile,
    companions: tuple[MaterializedCompanionService | WorkspaceCompanionSpec, ...],
) -> int:
    """Return the longest compose-up wait timeout requested for this stack."""
    timeouts = [profile.docker.startup_timeout_seconds]
    timeouts.extend(
        timeout
        for companion in companions
        if (timeout := _companion_compose_up_timeout_seconds(companion)) is not None
    )
    return max(timeouts)


def _companion_compose_up_timeout_seconds(
    companion: MaterializedCompanionService | WorkspaceCompanionSpec,
) -> int | None:
    """Return a companion timeout from either materialized or parsed specs."""
    if isinstance(companion, MaterializedCompanionService):
        return companion.spec.compose_up_timeout_seconds
    return companion.compose_up_timeout_seconds
