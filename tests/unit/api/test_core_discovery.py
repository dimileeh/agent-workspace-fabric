"""Core discovery endpoint contract tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from awf import __version__
from awf.api.app import create_app
from awf.common.config import get_settings
from awf.service import core_discovery as core_discovery_service

DISCOVERY_PATH = "/.well-known/awf-core.json"


def _client_without_database() -> AsyncClient:
    """Test helper for client without database."""
    app = create_app(use_lifespan=False)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_core_discovery_is_public_with_token_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify core discovery is public with token configured."""
    token = "secret-token-that-must-not-leak"
    monkeypatch.setenv("AWF_API_TOKEN", token)
    monkeypatch.setenv("AWF_GIT_COMMIT", "abc1234")
    get_settings.cache_clear()

    try:
        async with _client_without_database() as client:
            response = await client.get(DISCOVERY_PATH)
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "package_name": "agent-workspace-fabric",
        "package_version": __version__,
        "git_commit": "abc1234",
        "capabilities": ["workspace.execution.v1"],
    }
    assert token not in response.text


async def test_core_discovery_and_health_do_not_require_database() -> None:
    """Verify core discovery and health do not require database."""
    async with _client_without_database() as client:
        discovery = await client.get(DISCOVERY_PATH)
        health = await client.get("/healthz")

    assert discovery.status_code == 200
    assert health.status_code == 200


async def test_core_discovery_uses_safe_unknown_git_commit_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify core discovery uses safe unknown git commit fallback."""
    monkeypatch.delenv("AWF_GIT_COMMIT", raising=False)
    monkeypatch.setattr(
        "awf.service.core_discovery._git_rev_parse_head",
        lambda: None,
    )

    async with _client_without_database() as client:
        response = await client.get(DISCOVERY_PATH)

    assert response.status_code == 200
    assert response.json()["git_commit"] == "unknown"


def test_git_rev_parse_head_anchors_lookup_to_core_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify git rev parse head anchors lookup to core checkout."""
    foreign_repo = tmp_path / "foreign-project"
    foreign_repo.mkdir()
    monkeypatch.chdir(foreign_repo)
    expected_root = Path(core_discovery_service.__file__).resolve().parents[3]

    def _run(args: list[str], **kwargs: object) -> SimpleNamespace:
        """Test helper for run."""
        git_dir_index = args.index("-C") if "-C" in args else None
        command_root = Path(args[git_dir_index + 1]) if git_dir_index is not None else None
        stdout = "core-head\n" if command_root == expected_root else "foreign-head\n"
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(core_discovery_service.subprocess, "run", _run)

    assert core_discovery_service._git_rev_parse_head() == "core-head"


def test_git_rev_parse_head_returns_none_outside_core_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify git rev parse head returns none outside core source checkout."""
    installed_module = tmp_path / ".venv/lib/python3.12/site-packages/awf/service/core_discovery.py"
    monkeypatch.setattr(core_discovery_service, "__file__", str(installed_module))

    def _run(args: list[str], **kwargs: object) -> SimpleNamespace:
        """Test helper for run."""
        raise AssertionError("installed-layout discovery must not run git from a surrounding repo")

    monkeypatch.setattr(core_discovery_service.subprocess, "run", _run)

    assert core_discovery_service._git_rev_parse_head() is None


def test_core_discovery_payload_from_state_uses_default_when_cache_missing() -> None:
    """Verify core discovery payload from state uses default when cache missing."""
    payload = core_discovery_service.core_discovery_payload_from_state(SimpleNamespace())

    assert payload.git_commit == "unknown"
    assert payload.capabilities == ("workspace.execution.v1",)


def test_git_rev_parse_head_returns_none_when_source_checkout_has_no_git_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify git rev parse head returns none when source checkout has no git dir."""
    checkout = tmp_path / "checkout"
    module_file = checkout / "src" / "awf" / "service" / "core_discovery.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_discovery_service, "__file__", str(module_file))

    def _run(args: list[str], **kwargs: object) -> SimpleNamespace:
        """Test helper for run."""
        raise AssertionError("git must not run when the checkout has no .git directory")

    monkeypatch.setattr(core_discovery_service.subprocess, "run", _run)

    assert core_discovery_service._git_rev_parse_head() is None


def test_git_rev_parse_head_returns_none_when_git_command_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify git rev parse head returns none when git command fails."""

    def _run(args: list[str], **kwargs: object) -> SimpleNamespace:
        """Test helper for run."""
        return SimpleNamespace(returncode=128, stdout="")

    monkeypatch.setattr(core_discovery_service.subprocess, "run", _run)

    assert core_discovery_service._git_rev_parse_head() is None


def test_git_rev_parse_head_returns_none_when_git_exec_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify git rev parse head returns none when git exec raises."""

    def _run(args: list[str], **kwargs: object) -> SimpleNamespace:
        """Test helper for run."""
        raise OSError("git unavailable")

    monkeypatch.setattr(core_discovery_service.subprocess, "run", _run)

    assert core_discovery_service._git_rev_parse_head() is None


async def test_core_discovery_caches_git_commit_before_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify core discovery caches git commit before requests."""
    monkeypatch.delenv("AWF_GIT_COMMIT", raising=False)
    calls = 0

    def _initial_git_commit() -> str:
        """Test helper for initial git commit."""
        nonlocal calls
        calls += 1
        return "cached-head"

    def _request_path_git_commit() -> str:
        """Test helper for request path git commit."""
        raise AssertionError("discovery request path must not resolve git commit")

    monkeypatch.setattr(core_discovery_service, "_git_rev_parse_head", _initial_git_commit)
    app = create_app(use_lifespan=False)
    monkeypatch.setattr(core_discovery_service, "_git_rev_parse_head", _request_path_git_commit)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(DISCOVERY_PATH)
        second = await client.get(DISCOVERY_PATH)

    assert first.status_code == 200
    assert first.json()["git_commit"] == "cached-head"
    assert second.status_code == 200
    assert second.json()["git_commit"] == "cached-head"
    assert calls == 1


async def test_core_discovery_does_not_weaken_protected_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify core discovery does not weaken protected routes."""
    monkeypatch.setenv("AWF_API_TOKEN", "protected-token")
    get_settings.cache_clear()

    try:
        async with _client_without_database() as client:
            discovery = await client.get(DISCOVERY_PATH)
            missing_auth = await client.get("/v1/operations")
    finally:
        get_settings.cache_clear()

    assert discovery.status_code == 200
    assert missing_auth.status_code == 401
    assert missing_auth.headers["WWW-Authenticate"] == "Bearer"
    assert missing_auth.json()["detail"]["error_code"] == "UNAUTHORIZED"
