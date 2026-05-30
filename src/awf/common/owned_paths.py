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
# The reserved default AWF plan directory treats direct ``ws_*`` markdown/JSON
# files as generated artifacts. Configured custom paths below still use the
# stricter workspace-id glob matcher.
_WORKSPACE_ID_SUFFIX_PATTERN: Final = r"[0-9a-f]{24}"
_WORKSPACE_ID_SHORTHAND_SUFFIX_PATTERN: Final = r"[0-9]+"
INTERNAL_PLAN_ARTIFACT_NAME_RE: Final = re.compile(
    r"^ws_(?:[A-Za-z0-9][A-Za-z0-9_]*|\*)"
    r"(?:\.md|(?:\.conformance)?\.json)$"
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
    if planning.get("required") is not True:
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

    replacement = workspace_id or _WORKSPACE_ID_GLOB
    paths = [normalized.replace(_WORKSPACE_ID_PLACEHOLDER, replacement)]

    parent, separator, _filename = normalized.rpartition("/")
    if separator and parent and _WORKSPACE_ID_PLACEHOLDER in parent:
        rendered_parent = parent.replace(_WORKSPACE_ID_PLACEHOLDER, replacement)
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
    """Return true when a normalized path matches normalized artifact paths.

    Public helpers normalize both the path under test and configured artifact
    paths before calling this private matcher.
    """
    for artifact_path in internal_plan_artifact_paths:
        if not artifact_path:
            continue
        if normalized == artifact_path:
            return True
        if _WORKSPACE_ID_GLOB in normalized and _workspace_id_glob_path_matches(
            # The path being classified is the pattern here. Match the concrete
            # configured artifact path against it so persisted `ws_*` owned
            # paths can still be recognized as generated artifacts.
            artifact_path,
            normalized,
        ):
            return True
        if _WORKSPACE_ID_GLOB in artifact_path:
            if _workspace_id_glob_path_matches(normalized, artifact_path):
                return True
            continue
        # "/**"-suffix entries without a workspace-id glob are exact-only.
        # Configured leaf patterns classify generated files; scope entries only
        # classify the persisted owned-path scope declaration itself.
        if artifact_path.endswith("/**"):
            continue
        if has_wildcard(artifact_path) and fnmatchcase(normalized, artifact_path):
            return True
    return False


def _workspace_id_glob_path_matches(path: str, workspace_id_glob_path: str) -> bool:
    """Match a path against a constrained workspace-id glob path."""
    pattern = _workspace_id_glob_path_pattern(workspace_id_glob_path)
    return pattern is not None and re.fullmatch(pattern, path) is not None


def _workspace_id_glob_path_pattern(workspace_id_glob_path: str) -> str | None:
    """Build a regex that requires repeated workspace-id globs to share one id."""
    escaped_glob = re.escape(_WORKSPACE_ID_GLOB)
    escaped_path = re.escape(workspace_id_glob_path)
    path_parts = escaped_path.split(escaped_glob)
    if len(path_parts) == 1:
        return None

    glob_prefix = _WORKSPACE_ID_GLOB.rstrip("*")
    # Custom-profile artifact globs are generated from `{workspace_id}` and
    # intentionally match only concrete AWF workspace IDs. The default
    # `docs/awf-plans/ws_*` filename classifier is broader so legacy/manual AWF
    # artifact names in the reserved directory remain nonblocking.
    workspace_id_suffix_pattern = (
        rf"(?:{_WORKSPACE_ID_SUFFIX_PATTERN}|{_WORKSPACE_ID_SHORTHAND_SUFFIX_PATTERN})"
    )
    first_workspace_id_pattern = (
        rf"{re.escape(glob_prefix)}(?P<workspace_id>{workspace_id_suffix_pattern})"
    )
    repeated_workspace_id_pattern = rf"{re.escape(glob_prefix)}(?P=workspace_id)"

    pattern = path_parts[0]
    for index, path_part in enumerate(path_parts[1:]):
        pattern += first_workspace_id_pattern if index == 0 else repeated_workspace_id_pattern
        pattern += path_part
    return pattern


def has_wildcard(path: str) -> bool:
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
    """Owned paths that should participate in inter-workspace dependency checks.

    Filtering and deduplication compare normalized paths. Returned entries keep
    the first caller-provided string for each normalized path, so callers that do
    their own comparisons should normalize those preserved strings before
    matching.
    """
    filtered_paths: list[str] = []
    seen_normalized: set[str] = set()
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
        if normalized in seen_normalized:
            continue
        seen_normalized.add(normalized)
        filtered_paths.append(path)
    return tuple(filtered_paths)
