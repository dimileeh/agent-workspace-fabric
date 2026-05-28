"""AWF source-checkout asset validation for first-run host setup."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SOURCE_CHECKOUT_INVALID = "SOURCE_CHECKOUT_INVALID"
SOURCE_CHECKOUT_ASSETS_STALE = "SOURCE_CHECKOUT_ASSETS_STALE"

MarkerKind = Literal["file", "dir"]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class SourceCheckoutMarker:
    """Required marker path for a usable AWF source checkout."""

    path: str
    kind: MarkerKind


SOURCE_CHECKOUT_MARKERS: tuple[SourceCheckoutMarker, ...] = (
    SourceCheckoutMarker("pyproject.toml", "file"),
    SourceCheckoutMarker("src/awf/__init__.py", "file"),
    SourceCheckoutMarker("docker/compose/local-service.yml", "file"),
    SourceCheckoutMarker("docker/agent-runtime.Dockerfile", "file"),
    SourceCheckoutMarker("docker/control-plane.Dockerfile", "file"),
    SourceCheckoutMarker("README.md", "file"),
    SourceCheckoutMarker("docs", "dir"),
    SourceCheckoutMarker("RELEASING.md", "file"),
)
SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS: tuple[str, ...] = tuple(
    marker.path for marker in SOURCE_CHECKOUT_MARKERS
)


class SourceCheckoutAssetPaths(BaseModel):
    """Root-relative source asset paths persisted in host setup config."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compose_file: str = "docker/compose/local-service.yml"
    agent_runtime_dockerfile: str = "docker/agent-runtime.Dockerfile"
    control_plane_dockerfile: str = "docker/control-plane.Dockerfile"
    docs_dir: str = "docs"
    releasing_path: str = "RELEASING.md"
    pyproject_path: str = "pyproject.toml"
    awf_package_dir: str = "src/awf"


class SourceCheckoutAssetMetadata(BaseModel):
    """Persisted metadata for a previously verified AWF source checkout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    verified_at: datetime
    markers: tuple[str, ...] = Field(default_factory=lambda: SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS)
    asset_paths: SourceCheckoutAssetPaths = Field(default_factory=SourceCheckoutAssetPaths)


class VerifiedSourceCheckout(BaseModel):
    """Runtime handoff for setup/start code that needs source checkout assets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    compose_file: Path
    agent_runtime_dockerfile: Path
    control_plane_dockerfile: Path
    docs_dir: Path
    releasing_path: Path
    pyproject_path: Path
    awf_package_dir: Path
    verified_at: datetime
    markers: tuple[str, ...]
    asset_paths: SourceCheckoutAssetPaths = Field(default_factory=SourceCheckoutAssetPaths)

    def to_metadata(self) -> SourceCheckoutAssetMetadata:
        """Return the non-secret config metadata for this verified checkout."""

        return SourceCheckoutAssetMetadata(
            root=self.root,
            verified_at=self.verified_at,
            markers=self.markers,
            asset_paths=self.asset_paths,
        )


class SourceCheckoutError(RuntimeError):
    """Reason-coded source-checkout validation failure."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        root: Path,
        missing_markers: tuple[str, ...] = (),
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.root = root
        self.missing_markers = missing_markers
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable diagnostic payload."""

        payload: dict[str, object] = {
            "status": "failed",
            "reason_code": self.reason_code,
            "message": self.message,
            "root": str(self.root),
        }
        if self.missing_markers:
            payload["missing_markers"] = list(self.missing_markers)
        if self.details:
            payload["details"] = self.details
        return payload


def validate_source_checkout(
    root: str | Path,
    *,
    clock: Clock | None = None,
) -> VerifiedSourceCheckout:
    """Validate an AWF source checkout and return resolved setup asset paths."""

    resolved_root = _resolve_candidate(root)
    _validate_root_readable(resolved_root)

    missing_markers: list[str] = []
    unreadable_paths: list[str] = []
    for marker in SOURCE_CHECKOUT_MARKERS:
        marker_path = resolved_root / marker.path
        try:
            marker_exists = marker_path.is_dir() if marker.kind == "dir" else marker_path.is_file()
        except OSError:
            unreadable_paths.append(str(marker_path))
            continue
        if not marker_exists:
            missing_markers.append(marker.path)
            continue
        if not _path_readable(marker_path):
            unreadable_paths.append(str(marker_path))

    if missing_markers or unreadable_paths:
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_INVALID,
            message="AWF source checkout is missing required assets.",
            root=resolved_root,
            missing_markers=tuple(missing_markers),
            details=_source_checkout_details(
                missing_markers=tuple(missing_markers),
                unreadable_paths=tuple(unreadable_paths),
            ),
        )

    asset_paths = SourceCheckoutAssetPaths()
    verified_at = (clock or _now_utc)()
    return VerifiedSourceCheckout(
        root=resolved_root,
        compose_file=resolved_root / asset_paths.compose_file,
        agent_runtime_dockerfile=resolved_root / asset_paths.agent_runtime_dockerfile,
        control_plane_dockerfile=resolved_root / asset_paths.control_plane_dockerfile,
        docs_dir=resolved_root / asset_paths.docs_dir,
        releasing_path=resolved_root / asset_paths.releasing_path,
        pyproject_path=resolved_root / asset_paths.pyproject_path,
        awf_package_dir=resolved_root / asset_paths.awf_package_dir,
        verified_at=verified_at,
        markers=SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS,
        asset_paths=asset_paths,
    )


def verified_source_from_metadata(
    metadata: SourceCheckoutAssetMetadata,
    *,
    clock: Clock | None = None,
) -> VerifiedSourceCheckout:
    """Revalidate persisted source metadata before returning a runtime handoff."""

    try:
        verified = validate_source_checkout(metadata.root, clock=clock)
    except SourceCheckoutError as exc:
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_ASSETS_STALE,
            message="Stored AWF source checkout metadata is no longer valid.",
            root=exc.root,
            missing_markers=exc.missing_markers,
            details={
                **exc.details,
                "metadata_root": str(metadata.root),
                "fallback_used": False,
            },
        ) from exc

    expected_asset_paths = SourceCheckoutAssetPaths()
    stale_details: dict[str, object] = {"fallback_used": False}
    if metadata.markers != SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS:
        stale_details["expected_markers"] = list(SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS)
        stale_details["recorded_markers"] = list(metadata.markers)
    if metadata.asset_paths != expected_asset_paths:
        stale_details["expected_asset_paths"] = expected_asset_paths.model_dump(mode="json")
        stale_details["recorded_asset_paths"] = metadata.asset_paths.model_dump(mode="json")

    if len(stale_details) > 1:
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_ASSETS_STALE,
            message="Stored AWF source checkout metadata does not match current asset contract.",
            root=verified.root,
            details=stale_details,
        )

    return verified


def _resolve_candidate(root: str | Path) -> Path:
    base = Path(root)
    try:
        expanded = base.expanduser()
        return expanded.resolve()
    except (OSError, RuntimeError):
        return base.absolute()


def _validate_root_readable(root: Path) -> None:
    try:
        is_dir = root.is_dir()
    except OSError as exc:
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_INVALID,
            message="AWF source checkout path is not readable.",
            root=root,
            details={"path_status": "unreadable", "error_type": type(exc).__name__},
        ) from exc

    if not is_dir:
        status = "missing"
        try:
            if root.exists():
                status = "not_directory"
        except OSError:
            status = "unreadable"
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_INVALID,
            message="AWF source checkout path is not a readable directory.",
            root=root,
            details={"path_status": status},
        )

    if not _path_readable(root):
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_INVALID,
            message="AWF source checkout path is not readable.",
            root=root,
            details={"unreadable_paths": [str(root)]},
        )


def _source_checkout_details(
    *,
    missing_markers: tuple[str, ...],
    unreadable_paths: tuple[str, ...],
) -> dict[str, object]:
    details: dict[str, object] = {}
    if missing_markers:
        details["missing_markers"] = list(missing_markers)
    if unreadable_paths:
        details["unreadable_paths"] = list(unreadable_paths)
    return details


def _path_readable(path: Path) -> bool:
    try:
        exists = path.exists()
        if not exists:
            return False
        if os.name == "posix":
            mode = os.R_OK
            if path.is_dir():
                mode |= os.X_OK
            return os.access(path, mode)
        return True
    except OSError:
        return False


def _now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "SOURCE_CHECKOUT_ASSETS_STALE",
    "SOURCE_CHECKOUT_INVALID",
    "SOURCE_CHECKOUT_MARKERS",
    "SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS",
    "SourceCheckoutAssetMetadata",
    "SourceCheckoutAssetPaths",
    "SourceCheckoutError",
    "SourceCheckoutMarker",
    "VerifiedSourceCheckout",
    "validate_source_checkout",
    "verified_source_from_metadata",
]
