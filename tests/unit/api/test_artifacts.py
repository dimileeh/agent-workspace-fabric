"""Workspace artifact metadata API tests."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from awf.api.routes import artifacts
from awf.common.config import get_settings

_MINIMAL_BODY = {
    "repo_url": "git@github.com:example/artifacts.git",
    "branch_base": "main",
    "task_title": "List artifacts",
    "task_prompt": "Expose workspace artifact metadata.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_workspace(client: AsyncClient) -> str:
    response = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


def _configure_artifact_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, dict[str, str]]:
    work_dir = tmp_path / "awf-state"
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))
    get_settings.cache_clear()
    return work_dir, {"Authorization": "Bearer secret"}


class TestWorkspaceArtifacts:
    @pytest.mark.unit
    async def test_requires_local_bearer_token_when_configured(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        _configure_artifact_api(monkeypatch, tmp_path)

        response = await client.get(f"/v1/workspaces/{workspace_id}/artifacts")

        assert response.status_code == 401
        assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"

    @pytest.mark.unit
    async def test_missing_workspace_returns_not_found(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _, headers = _configure_artifact_api(monkeypatch, tmp_path)

        response = await client.get(
            "/v1/workspaces/ws_missing/artifacts",
            headers=headers,
        )

        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "NOT_FOUND"

    @pytest.mark.unit
    async def test_existing_workspace_without_artifact_directory_returns_empty_list(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        _, headers = _configure_artifact_api(monkeypatch, tmp_path)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None, "has_more": False}

    @pytest.mark.unit
    async def test_lists_recursive_file_metadata_and_skips_escaping_symlinks(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        work_dir, headers = _configure_artifact_api(monkeypatch, tmp_path)
        artifact_dir = work_dir / "artifacts" / workspace_id
        nested = artifact_dir / "logs"
        nested.mkdir(parents=True)
        stdout_path = nested / "stdout.txt"
        stdout_path.write_text("alpha\n", encoding="utf-8")
        screenshot_path = artifact_dir / "screenshot.png"
        screenshot_path.write_bytes(b"\x89PNG\r\n")
        (artifact_dir / "empty-dir").mkdir()
        outside_path = tmp_path / "outside.txt"
        outside_path.write_text("secret\n", encoding="utf-8")
        (artifact_dir / "outside-link.txt").symlink_to(outside_path)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            headers=headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert [item["relative_path"] for item in body["items"]] == [
            "logs/stdout.txt",
            "screenshot.png",
        ]

        stdout_item = body["items"][0]
        second_read = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            headers=headers,
        )
        assert stdout_item["artifact_id"] == second_read.json()["items"][0]["artifact_id"]
        assert stdout_item["workspace_id"] == workspace_id
        assert stdout_item["name"] == "stdout.txt"
        assert stdout_item["path"] == str(stdout_path.resolve())
        assert stdout_item["kind"] == "txt"
        assert stdout_item["size_bytes"] == len("alpha\n")
        datetime.fromisoformat(stdout_item["modified_at"])

    @pytest.mark.unit
    def test_listing_skips_file_deleted_during_metadata_read(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = "ws_artifact_race"
        artifact_dir = tmp_path / "artifacts" / workspace_id
        artifact_dir.mkdir(parents=True)
        stable_path = artifact_dir / "stable.txt"
        stable_path.write_text("kept\n", encoding="utf-8")
        deleted_path = artifact_dir / "deleted.txt"
        deleted_path.write_text("removed\n", encoding="utf-8")
        deleted_resolved = deleted_path.resolve()
        original_resolve = Path.resolve
        original_is_file = Path.is_file
        original_stat = Path.stat

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == deleted_path:
                return deleted_resolved
            return original_resolve(self, *args, **kwargs)

        def is_file_candidate(self: Path, *args: Any, **kwargs: Any) -> bool:
            if self == deleted_resolved:
                return True
            return original_is_file(self, *args, **kwargs)

        def stat_candidate(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == deleted_resolved:
                raise FileNotFoundError(str(self))
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)
        monkeypatch.setattr(Path, "is_file", is_file_candidate)
        monkeypatch.setattr(Path, "stat", stat_candidate)

        items = artifacts._list_artifacts(workspace_id, artifact_dir)

        assert [item.relative_path for item in items] == ["stable.txt"]

    @pytest.mark.unit
    def test_listing_returns_empty_for_symlinked_root(self, tmp_path: Path) -> None:
        workspace_id = "ws_artifact_symlink_root"
        target = tmp_path / "target"
        target.mkdir()
        artifact_dir = tmp_path / "artifacts" / workspace_id
        artifact_dir.parent.mkdir()
        artifact_dir.symlink_to(target, target_is_directory=True)

        assert artifacts._list_artifacts(workspace_id, artifact_dir) == []

    @pytest.mark.unit
    def test_listing_returns_empty_when_root_resolution_raises(self) -> None:
        class BrokenArtifactRoot:
            def is_dir(self) -> bool:
                return True

            def is_symlink(self) -> bool:
                return False

            def resolve(self, *, strict: bool = False) -> Path:
                assert strict is True
                raise OSError("cannot resolve")

        assert artifacts._list_artifacts("ws_broken_artifacts", BrokenArtifactRoot()) == []  # type: ignore[arg-type]

    @pytest.mark.unit
    def test_listing_skips_non_regular_files_and_uses_file_kind_for_extensionless_files(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_id = "ws_artifact_kinds"
        artifact_dir = tmp_path / "artifacts" / workspace_id
        artifact_dir.mkdir(parents=True)
        extensionless = artifact_dir / "README"
        extensionless.write_text("notes\n", encoding="utf-8")
        fifo = artifact_dir / "events.pipe"
        os.mkfifo(fifo)

        items = artifacts._list_artifacts(workspace_id, artifact_dir)

        assert [item.relative_path for item in items] == ["README"]
        assert items[0].kind == "file"

    @pytest.mark.unit
    async def test_artifact_listing_offloads_filesystem_scan(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        _, headers = _configure_artifact_api(monkeypatch, tmp_path)
        calls: list[tuple[Callable[..., object], tuple[Any, ...]]] = []

        async def fake_to_thread(func: Callable[..., object], /, *args: Any) -> object:
            calls.append((func, args))
            return func(*args)

        monkeypatch.setattr(artifacts.asyncio, "to_thread", fake_to_thread)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None, "has_more": False}
        assert calls == [
            (
                artifacts._list_artifacts,
                (workspace_id, artifacts._workspace_artifact_dir(workspace_id)),
            )
        ]
