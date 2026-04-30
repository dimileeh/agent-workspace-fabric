"""Artifact filesystem service tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.service.artifacts import (
    ArtifactNotFoundError,
    ArtifactPathError,
    _artifact_id,
    _artifact_kind,
    _is_symlink,
    _resolve_artifact_root,
    artifact_id,
    artifact_kind,
    get_downloadable_artifact,
    list_artifacts,
    workspace_artifact_dir,
)


class TestArtifactService:
    @pytest.mark.unit
    def test_workspace_artifact_dir_uses_managed_work_dir_root(self, tmp_path: Path) -> None:
        assert workspace_artifact_dir(tmp_path / "awf-state", "ws_123") == (
            tmp_path / "awf-state" / "artifacts" / "ws_123"
        )

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
    def test_listing_returns_empty_when_directory_walk_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        artifact_dir = tmp_path / "artifacts" / "ws_artifacts"
        artifact_dir.mkdir(parents=True)
        original_walk = Path.walk

        def walk_candidate(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == artifact_dir:
                raise OSError("walk failed")
            return original_walk(self, *args, **kwargs)

        monkeypatch.setattr(Path, "walk", walk_candidate)

        assert list_artifacts("ws_artifacts", artifact_dir) == []

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
        assert _artifact_kind(Path("summary.json")) == artifact_kind(
            Path("summary.json")
        )
        assert _artifact_kind(report) == artifact_kind(report) == "txt"

    @pytest.mark.unit
    def test_symlink_probe_fails_closed_when_filesystem_raises(self) -> None:
        class _UnstatablePath:
            def is_symlink(self) -> bool:
                raise OSError("cannot stat")

        assert _is_symlink(_UnstatablePath()) is True  # type: ignore[arg-type]
