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
