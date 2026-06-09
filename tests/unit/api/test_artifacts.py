"""Workspace artifact metadata API tests."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.routes import artifacts
from awf.common.config import get_settings
from awf.db.session import make_session_factory
from awf.service import artifacts as artifact_service
from awf.service.artifacts import list_workspace_artifacts_metadata

_MINIMAL_BODY = {
    "repo_url": "git@github.com:example/artifacts.git",
    "branch_base": "main",
    "task_title": "List artifacts",
    "task_prompt": "Expose workspace artifact metadata.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
    "preflight": {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": "artifact API fixture",
    },
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


@contextmanager
def _temporary_work_dir(work_dir: Path) -> object:
    previous = os.environ.get("AWF_WORK_DIR")
    try:
        os.environ["AWF_WORK_DIR"] = str(work_dir)
        get_settings.cache_clear()
        yield
    finally:
        if previous is None:
            os.environ.pop("AWF_WORK_DIR", None)
        else:
            os.environ["AWF_WORK_DIR"] = previous
        get_settings.cache_clear()


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

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            headers={"Authorization": "Bearer wrong"},
        )

        assert response.status_code == 401
        assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"

    @pytest.mark.unit
    async def test_download_requires_local_bearer_token_when_configured(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        _configure_artifact_api(monkeypatch, tmp_path)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts/download",
            params={"path": "logs/stdout.txt"},
            headers={"Authorization": "Bearer wrong"},
        )

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
    async def test_download_missing_workspace_returns_not_found(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _, headers = _configure_artifact_api(monkeypatch, tmp_path)

        response = await client.get(
            "/v1/workspaces/ws_missing/artifacts/download",
            params={"path": "logs/stdout.txt"},
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
        assert response.json() == {
            "items": [],
            "next_cursor": None,
            "has_more": False,
            "limit": artifacts.DEFAULT_ARTIFACT_LIST_LIMIT,
            "cursor": None,
        }

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
        assert body["limit"] == 50
        assert body["cursor"] is None
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
    async def test_artifact_list_next_cursor_fetches_second_page(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        work_dir, headers = _configure_artifact_api(monkeypatch, tmp_path)
        artifact_dir = work_dir / "artifacts" / workspace_id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "a.txt").write_text("alpha\n", encoding="utf-8")
        (artifact_dir / "b.txt").write_text("beta\n", encoding="utf-8")

        first_response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            params={"limit": 1},
            headers=headers,
        )

        assert first_response.status_code == 200
        first_page = first_response.json()
        assert [item["relative_path"] for item in first_page["items"]] == ["a.txt"]
        assert first_page["has_more"] is True
        assert first_page["next_cursor"] is not None

        second_response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            params={"limit": 1, "cursor": first_page["next_cursor"]},
            headers=headers,
        )

        assert second_response.status_code == 200
        second_page = second_response.json()
        assert [item["relative_path"] for item in second_page["items"]] == ["b.txt"]
        assert second_page["has_more"] is False
        assert second_page["next_cursor"] is None
        assert second_page["cursor"] == first_page["next_cursor"]

    @pytest.mark.unit
    async def test_artifact_list_rejects_invalid_cursor(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        _, headers = _configure_artifact_api(monkeypatch, tmp_path)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            params={"cursor": "not-a-valid-cursor"},
            headers=headers,
        )

        assert response.status_code == 400
        assert response.json()["detail"] == {
            "error_code": "INVALID_CURSOR",
            "message": "Invalid artifact list cursor.",
        }

    @pytest.mark.unit
    async def test_artifact_listing_bounds_filesystem_metadata_reads(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        work_dir, headers = _configure_artifact_api(monkeypatch, tmp_path)
        artifact_dir = work_dir / "artifacts" / workspace_id
        artifact_dir.mkdir(parents=True)
        for name in ("a.txt", "b.txt", "c.txt"):
            (artifact_dir / name).write_text(f"{name}\n", encoding="utf-8")
        original_metadata_from_stat = artifact_service._metadata_from_stat
        metadata_reads: list[str] = []

        def tracking_metadata_from_stat(
            workspace_id: str,
            relative_path: str,
            resolved: Path,
            stat: os.stat_result,
        ) -> artifact_service.ArtifactMetadata:
            metadata_reads.append(relative_path)
            return original_metadata_from_stat(workspace_id, relative_path, resolved, stat)

        monkeypatch.setattr(artifact_service, "_metadata_from_stat", tracking_metadata_from_stat)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            params={"limit": 1},
            headers=headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["relative_path"] for item in body["items"]] == ["a.txt"]
        assert body["has_more"] is True
        assert body["next_cursor"] is not None
        assert metadata_reads == ["a.txt", "b.txt"]

    @pytest.mark.unit
    async def test_download_artifact_serves_bytes_and_headers(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        work_dir, headers = _configure_artifact_api(monkeypatch, tmp_path)
        artifact_dir = work_dir / "artifacts" / workspace_id / "logs"
        artifact_dir.mkdir(parents=True)
        stdout_path = artifact_dir / "stdout.txt"
        payload = b"alpha\nbeta\n"
        stdout_path.write_bytes(payload)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts/download",
            params={"path": "logs/stdout.txt"},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-length"] == str(len(payload))
        assert response.headers["content-type"].startswith("text/plain")
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert 'filename="stdout.txt"' in disposition

    @pytest.mark.unit
    async def test_download_workspace_artifact_direct_uses_workspace_artifact_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_direct_download"
        artifact_dir.mkdir(parents=True)
        report = artifact_dir / "report.txt"
        report.write_text("report\n", encoding="utf-8")

        async def require_workspace(session: object, workspace_id: str) -> None:
            assert session is fake_session
            assert workspace_id == "ws_direct_download"

        fake_session = object()
        monkeypatch.setattr(artifacts, "_require_workspace", require_workspace)
        monkeypatch.setattr(
            artifacts,
            "_workspace_artifact_dir",
            lambda _workspace_id: artifact_dir,
        )

        response = await artifacts.download_workspace_artifact(
            "ws_direct_download",
            path="report.txt",
            session=fake_session,  # type: ignore[arg-type]
        )

        assert Path(response.path) == report
        assert response.filename == "report.txt"

    @pytest.mark.unit
    async def test_download_missing_artifact_returns_not_found_without_host_path(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        work_dir, headers = _configure_artifact_api(monkeypatch, tmp_path)
        (work_dir / "artifacts" / workspace_id).mkdir(parents=True)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts/download",
            params={"path": "logs/missing.txt"},
            headers=headers,
        )

        assert response.status_code == 404
        body = response.json()["detail"]
        assert body["error_code"] == "NOT_FOUND"
        assert "logs/missing.txt" in body["message"]
        assert str(work_dir) not in body["message"]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "unsafe_path",
        [
            "",
            "../secret.txt",
            "/tmp/secret.txt",
            "logs/../secret.txt",
            r"..\secret.txt",
            r"logs\..\secret.txt",
            "logs/\x00/stdout.txt",
        ],
    )
    async def test_download_rejects_unsafe_artifact_paths(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        unsafe_path: str,
    ) -> None:
        workspace_id = await _create_workspace(client)
        _, headers = _configure_artifact_api(monkeypatch, tmp_path)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/artifacts/download",
            params={"path": unsafe_path},
            headers=headers,
        )

        assert response.status_code == 400
        assert response.json()["detail"]["error_code"] == "INVALID_ARTIFACT_PATH"

    @pytest.mark.unit
    async def test_download_rejects_symlink_file_and_intermediate_directory(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        work_dir, headers = _configure_artifact_api(monkeypatch, tmp_path)
        artifact_dir = work_dir / "artifacts" / workspace_id
        artifact_dir.mkdir(parents=True)
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("secret\n", encoding="utf-8")
        (artifact_dir / "outside-link.txt").symlink_to(outside_file)
        outside_dir = tmp_path / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("nested secret\n", encoding="utf-8")
        (artifact_dir / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

        for unsafe_path in ("outside-link.txt", "linked-dir/secret.txt"):
            response = await client.get(
                f"/v1/workspaces/{workspace_id}/artifacts/download",
                params={"path": unsafe_path},
                headers=headers,
            )

            assert response.status_code == 404
            body = response.json()["detail"]
            assert body["error_code"] == "NOT_FOUND"
            assert response.content not in {b"secret\n", b"nested secret\n"}
            assert str(tmp_path) not in body["message"]

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
        assert response.json() == {
            "items": [],
            "next_cursor": None,
            "has_more": False,
            "limit": 50,
            "cursor": None,
        }
        assert calls == [
            (
                artifact_service._list_artifacts_page,
                (
                    workspace_id,
                    artifacts._workspace_artifact_dir(workspace_id),
                    artifacts.DEFAULT_ARTIFACT_LIST_LIMIT,
                    None,
                ),
            )
        ]

    @pytest.mark.unit
    async def test_route_function_resolves_artifact_directory_for_existing_workspace(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        work_dir = tmp_path / "awf-state"
        artifact_dir = work_dir / "artifacts" / workspace_id
        artifact_dir.mkdir(parents=True)
        readme = artifact_dir / "README"
        readme.write_text("artifact notes\n", encoding="utf-8")

        with _temporary_work_dir(work_dir):
            async with make_session_factory(engine)() as session:
                response = await artifacts.list_workspace_artifacts(
                    workspace_id,
                    session=session,
                )

        assert [item.relative_path for item in response.items] == ["README"]
        assert response.items[0].workspace_id == workspace_id
        assert response.items[0].kind == "file"
        assert response.items[0].size_bytes == len("artifact notes\n")

    @pytest.mark.unit
    async def test_list_route_maps_invalid_cursor_to_structured_400(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _raise_invalid_cursor(*_args: Any, **_kwargs: Any) -> object:
            raise artifacts.InvalidBoundedListCursorError("bad cursor")

        monkeypatch.setattr(
            artifacts,
            "list_workspace_artifacts_metadata",
            _raise_invalid_cursor,
        )

        with pytest.raises(HTTPException) as exc_info:
            await artifacts.list_workspace_artifacts(
                "ws_test",
                cursor="bad",
                session=object(),  # type: ignore[arg-type]
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == {
            "error_code": "INVALID_CURSOR",
            "message": "Invalid artifact list cursor.",
        }

    @pytest.mark.unit
    async def test_require_workspace_raises_structured_404_for_missing_workspace(
        self,
        engine: AsyncEngine,
    ) -> None:
        async with make_session_factory(engine)() as session:
            with pytest.raises(HTTPException) as exc_info:
                await artifacts._require_workspace(session, "ws_missing")

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == {
            "error_code": "NOT_FOUND",
            "message": "No workspace with id ws_missing",
        }

    @pytest.mark.unit
    async def test_require_workspace_accepts_existing_workspace(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client)

        async with make_session_factory(engine)() as session:
            await artifacts._require_workspace(session, workspace_id)

    @pytest.mark.unit
    async def test_artifact_service_and_route_report_missing_workspace(
        self,
        engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        async with make_session_factory(engine)() as session:
            service_response = await list_workspace_artifacts_metadata(
                session,
                workspace_id="ws_missing",
                work_dir=tmp_path,
            )
            with pytest.raises(HTTPException) as exc_info:
                await artifacts.list_workspace_artifacts("ws_missing", session=session)

        assert service_response is None
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == {
            "error_code": "NOT_FOUND",
            "message": "No workspace with id ws_missing",
        }
