"""Build-backed wheel/sdist content and distribution-metadata tests (T13).

These build the real ``agent-workspace-fabric`` wheel and sdist via ``uv build``
and assert that the packaged bootstrap context (compose, Dockerfiles, migrations,
openapi, env example, src) and installer metadata ship in the built artifacts —
the asset surface a clean install outside the source checkout depends on. They
skip cleanly when the build cannot run offline; CI builds release artifacts and
exercises them fully.
"""

from __future__ import annotations

import email
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.packaging_build import (
    REPO_ROOT,
    BuiltDistributions,
    PackageBuildUnavailableError,
    build_distributions,
    pyproject_project_version,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(600)]

# The five markers ``bootstrap._is_bootstrap_asset_root`` requires, relative to
# the wheel's ``awf/bootstrap_assets/`` root.
_BOOTSTRAP_ROOT_MARKERS = (
    "docker/agent-runtime.Dockerfile",
    "docker/compose/local-service.yml",
    "docker/control-plane.Dockerfile",
    "pyproject.toml",
    "src/awf/__init__.py",
)
_WHEEL_ASSET_ROOT = "awf/bootstrap_assets"
_CONSOLE_BUILD_ASSETS = (
    "Dockerfile",
    "package.json",
    "package-lock.json",
    "next.config.ts",
    "postcss.config.mjs",
    "tsconfig.json",
    "app",
    "components",
    "lib",
    "scripts",
)
_EXCLUDED_CONSOLE_ARTIFACTS = (
    "apps/console/.env.local",
    "apps/console/.next",
    "apps/console/next-env.d.ts",
    "apps/console/node_modules",
    "apps/console/playwright-report",
    "apps/console/test-results",
    "apps/console/tsconfig.tsbuildinfo",
)


@pytest.fixture(scope="module")
def built() -> BuiltDistributions:
    """Build the wheel+sdist once for this module, skipping if unavailable."""
    try:
        return build_distributions()
    except PackageBuildUnavailableError as exc:  # pragma: no cover - env dependent
        pytest.skip(str(exc))


def _wheel_names(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def _present(names: list[str], path: str) -> bool:
    """Return whether ``path`` is present as a file or a directory prefix."""
    prefix = f"{path}/"
    return path in names or any(name.startswith(prefix) for name in names)


def _control_plane_copy_sources() -> list[str]:
    """Return every COPY source path in ``docker/control-plane.Dockerfile``.

    The last token on each ``COPY`` line is the in-image destination; everything
    before it is a build-context source that force-include must relocate into the
    wheel so the in-container service image build stays valid.
    """
    dockerfile = (REPO_ROOT / "docker" / "control-plane.Dockerfile").read_text(encoding="utf-8")
    sources: list[str] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        tokens = stripped[len("COPY ") :].split()
        # Drop the destination (last token) and any option flags (--from=, --chown=, …).
        sources.extend(t for t in tokens[:-1] if not t.startswith("--"))
    return sources


def _tracked_console_sources() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "apps/console"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def test_console_build_asset_sources_are_tracked() -> None:
    """Console package inputs must survive a clean GitHub Actions checkout."""
    tracked = _tracked_console_sources()
    for relative in _CONSOLE_BUILD_ASSETS:
        source = f"apps/console/{relative}"
        assert source in tracked or any(name.startswith(f"{source}/") for name in tracked), source


# --------------------------------------------------------------------------- #
# Wheel content
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relative",
    (
        "docker/compose/local-service.yml",
        "docker/compose/workspace.base.yml.j2",
        "docker/agent-runtime.Dockerfile",
        "docker/control-plane.Dockerfile",
        "migrations",
        "alembic.ini",
        "openapi.json",
        ".env.example",
        "pyproject.toml",
        "uv.lock",
        "src/awf/__init__.py",
        "docs",
    ),
)
def test_wheel_ships_bootstrap_asset(built: BuiltDistributions, relative: str) -> None:
    """Every packaged bootstrap asset is present under ``awf/bootstrap_assets/``."""
    names = _wheel_names(built.wheel)
    assert _present(names, f"{_WHEEL_ASSET_ROOT}/{relative}"), relative


def test_wheel_satisfies_bootstrap_root_markers(built: BuiltDistributions) -> None:
    """The wheel's bootstrap root carries all five ``_is_bootstrap_asset_root`` markers."""
    names = _wheel_names(built.wheel)
    for marker in _BOOTSTRAP_ROOT_MARKERS:
        assert _present(names, f"{_WHEEL_ASSET_ROOT}/{marker}"), marker


def test_wheel_includes_every_control_plane_copy_input(built: BuiltDistributions) -> None:
    """Every control-plane Dockerfile COPY input is packaged (valid build context)."""
    names = _wheel_names(built.wheel)
    for source in _control_plane_copy_sources():
        assert _present(names, f"{_WHEEL_ASSET_ROOT}/{source}"), source


@pytest.mark.parametrize("relative", _CONSOLE_BUILD_ASSETS)
def test_wheel_ships_console_build_asset(built: BuiltDistributions, relative: str) -> None:
    """The packaged bootstrap root carries the console Docker build context."""
    names = _wheel_names(built.wheel)
    assert _present(names, f"{_WHEEL_ASSET_ROOT}/apps/console/{relative}"), relative


def test_wheel_excludes_generated_console_artifacts(built: BuiltDistributions) -> None:
    """The wheel never ships local console build output, installs, or env files."""
    names = _wheel_names(built.wheel)
    for relative in _EXCLUDED_CONSOLE_ARTIFACTS:
        assert not _present(names, f"{_WHEEL_ASSET_ROOT}/{relative}"), relative


def test_wheel_excludes_compose_secret_env(built: BuiltDistributions) -> None:
    """The secret-bearing compose ``.env`` is never packaged into the wheel."""
    names = _wheel_names(built.wheel)
    assert not any(name.endswith("bootstrap_assets/docker/compose/.env") for name in names)


def test_wheel_distribution_metadata_matches_pyproject(built: BuiltDistributions) -> None:
    """``METADATA`` Name/Version and the ``awf`` console entry point are intact."""
    version = pyproject_project_version()
    with zipfile.ZipFile(built.wheel) as archive:
        metadata_name = next(
            (name for name in archive.namelist() if name.endswith(".dist-info/METADATA")),
            None,
        )
        assert metadata_name is not None, "METADATA not found in wheel"
        metadata = email.message_from_bytes(archive.read(metadata_name))
        entry_points_name = next(
            (name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")),
            None,
        )
        assert entry_points_name is not None, "entry_points.txt not found in wheel"
        entry_points = archive.read(entry_points_name).decode("utf-8")

    assert metadata["Name"] == "agent-workspace-fabric"
    assert metadata["Version"] == version
    assert "awf = awf.cli.main:app" in entry_points


# --------------------------------------------------------------------------- #
# Sdist content
# --------------------------------------------------------------------------- #


def _sdist_relative_names(sdist: Path) -> set[str]:
    """Return sdist member paths with the top-level ``<pkg>-<ver>/`` prefix removed."""
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
    relative: set[str] = set()
    for name in names:
        parts = name.split("/", 1)
        if len(parts) == 2:
            relative.add(parts[1])
    return relative


@pytest.mark.parametrize(
    "relative",
    (
        "docker/compose/local-service.yml",
        "docker/compose/workspace.base.yml.j2",
        "docker/agent-runtime.Dockerfile",
        "docker/control-plane.Dockerfile",
        "migrations",
        "src",
        "openapi.json",
        ".env.example",
        "alembic.ini",
    ),
)
def test_sdist_ships_top_level_bootstrap_asset(built: BuiltDistributions, relative: str) -> None:
    """The sdist carries the top-level bootstrap/source assets a build lane needs."""
    names = _sdist_relative_names(built.sdist)
    assert relative in names or any(name.startswith(f"{relative}/") for name in names), relative


@pytest.mark.parametrize(
    "relative",
    (
        "packaging/install.sh",
        "scripts/generate_install_manifest.py",
        "RELEASING.md",
    ),
)
def test_sdist_ships_installer_metadata(built: BuiltDistributions, relative: str) -> None:
    """The checked-in installer/release metadata ships in the source distribution."""
    names = _sdist_relative_names(built.sdist)
    assert relative in names, relative


@pytest.mark.parametrize("relative", _CONSOLE_BUILD_ASSETS)
def test_sdist_ships_console_build_asset(built: BuiltDistributions, relative: str) -> None:
    """The source distribution carries the console Docker build context."""
    names = _sdist_relative_names(built.sdist)
    asset = f"apps/console/{relative}"
    assert asset in names or any(name.startswith(f"{asset}/") for name in names), relative


def test_sdist_excludes_generated_console_artifacts(built: BuiltDistributions) -> None:
    """The sdist never ships local console build output, installs, or env files."""
    names = _sdist_relative_names(built.sdist)
    for relative in _EXCLUDED_CONSOLE_ARTIFACTS:
        assert relative not in names
        assert not any(name.startswith(f"{relative}/") for name in names), relative


def test_sdist_excludes_compose_secret_env(built: BuiltDistributions) -> None:
    """The secret-bearing compose ``.env`` is never packaged into the sdist."""
    names = _sdist_relative_names(built.sdist)
    assert "docker/compose/.env" not in names
