import tomllib
from pathlib import Path

import pytest

import awf
from tests.packaging_build import pyproject_project_version

REPO_ROOT = Path(__file__).parents[3]


def test_packaging_metadata() -> None:
    """Test that the pyproject.toml package name and entrypoints match the plan."""
    pyproject_path = REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    assert data["project"]["name"] == "agent-workspace-fabric"

    # 2. project.scripts.awf exists as the canonical operator CLI
    scripts = data["project"].get("scripts", {})
    assert "awf" in scripts
    assert "awf-watchdog" not in scripts


def test_readme_install_paths() -> None:
    """Test that the README explicitly documents the primary install paths."""
    readme_path = REPO_ROOT / "docs" / "GETTING_STARTED.md"
    content = readme_path.read_text()

    assert "uv tool install agent-workspace-fabric" in content
    assert "pipx install agent-workspace-fabric" in content
    assert "python -m venv .venv" in content
    assert "pip install agent-workspace-fabric" in content

    # 4. README preserves the `git clone` path
    assert "git clone" in content, "README must preserve the git clone contributor path"


def test_wheel_includes_bootstrap_assets_without_secret_env_files() -> None:
    """The built wheel must include the local bootstrap context needed by awf service."""
    pyproject_path = REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    target = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    force_include = target.get("force-include", {})

    assert force_include["docker/agent-runtime.Dockerfile"] == (
        "awf/bootstrap_assets/docker/agent-runtime.Dockerfile"
    )
    assert force_include["docker/control-plane.Dockerfile"] == (
        "awf/bootstrap_assets/docker/control-plane.Dockerfile"
    )
    assert force_include["docker/compose/local-service.yml"] == (
        "awf/bootstrap_assets/docker/compose/local-service.yml"
    )
    assert force_include["docker/compose/workspace.base.yml.j2"] == (
        "awf/bootstrap_assets/docker/compose/workspace.base.yml.j2"
    )
    assert force_include[".env.example"] == "awf/bootstrap_assets/.env.example"
    assert force_include["migrations"] == "awf/bootstrap_assets/migrations"
    assert force_include["src"] == "awf/bootstrap_assets/src"
    assert force_include["openapi.json"] == "awf/bootstrap_assets/openapi.json"

    excluded = set(target.get("exclude", []))
    assert "docker/compose/.env" in excluded
    assert all("docker/compose/.env" not in destination for destination in force_include.values())

    sdist = data["tool"]["hatch"]["build"]["targets"]["sdist"]
    sdist_includes = set(sdist.get("include", []))
    assert "/docker/compose/local-service.yml" in sdist_includes
    assert "/docker/agent-runtime.Dockerfile" in sdist_includes
    assert "/migrations" in sdist_includes
    assert "/src" in sdist_includes
    assert "/openapi.json" in sdist_includes
    assert "/docker/compose/.env" in set(sdist.get("exclude", []))


def test_sdist_includes_installer_release_metadata() -> None:
    """The sdist ships the checked-in installer/release metadata (T11/T12 lanes)."""
    pyproject_path = REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    sdist_includes = set(data["tool"]["hatch"]["build"]["targets"]["sdist"].get("include", []))
    assert "/packaging/install.sh" in sdist_includes
    assert "/scripts/generate_install_manifest.py" in sdist_includes
    assert "/RELEASING.md" in sdist_includes


def test_control_plane_dockerfile_copies_forced_bootstrap_assets_before_uv_sync() -> None:
    """The service image build must include Hatch forced-includes before uv sync."""
    pyproject_path = REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    force_include = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    dockerfile_prefix = (
        (REPO_ROOT / "docker" / "control-plane.Dockerfile")
        .read_text()
        .split(
            "RUN uv sync --frozen --extra dev",
            maxsplit=1,
        )[0]
    )

    for source_path in force_include:
        assert source_path in dockerfile_prefix


# --------------------------------------------------------------------------- #
# Version-drift guard
# --------------------------------------------------------------------------- #
#
# These are sub-millisecond static checks (no build artifact), so they live here
# in the fast static-packaging suite rather than the ``slow``-marked build-backed
# module — drift between ``pyproject.toml`` and ``src/awf/__init__.py`` must be
# caught by fast CI lanes, not only a scheduled slow run.


def test_runtime_version_matches_pyproject() -> None:
    """``awf.__version__`` tracks ``pyproject[project].version`` (installer post-check)."""
    assert awf.__version__ == pyproject_project_version()


def test_installed_distribution_version_matches_pyproject() -> None:
    """When resolvable, the installed distribution metadata version matches pyproject."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("agent-workspace-fabric")
    except PackageNotFoundError:  # pragma: no cover - depends on install layout
        pytest.skip("agent-workspace-fabric distribution metadata is not resolvable")
    assert installed == pyproject_project_version()
