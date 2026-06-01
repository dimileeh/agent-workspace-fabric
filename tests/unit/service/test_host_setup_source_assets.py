"""AWF source-checkout asset validation tests for first-run host setup."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import awf.host_setup.source_assets as source_assets
from awf.host_setup import (
    HostSetupConfig,
    SourceCheckoutError,
    validate_source_checkout,
    verified_source_from_metadata,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_MARKERS,
    SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS,
)

_FIXED_NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


def _write_valid_source_checkout(root: Path) -> Path:
    """Create a checkout tree containing every required source marker."""
    for marker in SOURCE_CHECKOUT_MARKERS:
        path = root / marker.path
        if marker.kind == "dir":
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{marker.path}\n", encoding="utf-8")
    return root


@pytest.mark.unit
def test_valid_source_checkout_returns_verified_asset_paths(tmp_path: Path) -> None:
    """Verify a valid source checkout returns all resolved asset paths."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")

    verified = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    assert verified.root == checkout.resolve()
    assert verified.compose_file == checkout.resolve() / "docker/compose/local-service.yml"
    assert verified.agent_runtime_dockerfile == (
        checkout.resolve() / "docker/agent-runtime.Dockerfile"
    )
    assert verified.control_plane_dockerfile == (
        checkout.resolve() / "docker/control-plane.Dockerfile"
    )
    assert verified.workspace_base_template == (
        checkout.resolve() / "docker/compose/workspace.base.yml.j2"
    )
    assert verified.docs_dir == checkout.resolve() / "docs"
    assert verified.releasing_path == checkout.resolve() / "RELEASING.md"
    assert verified.markers == tuple(SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS)

    metadata = verified.to_metadata()
    assert HostSetupConfig(source_checkout=metadata).source_checkout == metadata
    assert (
        verified_source_from_metadata(metadata, clock=lambda: _FIXED_NOW).root == checkout.resolve()
    )


@pytest.mark.unit
def test_invalid_source_checkout_reports_missing_marker_details(tmp_path: Path) -> None:
    """Verify missing checkout markers are reported in structured details."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    (checkout / "RELEASING.md").unlink()

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == checkout.resolve()
    assert error.missing_markers == ("RELEASING.md",)
    assert error.to_dict()["missing_markers"] == ["RELEASING.md"]
    assert "details" not in error.to_dict()


@pytest.mark.unit
def test_source_checkout_requires_workspace_compose_template(tmp_path: Path) -> None:
    """Verify the workspace compose template is a required checkout asset."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    template = checkout / "docker/compose/workspace.base.yml.j2"
    if template.exists():
        template.unlink()

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.missing_markers == ("docker/compose/workspace.base.yml.j2",)
    assert error.to_dict()["missing_markers"] == ["docker/compose/workspace.base.yml.j2"]
    assert "details" not in error.to_dict()


@pytest.mark.parametrize(
    ("missing_marker", "kind"),
    (
        ("uv.lock", "file"),
        ("alembic.ini", "file"),
        (".env.example", "file"),
        ("openapi.json", "file"),
        ("migrations", "dir"),
    ),
)
@pytest.mark.unit
def test_source_checkout_requires_control_plane_docker_build_inputs(
    tmp_path: Path,
    missing_marker: str,
    kind: str,
) -> None:
    """Verify Docker build input markers are required checkout assets."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    marker_path = checkout / missing_marker
    if marker_path.exists():
        if kind == "dir":
            marker_path.rmdir()
        else:
            marker_path.unlink()

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.missing_markers == (missing_marker,)
    assert error.to_dict()["missing_markers"] == [missing_marker]
    assert "details" not in error.to_dict()


@pytest.mark.unit
def test_source_checkout_marker_probe_oserror_reports_unreadable_not_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify marker probe `OSError` values become unreadable details."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    releasing_path = (checkout / "RELEASING.md").resolve()
    original_is_file = Path.is_file

    def _is_file(self: Path) -> bool:
        """Raise on one marker file and delegate other file checks."""
        if self.resolve() == releasing_path:
            raise OSError("permission denied")
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_file", _is_file)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.missing_markers == ()
    assert error.details["unreadable_paths"] == [str(releasing_path)]
    assert "missing_markers" not in error.to_dict()


@pytest.mark.unit
def test_source_checkout_unreadable_marker_reports_unreadable_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify unreadable marker paths are reported in checkout details."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    unreadable_marker = (checkout / "RELEASING.md").resolve()

    def _path_readable(path: Path) -> bool:
        """Mark only the selected marker as unreadable."""
        return path.resolve() != unreadable_marker

    monkeypatch.setattr(source_assets, "_path_readable", _path_readable)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.missing_markers == ()
    assert error.details == {"unreadable_paths": [str(unreadable_marker)]}


@pytest.mark.unit
def test_source_checkout_resolve_failure_uses_expanded_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify source checkout resolve failures use the expanded path fallback."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    candidate = Path("~/missing-awf-source-checkout")
    expanded = home / "missing-awf-source-checkout"
    original_resolve = Path.resolve

    def _resolve_unavailable(self: Path, *args: object, **kwargs: object) -> Path:
        """Raise during resolution only for the expanded candidate."""
        if self == expanded:
            raise OSError("permission denied")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve_unavailable)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(candidate, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == expanded.absolute()
    assert error.details == {"path_status": "missing"}


@pytest.mark.unit
def test_source_checkout_expanduser_failure_remains_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify source checkout expanduser failures remain reason-coded."""
    candidate = Path("~/missing-awf-source-checkout")

    def _expanduser_unavailable(self: Path) -> Path:
        """Raise the same expanduser error produced by pathlib."""
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", _expanduser_unavailable)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(candidate, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == candidate.absolute()
    assert error.details == {"path_status": "missing"}


@pytest.mark.unit
def test_source_checkout_root_is_dir_oserror_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify source checkout directory probe failures are reason-coded."""
    checkout = tmp_path / "checkout"
    resolved = checkout.resolve()
    original_is_dir = Path.is_dir

    def _is_dir(self: Path) -> bool:
        """Raise on the checkout root and delegate other directory checks."""
        if self == resolved:
            raise OSError("permission denied")
        return original_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _is_dir)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == resolved
    assert error.details == {"path_status": "unreadable", "error_type": "OSError"}


@pytest.mark.unit
def test_source_checkout_root_exists_oserror_reports_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify source checkout existence probe failures report unreadable roots."""
    checkout_file = tmp_path / "checkout-file"
    checkout_file.write_text("not a directory\n", encoding="utf-8")
    resolved = checkout_file.resolve()
    original_exists = Path.exists

    def _exists(self: Path) -> bool:
        """Raise on the checkout root and delegate other existence checks."""
        if self == resolved:
            raise OSError("permission denied")
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", _exists)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout_file, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == resolved
    assert error.details == {"path_status": "unreadable"}


@pytest.mark.unit
def test_source_checkout_file_root_reports_not_directory(tmp_path: Path) -> None:
    """Verify file roots are reported as not-directory checkout paths."""
    checkout_file = tmp_path / "checkout-file"
    checkout_file.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout_file, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == checkout_file.resolve()
    assert error.details == {"path_status": "not_directory"}


@pytest.mark.unit
def test_unreadable_source_checkout_reports_source_checkout_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify unreadable checkout roots use the invalid checkout reason."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    resolved = checkout.resolve()

    def _unreadable_root(path: Path) -> bool:
        """Mark only the checkout root as unreadable."""
        return path.resolve() != resolved

    monkeypatch.setattr(source_assets, "_path_readable", _unreadable_root)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == resolved
    assert error.missing_markers == ()
    assert error.details["unreadable_paths"] == [str(resolved)]


@pytest.mark.unit
def test_stale_source_checkout_metadata_fails_without_package_fallback(tmp_path: Path) -> None:
    """Verify stale metadata fails when package fallback cannot satisfy assets."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    metadata = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW).to_metadata()
    (checkout / "docker/control-plane.Dockerfile").unlink()

    with pytest.raises(SourceCheckoutError) as exc_info:
        verified_source_from_metadata(metadata, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_ASSETS_STALE"
    assert error.root == checkout.resolve()
    assert error.missing_markers == ("docker/control-plane.Dockerfile",)


@pytest.mark.unit
def test_stale_source_metadata_not_masked_by_available_packaged_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stale source metadata fails loudly even when a valid packaged root is resolvable.

    The no-mask invariant must hold by construction: ``verified_source_from_metadata``
    never substitutes packaged assets. This pins that behavior even when a fully
    valid packaged bootstrap root IS available to the process, so a future change
    that wired in a silent package fallback would fail here.
    """
    import awf.service.bootstrap as bootstrap

    packaged_root = _write_valid_source_checkout(tmp_path / "packaged").resolve()
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: (packaged_root,))
    # Precondition: a valid packaged/bootstrap asset root really is resolvable now.
    assert bootstrap.get_bootstrap_asset_root() == packaged_root

    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    metadata = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW).to_metadata()
    (checkout / "docker/control-plane.Dockerfile").unlink()

    with pytest.raises(SourceCheckoutError) as exc_info:
        verified_source_from_metadata(metadata, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_ASSETS_STALE"
    assert error.root == checkout.resolve()
    assert error.missing_markers == ("docker/control-plane.Dockerfile",)
    assert error.details["fallback_used"] is False
    # The stale path reports the source root, never the available packaged root.
    assert str(packaged_root) not in str(error.root)


@pytest.mark.unit
def test_stale_source_checkout_metadata_reports_contract_drift(tmp_path: Path) -> None:
    """Verify stale source metadata reports marker and asset contract drift."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    metadata = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW).to_metadata()
    stale_metadata = metadata.model_copy(
        update={
            "markers": ("legacy-marker",),
            "asset_paths": source_assets.SourceCheckoutAssetPaths(
                compose_file="docker/compose/legacy.yml"
            ),
        }
    )

    with pytest.raises(SourceCheckoutError) as exc_info:
        verified_source_from_metadata(stale_metadata, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_ASSETS_STALE"
    assert error.root == checkout.resolve()
    assert error.details["fallback_used"] is False
    assert error.details["expected_markers"] == list(SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS)
    assert error.details["recorded_markers"] == ["legacy-marker"]
    assert error.details["expected_asset_paths"] == (
        source_assets.SourceCheckoutAssetPaths().model_dump(mode="json")
    )
    assert error.details["recorded_asset_paths"] == (
        stale_metadata.asset_paths.model_dump(mode="json")
    )


@pytest.mark.unit
def test_path_readable_handles_missing_non_posix_and_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify readability helper handles missing, non-POSIX, and OS errors."""
    missing_path = tmp_path / "missing"
    readable_path = tmp_path / "readable.txt"
    unreadable_path = tmp_path / "unreadable.txt"
    readable_path.write_text("ok\n", encoding="utf-8")
    unreadable_path.write_text("ok\n", encoding="utf-8")

    assert source_assets._path_readable(missing_path) is False

    monkeypatch.setattr(source_assets.os, "name", "nt")
    assert source_assets._path_readable(readable_path) is True

    original_exists = Path.exists

    def _exists(self: Path) -> bool:
        """Raise on one path and delegate other existence checks."""
        if self == unreadable_path:
            raise OSError("permission denied")
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", _exists)
    assert source_assets._path_readable(unreadable_path) is False


@pytest.mark.unit
def test_now_utc_returns_timezone_aware_timestamp() -> None:
    """Verify source asset timestamps are timezone-aware UTC values."""
    assert source_assets._now_utc().tzinfo is UTC


@pytest.mark.unit
def test_source_checkout_metadata_stale_detection_ignores_baseline_detail_count(
    tmp_path: Path,
) -> None:
    """Verify baseline staleness details do not make fresh metadata stale."""
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    metadata = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW).to_metadata()

    is_stale, details = source_assets._source_checkout_contract_staleness(
        metadata,
        baseline_details={
            "fallback_used": False,
            "revalidated_at": _FIXED_NOW.isoformat(),
        },
    )

    assert is_stale is False
    assert details == {
        "fallback_used": False,
        "revalidated_at": _FIXED_NOW.isoformat(),
    }
    assert (
        verified_source_from_metadata(metadata, clock=lambda: _FIXED_NOW).root == checkout.resolve()
    )
