"""Artifact filesystem service tests."""

from __future__ import annotations

from builtins import open as builtins_open
from pathlib import Path
from typing import Any

import pytest

import awf.service.artifacts as artifacts_module
from awf.common.config import get_settings
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.artifacts import (
    MAX_ARTIFACT_CONTENT_BYTES,
    ArtifactNotFoundError,
    ArtifactOversizedError,
    ArtifactPathError,
    _artifact_id,
    _artifact_kind,
    _is_symlink,
    _resolve_artifact_root,
    artifact_id,
    artifact_kind,
    get_downloadable_artifact,
    get_workspace_artifact_content,
    list_artifacts,
    workspace_artifact_dir,
)
from tests.postgres import create_postgres_test_engine


class TestArtifactService:
    @pytest.mark.unit
    def test_workspace_artifact_dir_uses_managed_work_dir_root(self, tmp_path: Path) -> None:
        assert workspace_artifact_dir(tmp_path / "awf-state", "ws_123") == (
            tmp_path / "awf-state" / "artifacts" / "ws_123"
        )

    @pytest.mark.unit
    def test_workspace_artifact_dir_private_helper_uses_explicit_or_settings_work_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "settings-work"))
        get_settings.cache_clear()
        try:
            assert (
                artifacts_module._workspace_artifact_dir(  # noqa: SLF001
                    "ws_123",
                    work_dir=tmp_path / "explicit-work",
                )
                == tmp_path / "explicit-work" / "artifacts" / "ws_123"
            )
            assert artifacts_module._workspace_artifact_dir("ws_123") == (  # noqa: SLF001
                tmp_path / "settings-work" / "artifacts" / "ws_123"
            )
        finally:
            get_settings.cache_clear()

    @pytest.mark.unit
    def test_safe_nested_relative_path_resolves_download_metadata(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        report_dir = artifact_dir / "reports"
        report_dir.mkdir(parents=True)
        report = report_dir / "summary.json"
        payload = b'{"ok": true}\n'
        report.write_bytes(payload)

        artifact = get_downloadable_artifact(
            workspace_id="ws_artifacts",
            artifact_dir=artifact_dir,
            relative_path="reports/summary.json",
        )

        assert artifact.path == report.resolve()
        assert artifact.workspace_id == "ws_artifacts"
        assert artifact.relative_path == "reports/summary.json"
        assert artifact.name == "summary.json"
        assert artifact.size_bytes == len(payload)
        assert artifact.content_type == "application/json"

    @pytest.mark.unit
    def test_artifact_compatibility_helpers_delegate_to_public_helpers(
        self, tmp_path: Path
    ) -> None:
        assert artifacts_module._artifact_id("ws_artifacts", "README").startswith("art_")
        assert artifacts_module._artifact_kind(tmp_path / "README") == "file"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "unsafe_path",
        [
            "",
            ".",
            "../secret.txt",
            "/tmp/secret.txt",
            "reports/../secret.txt",
            r"..\secret.txt",
            r"reports\summary.json",
            "reports/\x00/summary.json",
        ],
    )
    def test_rejects_unsafe_relative_paths(self, tmp_path: Path, unsafe_path: str) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)

        with pytest.raises(ArtifactPathError):
            get_downloadable_artifact(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path=unsafe_path,
            )

    @pytest.mark.unit
    def test_missing_root_and_symlink_root_fail_closed(self, tmp_path: Path) -> None:
        missing_root = tmp_path / "artifacts" / "ws_missing_root"
        with pytest.raises(ArtifactNotFoundError):
            get_downloadable_artifact(
                workspace_id="ws_missing_root",
                artifact_dir=missing_root,
                relative_path="report.txt",
            )
        assert list_artifacts("ws_missing_root", missing_root) == []

        target = tmp_path / "target"
        target.mkdir()
        symlink_root = tmp_path / "artifacts" / "ws_symlink_root"
        symlink_root.parent.mkdir()
        symlink_root.symlink_to(target, target_is_directory=True)

        with pytest.raises(ArtifactNotFoundError):
            get_downloadable_artifact(
                workspace_id="ws_symlink_root",
                artifact_dir=symlink_root,
                relative_path="report.txt",
            )
        assert list_artifacts("ws_symlink_root", symlink_root) == []

    @pytest.mark.unit
    def test_symlinked_file_and_intermediate_directory_fail_closed(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("secret\n", encoding="utf-8")
        (artifact_dir / "outside-link.txt").symlink_to(outside_file)
        outside_dir = tmp_path / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("nested secret\n", encoding="utf-8")
        (artifact_dir / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

        for unsafe_path in ("outside-link.txt", "linked-dir/secret.txt"):
            with pytest.raises(ArtifactNotFoundError):
                get_downloadable_artifact(
                    workspace_id="ws_artifacts",
                    artifact_dir=artifact_dir,
                    relative_path=unsafe_path,
                )

    @pytest.mark.unit
    def test_deleted_during_stat_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        report = artifact_dir / "report.txt"
        report.write_text("report\n", encoding="utf-8")
        report_resolved = report.resolve()
        original_stat = Path.stat

        def stat_candidate(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == report_resolved:
                raise FileNotFoundError(str(self))
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat_candidate)

        with pytest.raises(ArtifactNotFoundError):
            get_downloadable_artifact(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="report.txt",
            )

    @pytest.mark.unit
    def test_directory_download_request_fails_closed(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        report_dir = artifact_dir / "reports"
        report_dir.mkdir(parents=True)

        with pytest.raises(ArtifactNotFoundError):
            get_downloadable_artifact(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="reports",
            )

    @pytest.mark.unit
    def test_root_resolving_to_non_directory_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        resolved_file = tmp_path / "resolved-file"
        resolved_file.write_text("not a directory\n", encoding="utf-8")
        original_resolve = Path.resolve

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == artifact_dir:
                return resolved_file
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)

        with pytest.raises(ArtifactNotFoundError):
            get_downloadable_artifact(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="report.txt",
            )

    @pytest.mark.unit
    def test_listing_returns_empty_when_directory_walk_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        original_iterdir = Path.iterdir

        def iterdir_candidate(self: Path) -> Any:
            if self == artifact_dir:
                raise OSError("iterdir failed")
            return original_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", iterdir_candidate)

        assert list_artifacts("ws_artifacts", artifact_dir) == []

    @pytest.mark.unit
    def test_listing_skips_directory_that_resolves_to_outside_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        nested = artifact_dir / "nested"
        nested.mkdir(parents=True)
        outside_file = tmp_path / "outside"
        outside_file.write_text("not a directory\n", encoding="utf-8")
        original_resolve = Path.resolve

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == nested:
                return outside_file
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)

        assert list_artifacts("ws_artifacts", artifact_dir) == []

    @pytest.mark.unit
    def test_listing_skips_directory_that_disappears_during_scan(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        nested = artifact_dir / "nested"
        nested.mkdir(parents=True)
        original_resolve = Path.resolve

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == nested:
                raise FileNotFoundError(str(self))
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)

        assert list_artifacts("ws_artifacts", artifact_dir) == []

    @pytest.mark.unit
    def test_is_symlink_fails_closed_on_filesystem_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_path = tmp_path / "artifact"
        original_is_symlink = Path.is_symlink

        def is_symlink_candidate(self: Path) -> bool:
            if self == artifact_path:
                raise OSError(str(self))
            return original_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", is_symlink_candidate)

        assert artifacts_module._is_symlink(artifact_path) is True

    @pytest.mark.unit
    def test_listing_reports_metadata_and_skips_deleted_files(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        stable = artifact_dir / "stable.txt"
        stable.write_text("kept\n", encoding="utf-8")
        deleted = artifact_dir / "deleted.txt"
        deleted.write_text("removed\n", encoding="utf-8")
        deleted_resolved = deleted.resolve()
        original_resolve = Path.resolve
        original_is_file = Path.is_file
        original_stat = Path.stat

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == deleted:
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

        items = list_artifacts("ws_artifacts", artifact_dir)

        assert [item.relative_path for item in items] == ["stable.txt"]
        assert items[0].workspace_id == "ws_artifacts"
        assert items[0].kind == "txt"
        assert items[0].content_type == "text/plain"

    @pytest.mark.unit
    def test_listing_skips_file_lost_between_classification_and_stat(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        flaky = artifact_dir / "flaky.txt"
        flaky.write_text("flaky\n", encoding="utf-8")
        flaky_resolved = flaky.resolve()
        original_resolve = Path.resolve
        original_is_file = Path.is_file
        original_stat = Path.stat

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == flaky:
                return flaky_resolved
            return original_resolve(self, *args, **kwargs)

        def is_file_candidate(self: Path, *args: Any, **kwargs: Any) -> bool:
            if self == flaky_resolved:
                return True
            return original_is_file(self, *args, **kwargs)

        def stat_candidate(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == flaky_resolved:
                raise FileNotFoundError(str(self))
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)
        monkeypatch.setattr(Path, "is_file", is_file_candidate)
        monkeypatch.setattr(Path, "stat", stat_candidate)

        assert list_artifacts("ws_artifacts", artifact_dir) == []

    @pytest.mark.unit
    def test_listing_skips_file_when_explicit_stat_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        stat_error = artifact_dir / "stat-error.txt"
        stat_error.write_text("flaky\n", encoding="utf-8")
        stable = artifact_dir / "stable.txt"
        stable.write_text("kept\n", encoding="utf-8")
        stat_error_resolved = stat_error.resolve()
        original_resolve = Path.resolve
        stat_failures = 0

        class FlakyResolvedArtifact:
            def is_relative_to(self, _root: Path) -> bool:
                return True

            def is_file(self) -> bool:
                return True

            def stat(self) -> Any:
                nonlocal stat_failures
                stat_failures += 1
                raise OSError("cannot stat artifact")

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == stat_error_resolved:
                return FlakyResolvedArtifact()
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)

        assert [item.relative_path for item in list_artifacts("ws_artifacts", artifact_dir)] == [
            "stable.txt"
        ]
        assert stat_failures == 1

    @pytest.mark.unit
    def test_private_listing_helpers_return_response_models_and_pages(
        self,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "a.txt").write_text("a\n", encoding="utf-8")
        (artifact_dir / "b.txt").write_text("b\n", encoding="utf-8")

        items = artifacts_module._list_artifacts("ws_artifacts", artifact_dir)  # noqa: SLF001
        page = artifacts_module._list_artifacts_page(  # noqa: SLF001
            "ws_artifacts",
            artifact_dir,
            limit=1,
            cursor=None,
        )

        assert [item.relative_path for item in items] == ["a.txt", "b.txt"]
        assert items[0].path.endswith("/a.txt")
        assert page.items[0].relative_path == "a.txt"
        assert page.has_more is True
        assert page.next_cursor is not None

    @pytest.mark.unit
    def test_listing_skips_entries_that_change_during_traversal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        flaky_dir = artifact_dir / "flaky-dir"
        flaky_dir.mkdir()
        flaky_file = artifact_dir / "flaky-file.txt"
        flaky_file.write_text("file\n", encoding="utf-8")
        stable = artifact_dir / "stable.txt"
        stable.write_text("stable\n", encoding="utf-8")
        original_resolve = Path.resolve
        original_is_symlink = Path.is_symlink

        symlink_checks: dict[Path, int] = {}

        def is_symlink_candidate(self: Path) -> bool:
            if self == flaky_file:
                symlink_checks[self] = symlink_checks.get(self, 0) + 1
                return symlink_checks[self] > 1
            return original_is_symlink(self)

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == flaky_dir:
                raise OSError("directory disappeared")
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_symlink", is_symlink_candidate)
        monkeypatch.setattr(Path, "resolve", resolve_candidate)

        assert [item.relative_path for item in list_artifacts("ws_artifacts", artifact_dir)] == [
            "stable.txt"
        ]

    @pytest.mark.unit
    def test_directory_entry_push_fails_closed_on_iterdir_and_is_dir_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        flaky = artifact_dir / "flaky"
        flaky.write_text("file\n", encoding="utf-8")
        original_iterdir = Path.iterdir
        original_is_dir = Path.is_dir

        def iterdir_candidate(self: Path) -> Any:
            if self == artifact_dir:
                raise OSError("cannot list")
            return original_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", iterdir_candidate)
        assert list_artifacts("ws_artifacts", artifact_dir) == []

        monkeypatch.setattr(Path, "iterdir", original_iterdir)

        def is_dir_candidate(self: Path, *args: Any, **kwargs: Any) -> bool:
            if self == flaky:
                raise OSError("cannot classify")
            return original_is_dir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_dir", is_dir_candidate)
        assert list_artifacts("ws_artifacts", artifact_dir) == []

    @pytest.mark.unit
    def test_listing_skips_directory_and_file_races_without_leaving_artifact_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        valid_dir = artifact_dir / "valid-dir"
        valid_dir.mkdir()
        (valid_dir / "nested.txt").write_text("nested\n", encoding="utf-8")
        dir_becomes_symlink = artifact_dir / "dir-becomes-symlink"
        dir_becomes_symlink.mkdir()
        dir_outside = artifact_dir / "dir-outside"
        dir_outside.mkdir()
        file_outside = artifact_dir / "file-outside.txt"
        file_outside.write_text("outside\n", encoding="utf-8")
        file_stat_error = artifact_dir / "file-stat-error.txt"
        file_stat_error.write_text("stat error\n", encoding="utf-8")
        outside_dir = tmp_path / "outside-dir"
        outside_dir.mkdir()
        outside_file = tmp_path / "outside-file.txt"
        outside_file.write_text("outside\n", encoding="utf-8")
        (artifact_dir / "symlink-skipped-in-push").symlink_to(outside_file)
        file_stat_error_resolved = file_stat_error.resolve()
        original_is_symlink = Path.is_symlink
        original_resolve = Path.resolve
        original_stat = Path.stat
        symlink_checks: dict[Path, int] = {}

        def is_symlink_candidate(self: Path) -> bool:
            if self == dir_becomes_symlink:
                symlink_checks[self] = symlink_checks.get(self, 0) + 1
                return symlink_checks[self] > 1
            return original_is_symlink(self)

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == dir_outside:
                return outside_dir
            if self == file_outside:
                return outside_file
            return original_resolve(self, *args, **kwargs)

        def stat_candidate(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == file_stat_error_resolved:
                raise OSError("cannot stat")
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_symlink", is_symlink_candidate)
        monkeypatch.setattr(Path, "resolve", resolve_candidate)
        monkeypatch.setattr(Path, "stat", stat_candidate)

        assert [item.relative_path for item in list_artifacts("ws_artifacts", artifact_dir)] == [
            "valid-dir/nested.txt"
        ]

    @pytest.mark.unit
    async def test_list_workspace_artifacts_metadata_returns_none_for_missing_workspace(
        self,
        tmp_path: Path,
    ) -> None:
        engine = await create_postgres_test_engine()
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                assert (
                    await artifacts_module.list_workspace_artifacts_metadata(
                        session,
                        workspace_id="ws_missing",
                        work_dir=tmp_path / "work",
                    )
                    is None
                )
        finally:
            await engine.dispose()

    @pytest.mark.unit
    async def test_list_workspace_artifacts_metadata_pages_existing_workspace(
        self,
        tmp_path: Path,
    ) -> None:
        engine = await create_postgres_test_engine()
        factory = make_session_factory(engine)
        work_dir = tmp_path / "work"
        try:
            async with factory() as session:
                repo = WorkspaceRepository(session)
                ws = await repo.create(
                    repo_url="git@github.com:x/y.git",
                    branch_base="main",
                    task_title="artifacts",
                    task_prompt="artifacts",
                    agent="codex",
                    test_commands=[],
                    requires_database=False,
                )
                artifact_dir = workspace_artifact_dir(work_dir, ws.id)
                artifact_dir.mkdir(parents=True)
                (artifact_dir / "report.txt").write_text("report\n", encoding="utf-8")
                await session.commit()

                response = await artifacts_module.list_workspace_artifacts_metadata(
                    session,
                    workspace_id=ws.id,
                    work_dir=work_dir,
                    limit=1,
                )
        finally:
            await engine.dispose()

        assert response is not None
        assert [item.relative_path for item in response.items] == ["report.txt"]
        assert response.has_more is False

    @pytest.mark.unit
    def test_resolved_artifact_root_must_remain_a_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        resolved_file = tmp_path / "not-a-directory"
        resolved_file.write_text("file\n", encoding="utf-8")
        original_resolve = Path.resolve

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == artifact_dir:
                return resolved_file
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)

        with pytest.raises(ArtifactNotFoundError):
            _resolve_artifact_root(artifact_dir)

    @pytest.mark.unit
    def test_artifact_private_aliases_match_public_helpers(self, tmp_path: Path) -> None:
        report = tmp_path / "report.txt"

        assert _artifact_id("ws_artifacts", "reports/summary.json") == artifact_id(
            "ws_artifacts",
            "reports/summary.json",
        )
        assert _artifact_id("ws_artifacts", "report.txt") == artifact_id(
            "ws_artifacts",
            "report.txt",
        )
        assert _artifact_kind(Path("summary.json")) == artifact_kind(Path("summary.json"))
        assert _artifact_kind(report) == artifact_kind(report) == "txt"

    @pytest.mark.unit
    def test_symlink_probe_fails_closed_when_filesystem_raises(self) -> None:
        class _UnstatablePath:
            def is_symlink(self) -> bool:
                raise OSError("cannot stat")

        assert _is_symlink(_UnstatablePath()) is True  # type: ignore[arg-type]

    @pytest.mark.unit
    def test_listing_skips_directory_that_resolves_outside_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        skipped_dir = artifact_dir / "outside-looking-dir"
        kept = artifact_dir / "kept.txt"
        skipped_dir.mkdir(parents=True)
        kept.write_text("kept\n", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        original_resolve = Path.resolve

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == skipped_dir:
                return outside
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)

        assert [item.relative_path for item in list_artifacts("ws_artifacts", artifact_dir)] == [
            "kept.txt"
        ]

    @pytest.mark.unit
    def test_listing_skips_symlinked_directory_after_queueing(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        linked_target = tmp_path / "outside-dir"
        linked_target.mkdir(parents=True)
        (linked_target / "secret.txt").write_text("secret\n", encoding="utf-8")
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "linked-dir").symlink_to(linked_target, target_is_directory=True)
        kept = artifact_dir / "kept.txt"
        kept.write_text("kept\n", encoding="utf-8")

        assert [item.relative_path for item in list_artifacts("ws_artifacts", artifact_dir)] == [
            "kept.txt"
        ]

    @pytest.mark.unit
    def test_listing_skips_directory_that_disappears_during_resolve(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        disappearing_dir = artifact_dir / "disappearing"
        disappearing_dir.mkdir(parents=True)
        (disappearing_dir / "hidden.txt").write_text("gone\n", encoding="utf-8")
        kept = artifact_dir / "kept.txt"
        kept.write_text("kept\n", encoding="utf-8")
        original_resolve = Path.resolve

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == disappearing_dir:
                raise FileNotFoundError(str(self))
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)

        assert [item.relative_path for item in list_artifacts("ws_artifacts", artifact_dir)] == [
            "kept.txt"
        ]

    @pytest.mark.unit
    def test_listing_skips_leaf_symlink_after_directory_queueing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        target = tmp_path / "outside.txt"
        target.write_text("secret\n", encoding="utf-8")
        link = artifact_dir / "link.txt"
        link.symlink_to(target)
        original_is_symlink = Path.is_symlink
        seen_link = False

        def is_symlink_candidate(self: Path) -> bool:
            nonlocal seen_link
            if self == link and not seen_link:
                seen_link = True
                return False
            return original_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", is_symlink_candidate)

        assert list_artifacts("ws_artifacts", artifact_dir) == []

    @pytest.mark.unit
    def test_get_workspace_artifact_content_reads_small_file(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        report = artifact_dir / "report.txt"
        payload = b"hello artifact\n"
        report.write_bytes(payload)

        name, content_type, size_bytes, content = get_workspace_artifact_content(
            workspace_id="ws_artifacts",
            artifact_dir=artifact_dir,
            relative_path="report.txt",
            limit_bytes=MAX_ARTIFACT_CONTENT_BYTES,
        )

        assert name == "report.txt"
        assert content_type == "text/plain"
        assert size_bytes == len(payload)
        assert content == payload

    @pytest.mark.unit
    def test_get_workspace_artifact_content_rejects_oversized_file(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        report = artifact_dir / "big.bin"
        report.write_bytes(b"x" * 100)

        with pytest.raises(ArtifactOversizedError):
            get_workspace_artifact_content(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="big.bin",
                limit_bytes=50,
            )

    @pytest.mark.unit
    def test_get_workspace_artifact_content_rejects_excessive_limit(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        report = artifact_dir / "small.bin"
        report.write_bytes(b"x")

        with pytest.raises(ArtifactOversizedError) as exc_info:
            get_workspace_artifact_content(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="small.bin",
                limit_bytes=MAX_ARTIFACT_CONTENT_BYTES + 1,
            )
        assert exc_info.value.detail is not None
        assert exc_info.value.detail["limit_bytes"] == MAX_ARTIFACT_CONTENT_BYTES + 1
        assert exc_info.value.detail["actual_bytes"] is None

    @pytest.mark.unit
    def test_get_workspace_artifact_content_rejects_missing_file(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)

        with pytest.raises(ArtifactNotFoundError):
            get_workspace_artifact_content(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="missing.txt",
                limit_bytes=MAX_ARTIFACT_CONTENT_BYTES,
            )

    @pytest.mark.unit
    def test_get_workspace_artifact_content_rejects_invalid_path(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)

        with pytest.raises(ArtifactPathError):
            get_workspace_artifact_content(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="../secret.txt",
                limit_bytes=MAX_ARTIFACT_CONTENT_BYTES,
            )

    @pytest.mark.unit
    def test_get_workspace_artifact_content_rejects_symlink_escape(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (artifact_dir / "link.txt").symlink_to(outside)

        with pytest.raises(ArtifactNotFoundError):
            get_workspace_artifact_content(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="link.txt",
                limit_bytes=MAX_ARTIFACT_CONTENT_BYTES,
            )

    @pytest.mark.unit
    def test_get_workspace_artifact_content_race_growth_after_stat(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        report = artifact_dir / "grow.bin"
        # Start small so stat check passes
        report.write_bytes(b"x" * 10)

        original_open = Path.open

        def racing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self.resolve() == report.resolve():
                # Grow the file just before the actual read using builtin open
                # to avoid recursion through Path.write_bytes -> Path.open
                with builtins_open(str(report), "wb") as f:  # noqa: PTH123
                    f.write(b"x" * 200)
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", racing_open)

        with pytest.raises(ArtifactOversizedError):
            get_workspace_artifact_content(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="grow.bin",
                limit_bytes=50,
            )

    @pytest.mark.unit
    def test_get_workspace_artifact_content_bounded_read_exact_limit(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        report = artifact_dir / "exact.bin"
        payload = b"x" * 50
        report.write_bytes(payload)

        name, content_type, size_bytes, content = get_workspace_artifact_content(
            workspace_id="ws_artifacts",
            artifact_dir=artifact_dir,
            relative_path="exact.bin",
            limit_bytes=50,
        )

        assert content == payload
        assert len(content) == 50

    @pytest.mark.unit
    def test_listing_skips_file_that_disappears_during_resolve(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        vanished = artifact_dir / "vanished.txt"
        vanished.write_text("gone\n", encoding="utf-8")
        original_resolve = Path.resolve

        def resolve_candidate(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == vanished:
                raise FileNotFoundError(str(self))
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_candidate)

        assert list_artifacts("ws_artifacts", artifact_dir) == []

    @pytest.mark.unit
    def test_get_workspace_artifact_content_rejects_hard_link(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        outside = tmp_path / "outsideroot.txt"
        outside.write_text("secret\n", encoding="utf-8")
        report = artifact_dir / "report.txt"
        report.hardlink_to(outside)

        with pytest.raises(ArtifactNotFoundError):
            get_workspace_artifact_content(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="report.txt",
                limit_bytes=MAX_ARTIFACT_CONTENT_BYTES,
            )

    @pytest.mark.unit
    def test_listing_keeps_regular_single_link_file(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        report = artifact_dir / "report.txt"
        report.write_text("report\n", encoding="utf-8")

        assert [item.relative_path for item in list_artifacts("ws_artifacts", artifact_dir)] == [
            "report.txt",
        ]

    @pytest.mark.unit
    def test_deleted_between_stat_and_open_raises_artifact_not_found(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        report = artifact_dir / "report.txt"
        report.write_text("report\n", encoding="utf-8")
        report_resolved = report.resolve()
        original_open = Path.open

        def open_candidate(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self.resolve() == report_resolved:
                raise FileNotFoundError(str(self))
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_candidate)

        with pytest.raises(ArtifactNotFoundError):
            get_workspace_artifact_content(
                workspace_id="ws_artifacts",
                artifact_dir=artifact_dir,
                relative_path="report.txt",
                limit_bytes=1024,
            )
