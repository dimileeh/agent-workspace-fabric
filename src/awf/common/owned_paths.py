"""Shared owned-path classification helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import Final

INTERNAL_PLAN_ARTIFACT_DIR: Final = "docs/awf-plans"
_PLANNING_PATH_FIELDS: Final = ("plan_path", "conformance_report_path")
_WORKSPACE_ID_PLACEHOLDER: Final = "{workspace_id}"
_WORKSPACE_ID_GLOB: Final = "ws_*"
INTERNAL_PLAN_ARTIFACT_NAME_RE: Final = re.compile(
    r"^ws_[A-Za-z0-9_*?-]+(?:\.md|(?:\.conformance)?\.json)$"
)


def normalize_owned_path(path: str) -> str:
    """Normalize owned-path strings without interpreting glob syntax."""
    segments: list[str] = []
    for segment in path.strip().replace("\\", "/").split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/".join(segments)


def internal_plan_artifact_owned_paths_from_profile(
    resolved_profile: Mapping[str, object] | None,
    *,
    workspace_id: str | None = None,
) -> tuple[str, ...]:
    """Return internal plan artifact paths/scopes declared by a resolved profile."""
    if not isinstance(resolved_profile, Mapping):
        return ()
    planning = resolved_profile.get("planning")
    if not isinstance(planning, Mapping):
        return ()

    paths: list[str] = []
    for field_name in _PLANNING_PATH_FIELDS:
        value = planning.get(field_name)
        if isinstance(value, str):
            paths.extend(
                _internal_plan_artifact_paths_from_template(
                    value,
                    workspace_id=workspace_id,
                )
            )
    return tuple(dict.fromkeys(path for path in paths if path))


def _internal_plan_artifact_paths_from_template(
    template: str,
    *,
    workspace_id: str | None,
) -> tuple[str, ...]:
    """Render configured planning artifact templates into matchable owned paths."""
    normalized = normalize_owned_path(template)
    if _WORKSPACE_ID_PLACEHOLDER not in normalized:
        return ()

    paths = [normalized.replace(_WORKSPACE_ID_PLACEHOLDER, _WORKSPACE_ID_GLOB)]
    if workspace_id:
        paths.append(normalized.replace(_WORKSPACE_ID_PLACEHOLDER, workspace_id))

    parent, separator, _filename = normalized.rpartition("/")
    if separator and parent and _WORKSPACE_ID_PLACEHOLDER in parent:
        wildcard_parent = parent.replace(_WORKSPACE_ID_PLACEHOLDER, _WORKSPACE_ID_GLOB)
        paths.append(f"{wildcard_parent}/**")
        if workspace_id:
            rendered_parent = parent.replace(_WORKSPACE_ID_PLACEHOLDER, workspace_id)
            paths.append(f"{rendered_parent}/**")

    return tuple(paths)


def _normalized_internal_plan_artifact_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Normalize configured internal artifact paths while preserving order."""
    return tuple(dict.fromkeys(normalize_owned_path(path) for path in paths))


def _is_internal_plan_artifact_normalized(
    normalized: str,
    *,
    internal_plan_artifact_paths: Iterable[str] = (),
) -> bool:
    """Return true for normalized AWF-generated planning/conformance artifact paths."""
    if _matches_configured_internal_plan_artifact_path(
        normalized,
        internal_plan_artifact_paths,
    ):
        return True
    if normalized == f"{INTERNAL_PLAN_ARTIFACT_DIR}/**":
        return True
    prefix = f"{INTERNAL_PLAN_ARTIFACT_DIR}/"
    if not normalized.startswith(prefix):
        return False
    filename = normalized.removeprefix(prefix)
    return "/" not in filename and INTERNAL_PLAN_ARTIFACT_NAME_RE.fullmatch(filename) is not None


def _matches_configured_internal_plan_artifact_path(
    normalized: str,
    internal_plan_artifact_paths: Iterable[str],
) -> bool:
    """Return true when a normalized path matches configured artifact paths."""
    for artifact_path in internal_plan_artifact_paths:
        if not artifact_path:
            continue
        if normalized == artifact_path:
            return True
        # "/**"-suffix entries are matched only via exact equality above.
        # Sub-path matching relies on the companion workspace-id filename
        # patterns that _internal_plan_artifact_paths_from_template always
        # generates alongside every "/**" entry. Do not add standalone "/**"
        # entries without them.
        if artifact_path.endswith("/**"):
            continue
        if _WORKSPACE_ID_GLOB in artifact_path:
            if _workspace_id_glob_path_matches(normalized, artifact_path):
                return True
            continue
        if _has_wildcard(artifact_path) and fnmatchcase(normalized, artifact_path):
            return True
    return False


def _workspace_id_glob_path_matches(normalized: str, artifact_path: str) -> bool:
    """Match a configured artifact path with a constrained workspace-id glob."""
    # Keep the constrained regex in sync with _WORKSPACE_ID_GLOB's literal prefix.
    glob_prefix = _WORKSPACE_ID_GLOB.rstrip("*")
    workspace_id_pattern = rf"{re.escape(glob_prefix)}[A-Za-z0-9_*?-]+"
    pattern = re.escape(artifact_path).replace(
        re.escape(_WORKSPACE_ID_GLOB),
        workspace_id_pattern,
    )
    return re.fullmatch(pattern, normalized) is not None


def _has_wildcard(path: str) -> bool:
    """Return true when a path pattern contains fnmatch wildcards."""
    return "*" in path or "?" in path or "[" in path


def is_internal_plan_artifact_owned_path(
    path: str,
    *,
    internal_plan_artifact_paths: Iterable[str] = (),
) -> bool:
    """Return true for AWF-generated planning/conformance artifact paths."""
    return _is_internal_plan_artifact_normalized(
        normalize_owned_path(path),
        internal_plan_artifact_paths=_normalized_internal_plan_artifact_paths(
            internal_plan_artifact_paths
        ),
    )


def interworkspace_owned_paths(
    paths: Iterable[str],
    *,
    internal_plan_artifact_paths: Iterable[str] = (),
) -> tuple[str, ...]:
    """Owned paths that should participate in inter-workspace dependency checks."""
    filtered_paths: list[str] = []
    normalized_internal_paths = _normalized_internal_plan_artifact_paths(
        internal_plan_artifact_paths
    )
    for path in paths:
        normalized = normalize_owned_path(path)
        if normalized == "" or _is_internal_plan_artifact_normalized(
            normalized,
            internal_plan_artifact_paths=normalized_internal_paths,
        ):
            continue
        filtered_paths.append(path)
    return tuple(filtered_paths)
