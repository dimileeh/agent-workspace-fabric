"""Unit tests for packaged bootstrap-asset resolution edge cases.

These directly exercise the real ``_packaged_bootstrap_asset_root`` and
``_bootstrap_environment_file`` helpers, so this module intentionally avoids the
``_no_discovery`` autouse fixture in ``test_bootstrap_part_003.py`` that stubs
``_packaged_bootstrap_asset_root`` out for the pinning tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import awf.service.bootstrap as bootstrap
from tests.packaging_build import (
    BuiltDistributions,
    PackageBuildUnavailableError,
    build_distributions,
    extract_wheel,
)


class _FakeAwfResources:
    """Stand-in for ``importlib.resources.files("awf")`` over a real directory.

    The real lookup is ``files("awf").joinpath("bootstrap_assets")``; returning a
    filesystem ``Path`` (as a wheel/editable install would) keeps the resolver's
    ``isinstance(candidate, Path)`` guard satisfied while pointing it at the
    extracted wheel's bundled asset root.
    """

    def __init__(self, awf_dir: Path) -> None:
        self._awf_dir = awf_dir

    def joinpath(self, *parts: str) -> Path:
        return self._awf_dir.joinpath(*parts)


def _write_packaged_asset_root(awf_dir: Path) -> Path:
    """Create a hand-built ``awf/bootstrap_assets`` tree with every required marker.

    Mirrors what a built wheel ships under ``awf/bootstrap_assets/`` so the
    packaged-root resolver accepts it, without paying for a real ``uv build``.
    The marker set is exactly what ``_is_bootstrap_asset_root`` checks for.
    """
    root = awf_dir / "bootstrap_assets"
    markers = (
        "docker/agent-runtime.Dockerfile",
        "docker/compose/local-service.yml",
        "docker/control-plane.Dockerfile",
        "pyproject.toml",
        "src/awf/__init__.py",
    )
    for marker in markers:
        path = root / marker
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{marker}\n", encoding="utf-8")
    return root


@pytest.mark.unit
def test_bootstrap_environment_file_defaults_without_root() -> None:
    """Assets without a compose env file or root fall back to the compose env path."""
    assets = bootstrap._BootstrapAssets(  # noqa: SLF001
        root=None,
        agent_runtime_dockerfile=None,
        compose_file=Path("docker/compose/local-service.yml"),
        compose_env_file=None,
    )

    assert (
        bootstrap._bootstrap_environment_file(assets)  # noqa: SLF001
        == bootstrap.LOCAL_SERVICE_COMPOSE_ENV_FILE
    )


@pytest.mark.unit
@pytest.mark.parametrize("error", (ModuleNotFoundError, TypeError))
def test_packaged_asset_root_none_when_resource_lookup_raises(
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    """A missing/unsupported package resource yields no root instead of raising."""

    def _raise(_name: str) -> object:
        raise error("awf package resources unavailable")

    monkeypatch.setattr(bootstrap, "files", _raise)

    assert bootstrap._packaged_bootstrap_asset_root() is None  # noqa: SLF001


@pytest.mark.unit
def test_packaged_asset_root_none_for_non_path_traversable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zip-imported (non-Path) Traversable is rejected as a packaged asset root."""

    class _FakeTraversable:
        def joinpath(self, *_parts: str) -> object:
            return object()

    monkeypatch.setattr(bootstrap, "files", lambda _name: _FakeTraversable())

    assert bootstrap._packaged_bootstrap_asset_root() is None  # noqa: SLF001


@pytest.mark.unit
def test_hand_built_asset_root_resolves_as_packaged_bootstrap_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hand-built bootstrap-asset tree resolves as the packaged root, no build needed.

    This is the hermetic twin of ``test_extracted_wheel_resolves_packaged_bootstrap_root``:
    it drives the real ``_packaged_bootstrap_asset_root`` /
    ``is_packaged_bootstrap_asset_root`` / ``get_bootstrap_asset_root`` /
    ``_resolve_bootstrap_assets`` happy paths without ``uv build``. The packaged
    fallback is otherwise covered only by the ``slow`` wheel test, which skips on
    CI lanes where the build backend is unavailable — leaving those lines
    uncovered. Keeping a fast twin pins the coverage deterministically.
    """
    awf_dir = tmp_path / "site-packages" / "awf"
    packaged_root = _write_packaged_asset_root(awf_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: ())
    monkeypatch.setattr(bootstrap, "files", lambda _name: _FakeAwfResources(awf_dir))

    resolved = bootstrap._packaged_bootstrap_asset_root()  # noqa: SLF001
    assert resolved is not None
    assert resolved.resolve() == packaged_root.resolve()
    assert bootstrap._is_bootstrap_asset_root(packaged_root)  # noqa: SLF001
    assert bootstrap.is_packaged_bootstrap_asset_root(packaged_root)
    assert bootstrap.get_bootstrap_asset_root() == resolved

    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        require_agent_runtime=True,
    )
    assert assets.root is not None
    assert assets.root.resolve() == packaged_root.resolve()
    assert assets.compose_file == resolved / bootstrap.LOCAL_SERVICE_COMPOSE_FILE
    assert assets.agent_runtime_dockerfile == resolved / bootstrap.AGENT_RUNTIME_DOCKERFILE
    # Packaged assets carry no compose .env; bootstrap falls back to a plain ".env".
    assert assets.compose_env_file is None
    assert bootstrap._bootstrap_environment_file(assets) == Path(".env")  # noqa: SLF001


@pytest.fixture(scope="module")
def built_wheel() -> BuiltDistributions:
    """Build the wheel once for this module, skipping if the build is unavailable."""
    try:
        return build_distributions()
    except PackageBuildUnavailableError as exc:  # pragma: no cover - env dependent
        pytest.skip(str(exc))


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_extracted_wheel_resolves_packaged_bootstrap_root(
    built_wheel: BuiltDistributions,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A real extracted wheel resolves as the packaged bootstrap root from outside any checkout.

    Drives ``_packaged_bootstrap_asset_root`` / ``is_packaged_bootstrap_asset_root`` /
    ``get_bootstrap_asset_root`` against the built wheel's ``awf/bootstrap_assets/``
    while source discovery is forced to find nothing and cwd sits outside the
    checkout, so the packaged fallback is the only asset source — exactly how a
    clean install locates compose/agent-runtime assets with no source checkout.
    """
    extract_root = extract_wheel(built_wheel.wheel, tmp_path / "wheel")
    packaged_root = extract_root / "awf" / "bootstrap_assets"
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: ())
    monkeypatch.setattr(
        bootstrap,
        "files",
        lambda _name: _FakeAwfResources(extract_root / "awf"),
    )

    resolved = bootstrap._packaged_bootstrap_asset_root()  # noqa: SLF001
    assert resolved is not None
    assert resolved.resolve() == packaged_root.resolve()
    assert bootstrap._is_bootstrap_asset_root(packaged_root)  # noqa: SLF001
    assert bootstrap.is_packaged_bootstrap_asset_root(packaged_root)
    assert bootstrap.get_bootstrap_asset_root() == resolved

    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        require_agent_runtime=True,
    )
    assert assets.root is not None
    assert assets.root.resolve() == packaged_root.resolve()
    assert assets.compose_file == resolved / bootstrap.LOCAL_SERVICE_COMPOSE_FILE
    assert assets.agent_runtime_dockerfile == resolved / bootstrap.AGENT_RUNTIME_DOCKERFILE
    # Packaged assets carry no compose .env; bootstrap falls back to a plain ".env".
    assert assets.compose_env_file is None
    assert bootstrap._bootstrap_environment_file(assets) == Path(".env")  # noqa: SLF001
