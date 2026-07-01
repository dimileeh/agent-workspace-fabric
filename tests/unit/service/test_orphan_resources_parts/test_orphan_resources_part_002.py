"""Orphan AWF Docker resource and worktree detection."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from awf.service.gc_worktrees import (
    WorkspaceGCWorktreeRemoveResult,
    WorkspaceGCWorktreeRemoveTargetResult,
)
from awf.service.orphan_resources import (
    WorkspaceIdView,
    build_orphan_resource_summary,
    empty_docker_scan,
    scan_docker_resources,
    scan_managed_worktrees,
)


class _Completed:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _jsonl(*rows: dict[str, str]) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


def _ok_view(
    *,
    active: set[str] | None = None,
    terminal: set[str] | None = None,
    retained: set[str] | None = None,
) -> WorkspaceIdView:
    return WorkspaceIdView(
        active_ids=frozenset(active or set()),
        terminal_ids=frozenset(terminal or set()),
        retained_ids=frozenset(retained or set()),
        available=True,
    )


def _run_for(
    *,
    containers: str = "",
    networks: str = "",
    volumes: str = "",
    fail_networks: bool = False,
) -> Any:
    """Return a fake Docker subprocess runner for orphan-resource scan tests."""

    def _run(args: list[str], **_kwargs: object) -> _Completed:
        """Return canned Docker inventory responses for orphan-resource scans."""
        if args[:3] == ["docker", "ps", "-a"]:
            return _Completed(stdout=containers)
        if args[:3] == ["docker", "network", "ls"]:
            if fail_networks:
                return _Completed(returncode=1, stderr="network list failed")
            return _Completed(stdout=networks)
        if args[:3] == ["docker", "volume", "ls"]:
            return _Completed(stdout=volumes)
        raise AssertionError(f"unexpected subprocess call: {args}")

    return _run


class _RecordingComposeTeardown:
    """Fake compose teardown for the readiness-driven reaper tests."""

    def __init__(self, result: Any | None = None) -> None:
        self.calls: list[tuple[str, Path, str]] = []
        self.remove_volumes_calls: list[bool] = []
        self.fallback_volume_names_calls: list[tuple[str, ...]] = []
        from awf.node.compose_manager import ComposeTeardownResult

        self._result = result or ComposeTeardownResult(
            status="succeeded", reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED"
        )

    async def __call__(
        self,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        remove_volumes: bool,
        *,
        fallback_volume_names: tuple[str, ...] = (),
    ) -> Any:
        self.calls.append((project_name, compose_file, workspace_id))
        self.remove_volumes_calls.append(remove_volumes)
        self.fallback_volume_names_calls.append(fallback_volume_names)
        return self._result


def _orphan_summary_with_compose_and_worktree(tmp_path: Path, *, auto_cleanup_orphans: bool) -> Any:
    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_dead-agent-1",
                    "project": "awf_ws_dead",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            )
        ),
    )
    return build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> both records are "missing" orphans
        auto_cleanup_orphans=auto_cleanup_orphans,
        reaper_available=auto_cleanup_orphans,
    )


@pytest.mark.unit
def test_reaper_uses_git_aware_remover_for_git_managed_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper uses git aware remover for git managed worktree."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../mirrors/repo.git/worktrees/ws_dead\n")
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    calls: list[tuple[str, Path]] = []

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        calls.append((workspace_id, path))
        assert work_dir == tmp_path.resolve()
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=workspace_id,
                    status="succeeded",
                    reason_code="WORKTREE_REMOVE_SUCCEEDED",
                ),
            ),
        )

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete forbidden."""
        raise AssertionError(f"direct filesystem delete used for {kind}: {path}")

    monkeypatch.setattr(
        "awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete_forbidden
    )

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "ok"
    assert calls == [("ws_dead", worktree)]
    assert [outcome.to_dict() for outcome in result.reaped] == [
        {
            "kind": "worktree",
            "workspace_id": "ws_dead",
            "status": "reaped",
            "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
        }
    ]


@pytest.mark.unit
def test_reaper_falls_back_to_direct_delete_when_git_remover_skips_not_git_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper falls back to direct delete when git remover skips not git managed."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../mirrors/repo.git/worktrees/ws_dead\n")
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    remover_calls: list[tuple[str, Path]] = []
    direct_delete_calls: list[tuple[str, Path, Path]] = []

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        remover_calls.append((workspace_id, path))
        assert work_dir == tmp_path.resolve()
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code="WORKTREE_NOT_GIT_MANAGED",
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=workspace_id,
                    status="skipped",
                    reason_code="WORKTREE_NOT_GIT_MANAGED",
                ),
            ),
        )

    def _direct_delete(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete."""
        direct_delete_calls.append((kind, path, work_dir))
        return True, None, "PATH_DELETED"

    monkeypatch.setattr("awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete)

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "ok"
    assert result.errors == ()
    assert remover_calls == [("ws_dead", worktree)]
    assert direct_delete_calls == [("worktree", worktree, tmp_path.resolve())]
    assert [outcome.to_dict() for outcome in result.reaped] == [
        {
            "kind": "worktree",
            "workspace_id": "ws_dead",
            "status": "reaped",
            "reason_code": "PATH_DELETED",
        }
    ]


@pytest.mark.unit
def test_reaper_uses_git_aware_remover_for_stale_linked_mirror_gitfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper uses git aware remover for stale linked mirror gitfile."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    linked_git_dir = tmp_path / "git" / "mirrors" / "repo.git" / "worktrees" / "ws_dead"
    linked_git_dir.mkdir(parents=True)
    (worktree / ".git").write_text(
        "gitdir: ../../mirrors/repo.git/worktrees/ws_dead\n",
        encoding="utf-8",
    )
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    calls: list[tuple[str, Path]] = []
    direct_delete_calls: list[tuple[str, Path, Path]] = []

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        calls.append((workspace_id, path))
        assert work_dir == tmp_path.resolve()
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code="WORKTREE_NOT_GIT_MANAGED",
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=workspace_id,
                    status="skipped",
                    reason_code="WORKTREE_NOT_GIT_MANAGED",
                ),
            ),
        )

    def _direct_delete(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete."""
        direct_delete_calls.append((kind, path, work_dir))
        return True, None, "PATH_DELETED"

    monkeypatch.setattr("awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete)

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "ok"
    assert result.errors == ()
    assert calls == [("ws_dead", worktree)]
    assert direct_delete_calls == [("worktree", worktree, tmp_path.resolve())]
    assert [outcome.to_dict() for outcome in result.reaped] == [
        {
            "kind": "worktree",
            "workspace_id": "ws_dead",
            "status": "reaped",
            "reason_code": "PATH_DELETED",
        }
    ]


@pytest.mark.unit
def test_reaper_uses_direct_delete_for_unmanaged_standalone_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper uses direct delete for unmanaged standalone git dir."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".git" / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n",
        encoding="utf-8",
    )
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    direct_delete_calls: list[tuple[str, Path, Path]] = []

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        raise AssertionError(f"git-aware remover used for unmanaged worktree {workspace_id}")

    def _direct_delete(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete."""
        direct_delete_calls.append((kind, path, work_dir))
        return True, None, "PATH_DELETED"

    monkeypatch.setattr("awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete)

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "ok"
    assert result.errors == ()
    assert direct_delete_calls == [("worktree", worktree, tmp_path.resolve())]
    assert [outcome.to_dict() for outcome in result.reaped] == [
        {
            "kind": "worktree",
            "workspace_id": "ws_dead",
            "status": "reaped",
            "reason_code": "PATH_DELETED",
        }
    ]


@pytest.mark.unit
def test_reaper_uses_scanned_companion_worktree_id_for_git_aware_remover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper uses scanned companion worktree id for git aware remover."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_parent__companion__backend"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        "gitdir: ../mirrors/backend.git/worktrees/ws_parent__companion__backend\n",
        encoding="utf-8",
    )
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    calls: list[tuple[str, Path]] = []

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        calls.append((workspace_id, path))
        assert work_dir == tmp_path.resolve()
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=workspace_id,
                    status="succeeded",
                    reason_code="WORKTREE_REMOVE_SUCCEEDED",
                ),
            ),
        )

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete forbidden."""
        raise AssertionError(f"direct filesystem delete used for {kind}: {path}")

    monkeypatch.setattr(
        "awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete_forbidden
    )

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "ok"
    assert calls == [("ws_parent__companion__backend", worktree)]
    assert [outcome.to_dict() for outcome in result.reaped] == [
        {
            "kind": "worktree",
            "workspace_id": "ws_parent",
            "status": "reaped",
            "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
        }
    ]


@pytest.mark.unit
def test_reaper_refuses_symlinked_worktree_before_git_remover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper refuses symlinked worktree before git remover."""
    from awf.service.gc_classify import PATH_DELETE_FAILED
    from awf.service.orphan_resources import reap_classified_orphans

    target = tmp_path / "outside-checkout"
    target.mkdir()
    (target / ".git").write_text("gitdir: ../mirrors/repo.git/worktrees/ws_dead\n")
    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.parent.mkdir(parents=True)
    worktree.symlink_to(target, target_is_directory=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        raise AssertionError(f"git-aware removal used for symlinked worktree: {path}")

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete forbidden."""
        raise AssertionError(f"direct filesystem delete used for symlinked {kind}: {path}")

    monkeypatch.setattr(
        "awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete_forbidden
    )

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    assert result.reaped == ()
    assert len(result.errors) == 1
    assert result.errors[0].kind == "worktree"
    assert result.errors[0].workspace_id == "ws_dead"
    assert result.errors[0].reason_code == PATH_DELETE_FAILED
    assert result.errors[0].error == "refusing to remove symlinked worktree"
    assert worktree.is_symlink()
    assert target.exists()


@pytest.mark.unit
def test_reaper_reports_worktree_probe_os_error_as_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper reports worktree probe os error as partial failure."""
    from awf.service.gc_classify import PATH_DELETE_FAILED
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../mirrors/repo.git/worktrees/ws_dead\n")
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    def _probe_fails(path: Path, *, work_dir: Path | None = None, **kwargs: object) -> bool:
        """Test helper for probe fails."""
        del path, work_dir, kwargs
        raise OSError("bad gitdir")

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        raise AssertionError(f"git-aware removal used after failed probe: {path}")

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete forbidden."""
        raise AssertionError(f"direct filesystem delete used after failed probe: {kind} {path}")

    monkeypatch.setattr("awf.service.orphan_resources.is_existing_non_git_worktree", _probe_fails)
    monkeypatch.setattr(
        "awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete_forbidden
    )

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    assert result.reaped == ()
    assert len(result.errors) == 1
    assert result.errors[0].kind == "worktree"
    assert result.errors[0].workspace_id == "ws_dead"
    assert result.errors[0].status == "failed"
    assert result.errors[0].reason_code == PATH_DELETE_FAILED
    assert result.errors[0].error == "bad gitdir"
    assert worktree.exists()


@pytest.mark.unit
def test_reaper_uses_direct_delete_for_plain_worktree_with_malformed_mirror_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper uses direct delete for plain worktree with malformed mirror registry."""
    from awf.service.orphan_resources import reap_classified_orphans

    workspace_id = "ws_dead"
    worktree = tmp_path / "git" / "worktrees" / workspace_id
    malformed_mirror = tmp_path / "git" / "mirrors" / "malformed.git"
    malformed_git_dir = malformed_mirror / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    malformed_git_dir.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", str(malformed_mirror)], check=True, capture_output=True
    )
    (malformed_git_dir / "gitdir").write_text("", encoding="utf-8")
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    direct_delete_calls: list[tuple[str, Path, Path]] = []

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        raise AssertionError(f"git-aware removal used for unresolved mirror registry: {path}")

    def _direct_delete(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete."""
        direct_delete_calls.append((kind, path, work_dir))
        return True, None, "PATH_DELETED"

    monkeypatch.setattr("awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete)

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "ok"
    assert result.errors == ()
    assert direct_delete_calls == [("worktree", worktree, tmp_path.resolve())]
    assert [outcome.to_dict() for outcome in result.reaped] == [
        {
            "kind": "worktree",
            "workspace_id": workspace_id,
            "status": "reaped",
            "reason_code": "PATH_DELETED",
        }
    ]


@pytest.mark.unit
def test_reaper_uses_direct_delete_for_plain_worktree_with_damaged_mirror_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper uses direct delete for plain worktree with damaged mirror registry."""
    from awf.service.orphan_resources import reap_classified_orphans

    workspace_id = "ws_dead"
    worktree = tmp_path / "git" / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    linked_git_dir = tmp_path / "git" / "mirrors" / "repo.git" / "worktrees" / workspace_id
    linked_git_dir.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", str(linked_git_dir.parent.parent)],
        check=True,
        capture_output=True,
    )
    (linked_git_dir / "gitdir").write_text("\n", encoding="utf-8")
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    direct_delete_calls: list[tuple[str, Path, Path]] = []

    def _direct_delete(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete."""
        direct_delete_calls.append((kind, path, work_dir))
        return True, None, "PATH_DELETED"

    monkeypatch.setattr("awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete)

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
        )
    )

    assert result.status == "ok"
    assert result.errors == ()
    assert direct_delete_calls == [("worktree", worktree, tmp_path.resolve())]
    assert [outcome.to_dict() for outcome in result.reaped] == [
        {
            "kind": "worktree",
            "workspace_id": workspace_id,
            "status": "reaped",
            "reason_code": "PATH_DELETED",
        }
    ]


@pytest.mark.unit
def test_reaper_reports_git_aware_worktree_remover_failure_without_direct_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper reports git aware worktree remover failure without direct fallback."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../mirrors/repo.git/worktrees/ws_dead\n")
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        del path, work_dir
        return WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
            error="mirror lock removal failed",
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=workspace_id,
                    status="failed",
                    reason_code="GIT_WORKTREE_REMOVE_FAILED",
                    error="mirror lock removal failed",
                ),
            ),
        )

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete forbidden."""
        raise AssertionError(f"direct filesystem delete used for {kind}: {path}")

    monkeypatch.setattr(
        "awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete_forbidden
    )

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    assert result.reaped == ()
    assert len(result.errors) == 1
    assert result.errors[0].reason_code == "GIT_WORKTREE_REMOVE_FAILED"
    assert result.errors[0].error == "mirror lock removal failed"
    assert worktree.exists()


@pytest.mark.unit
def test_reaper_uses_git_aware_remover_for_worktree_missing_gitfile_with_mirror_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper uses git aware remover for worktree missing gitfile with mirror registry."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    mirror = tmp_path / "git" / "mirrors" / "repo.git"
    subprocess.run(["git", "init", "--bare", str(mirror)], check=True, capture_output=True)
    linked_git_dir = mirror / "worktrees" / "ws_dead"
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "gitdir").write_text(str(worktree / ".git"), encoding="utf-8")
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    calls: list[tuple[str, Path]] = []

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
        """Test helper for git aware remover."""
        calls.append((workspace_id, path))
        assert work_dir == tmp_path.resolve()
        return WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code="ORPHAN_WORKTREE_REPO_URL_UNRESOLVED",
            error="repo URL missing",
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=workspace_id,
                    status="failed",
                    reason_code="ORPHAN_WORKTREE_REPO_URL_UNRESOLVED",
                    error="repo URL missing",
                ),
            ),
        )

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete forbidden."""
        raise AssertionError(f"direct filesystem delete used for {kind}: {path}")

    monkeypatch.setattr(
        "awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete_forbidden
    )

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
            worktree_remover=_git_aware_remover,
        )
    )

    assert result.status == "partial"
    assert calls == [("ws_dead", worktree)]
    assert result.errors[0].reason_code == "ORPHAN_WORKTREE_REPO_URL_UNRESOLVED"
    assert worktree.exists()


@pytest.mark.unit
def test_reaper_reports_unscannable_mirror_registry_without_direct_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reaper reports unscannable mirror registry without direct delete."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    mirrors_dir = tmp_path / "git" / "mirrors"
    mirrors_dir.mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    original_iterdir = Path.iterdir

    def _raise_for_mirrors_dir(path: Path):
        """Test helper for raise for mirrors dir."""
        if path == mirrors_dir:
            raise PermissionError("permission denied")
        return original_iterdir(path)

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
        """Test helper for direct delete forbidden."""
        raise AssertionError(f"direct filesystem delete used for {kind}: {path}")

    monkeypatch.setattr(Path, "iterdir", _raise_for_mirrors_dir)
    monkeypatch.setattr(
        "awf.service.orphan_resources.build_and_delete_gc_path", _direct_delete_forbidden
    )

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            min_age_hours=0,
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    assert result.reaped == ()
    assert len(result.errors) == 1
    assert result.errors[0].kind == "worktree"
    assert result.errors[0].workspace_id == "ws_dead"
    assert result.errors[0].reason_code == "MIRROR_REGISTRY_SCAN_FAILED"
    assert "permission denied" in (result.errors[0].error or "")
    assert worktree.exists()
