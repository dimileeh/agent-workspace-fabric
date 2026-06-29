"""Orphan AWF Docker resource and worktree detection."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
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
    empty_worktree_scan,
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
    def _run(args: list[str], **_kwargs: object) -> _Completed:
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


def test_reaper_uses_git_aware_remover_for_git_managed_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_reaper_reports_git_aware_skipped_worktree_without_direct_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    async def _git_aware_remover(
        *, workspace_id: str, path: Path, work_dir: Path
    ) -> WorkspaceGCWorktreeRemoveResult:
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

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
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
    assert remover_calls == [("ws_dead", worktree)]
    assert len(result.errors) == 1
    assert result.errors[0].kind == "worktree"
    assert result.errors[0].workspace_id == "ws_dead"
    assert result.errors[0].status == "failed"
    assert result.errors[0].reason_code == "WORKTREE_NOT_GIT_MANAGED"
    assert worktree.exists()


@pytest.mark.unit
def test_reaper_uses_scanned_companion_worktree_id_for_git_aware_remover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        raise AssertionError(f"git-aware removal used for symlinked worktree: {path}")

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
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
def test_reaper_reports_git_aware_worktree_remover_failure_without_direct_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    linked_git_dir = tmp_path / "git" / "mirrors" / "repo.git" / "worktrees" / "ws_dead"
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
def test_reaper_reports_damaged_mirror_registry_without_direct_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    linked_git_dir = tmp_path / "git" / "mirrors" / "repo.git" / "worktrees" / "ws_dead"
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "gitdir").write_text("\n", encoding="utf-8")
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
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
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    assert result.reaped == ()
    assert len(result.errors) == 1
    assert result.errors[0].kind == "worktree"
    assert result.errors[0].workspace_id == "ws_dead"
    assert result.errors[0].reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    assert "empty linked-worktree gitdir back-reference" in (result.errors[0].error or "")
    assert worktree.exists()


@pytest.mark.unit
def test_reaper_reports_unscannable_mirror_registry_without_direct_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        if path == mirrors_dir:
            raise PermissionError("permission denied")
        return original_iterdir(path)

    def _direct_delete_forbidden(
        kind: str, path: Path, *, work_dir: Path
    ) -> tuple[bool, str | None, str | None]:
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


@pytest.mark.unit
def test_reaper_flag_off_is_dry_run_and_noop(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _orphan_summary_with_compose_and_worktree(tmp_path, auto_cleanup_orphans=False)
    assert summary.cleanup_readiness.dry_run_only is True

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=False,
        )
    )

    assert result.enabled is False
    assert result.status == "disabled"
    assert teardown.calls == []
    assert (tmp_path / "git" / "worktrees" / "ws_dead").exists()


@pytest.mark.unit
def test_reaper_flag_on_reaps_compose_and_worktree(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _orphan_summary_with_compose_and_worktree(tmp_path, auto_cleanup_orphans=True)
    assert summary.cleanup_readiness.dry_run_only is False

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate reap mechanics from the row-less age guard
        )
    )

    assert result.status == "ok"
    assert len(teardown.calls) == 1
    assert teardown.calls[0][0] == "awf_ws_dead"
    assert teardown.calls[0][2] == "ws_dead"
    assert not (tmp_path / "git" / "worktrees" / "ws_dead").exists()
    reaped_kinds = sorted(outcome.kind for outcome in result.reaped)
    assert reaped_kinds == ["compose", "worktree"]
    payload = result.to_dict()
    assert payload["enabled"] is True
    assert payload["status"] == "ok"
    assert {entry["kind"] for entry in payload["reaped"]} == {"compose", "worktree"}


@pytest.mark.unit
def test_reaper_flag_on_reaps_terminal_volume_and_worktree(
    tmp_path: Path,
) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            volumes=_jsonl(
                {
                    "name": "awf_ws_dead_pgdata",
                    "project": "awf_ws_dead",
                    "driver": "local",
                    "scope": "local",
                }
            )
        ),
    )
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_dead"}),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    assert {record.kind: record.classification for record in summary.records} == {
        "volume": "terminal",
        "worktree": "terminal",
    }

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=168.0,
        )
    )

    assert result.status == "ok"
    assert teardown.remove_volumes_calls == [True]
    assert not worktree.exists()
    assert sorted(outcome.kind for outcome in result.reaped) == ["compose", "worktree"]


@pytest.mark.unit
def test_reaper_leaves_expected_and_unknown_records(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_live").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(active={"ws_live"}),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert result.reaped == ()
    assert teardown.calls == []
    assert (tmp_path / "git" / "worktrees" / "ws_live").exists()


@pytest.mark.unit
def test_reaper_skips_when_classification_unknown(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=False,
        ),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "skipped"
    assert result.reason_code == "ORPHAN_REAP_SKIPPED_UNKNOWN"
    assert teardown.calls == []


@pytest.mark.unit
def test_reaper_skips_when_scanner_unavailable_with_partial_orphans(tmp_path: Path) -> None:
    """A scanner failure that still surfaces partial orphans must not drive reaping.

    When the container list succeeds but the network/volume scan fails,
    ``scan_docker_resources`` returns ``ok=False`` while still carrying the
    listed containers. Those containers would classify as orphans, but the
    inventory is incomplete, so the summary reports the degraded scan as
    ``unavailable``/report-only (the scanner-unavailable branch runs before the
    orphan-present branch) instead of advertising reaping for an inventory the
    reaper will not act on. The reaper then skips on the same incomplete
    inventory rather than tearing down stacks on guesswork.
    """
    from awf.service.orphan_resources import reap_classified_orphans

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
            ),
            fail_networks=True,
        ),
    )
    assert docker.ok is False
    assert docker.resources  # partial inventory: the container survived the failed scan
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> the listed container is a "missing" orphan
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    # The degraded scan is reported as report-only/unknown -- not blocked with
    # reaping advertised -- so operators are never told deletion is enabled for
    # an incomplete inventory.
    assert summary.status == "unavailable"
    assert summary.orphan_count == 0
    assert summary.cleanup_readiness.status == "unknown"
    assert summary.cleanup_readiness.dry_run_only is True

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,
        )
    )

    assert result.status == "skipped"
    assert result.reason_code == "ORPHAN_REAP_SKIPPED_UNKNOWN"
    assert teardown.calls == []


@pytest.mark.unit
def test_reaper_permission_denied_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from awf.service.gc_classify import PATH_DELETE_PERMISSION_DENIED
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    def _denied(kind: str, path: Path, *, work_dir: Path) -> tuple[bool, str | None, str | None]:
        return False, "permission denied", PATH_DELETE_PERMISSION_DENIED

    monkeypatch.setattr("awf.service.orphan_resources.build_and_delete_gc_path", _denied)

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the permission-refusal path from the age guard
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    assert len(result.errors) == 1
    assert result.errors[0].reason_code == PATH_DELETE_PERMISSION_DENIED


@pytest.mark.unit
def test_reaper_worktree_already_removed_is_idempotent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awf.service.gc_classify import PATH_ALREADY_REMOVED
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    def _vanished(kind: str, path: Path, *, work_dir: Path) -> tuple[bool, str | None, str | None]:
        return False, None, PATH_ALREADY_REMOVED

    monkeypatch.setattr("awf.service.orphan_resources.build_and_delete_gc_path", _vanished)

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the idempotent-removal path from the age guard
        )
    )

    assert result.status == "ok"
    assert len(result.reaped) == 1
    assert result.reaped[0].status == "already_removed"


@pytest.mark.unit
def test_build_orphan_compose_teardown_invokes_manager() -> None:
    from awf.node.compose_manager import ComposeTeardownResult
    from awf.service.orphan_resources import build_orphan_compose_teardown

    class _FakeManager:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def teardown_project(
            self,
            *,
            project_name: str,
            compose_file: Path,
            workspace_id: str,
            remove_volumes: bool = True,
            fallback_volume_names: tuple[str, ...] = (),
        ) -> ComposeTeardownResult:
            self.calls.append(
                {
                    "project_name": project_name,
                    "compose_file": compose_file,
                    "workspace_id": workspace_id,
                    "remove_volumes": remove_volumes,
                    "fallback_volume_names": fallback_volume_names,
                }
            )
            return ComposeTeardownResult(
                status="succeeded", reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED"
            )

    manager = _FakeManager()
    teardown = build_orphan_compose_teardown(manager)  # type: ignore[arg-type]
    result = asyncio.run(
        teardown(
            "awf_ws_x",
            Path("/tmp/awf/compose/ws_x/compose.yml"),
            "ws_x",
            True,
            fallback_volume_names=("awf-ws_x-postgres_data",),
        )
    )

    assert result.ok
    assert manager.calls[0]["remove_volumes"] is True
    assert manager.calls[0]["project_name"] == "awf_ws_x"
    # The closure forwards the recovered label-less volume names verbatim so the
    # label-scoped teardown can remove them by name (#637, PRRT_kwDOSJAM6s6LCiLk).
    assert manager.calls[0]["fallback_volume_names"] == ("awf-ws_x-postgres_data",)

    # The closure forwards the caller's per-workspace decision verbatim: a
    # retained-terminal stack is torn down without deleting its salvage volumes.
    asyncio.run(teardown("awf_ws_y", Path("/tmp/awf/compose/ws_y/compose.yml"), "ws_y", False))
    assert manager.calls[1]["remove_volumes"] is False
    # Default forwards an empty tuple when no label-less names were recovered.
    assert manager.calls[1]["fallback_volume_names"] == ()


def _retained_terminal_runtime_summary(*, retained: bool) -> Any:
    """Summary for a terminal workspace with a leaked container plus a volume.

    When ``retained`` the workspace is still inside its retention window, so
    _classify keeps the volume ``expected`` (salvage evidence) while surfacing
    the live container as ``terminal``. When not retained, both the container
    and the volume classify ``terminal``.
    """

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_done-agent-1",
                    "project": "awf_ws_done",
                    "service": "agent",
                    "state": "running",
                    "status": "Up",
                }
            ),
            volumes=_jsonl(
                {
                    "name": "awf_ws_done_pgdata",
                    "project": "awf_ws_done",
                    "driver": "local",
                    "scope": "local",
                }
            ),
        ),
    )
    view = _ok_view(terminal={"ws_done"}, retained={"ws_done"} if retained else None)
    return build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=empty_worktree_scan(),
        workspace_view=view,
        auto_cleanup_orphans=True,
        reaper_available=True,
    )


@pytest.mark.unit
def test_reaper_preserves_retained_terminal_salvage_volumes(tmp_path: Path) -> None:
    """Reaping a leaked container for a within-retention terminal workspace must
    not pass ``--volumes`` and delete the salvage volume _classify protected."""
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _retained_terminal_runtime_summary(retained=True)
    # The volume is preserved as salvage evidence; only the live container reaps.
    volume_record = next(record for record in summary.records if record.kind == "volume")
    assert volume_record.classification == "expected"
    assert volume_record.reason == "WORKSPACE_TERMINAL_WITHIN_RETENTION"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert teardown.calls == [
        ("awf_ws_done", tmp_path / "compose" / "ws_done" / "compose.yml", "ws_done")
    ]
    assert teardown.remove_volumes_calls == [False]


@pytest.mark.unit
def test_reaper_removes_volumes_for_fully_terminal_workspace(tmp_path: Path) -> None:
    """A terminal workspace past its retention window has its volume classified
    ``terminal``, so the stack is torn down with ``--volumes``."""
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _retained_terminal_runtime_summary(retained=False)
    volume_record = next(record for record in summary.records if record.kind == "volume")
    assert volume_record.classification == "terminal"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert teardown.remove_volumes_calls == [True]


@pytest.mark.unit
def test_reaper_row_less_only_skips_terminal_db_record_resources(tmp_path: Path) -> None:
    """``row_less_only=True`` reaps only no-DB-record orphans, leaving terminal rows.

    The on-demand ``awf service gc`` sweep forces this so it can never tear down a terminal
    workspace the operator scoped out via ``--status``/``--exclude-status``
    (PRRT_kwDOSJAM6s6LB30p): a terminal DB-record stack is left for the scope-honouring
    DB-row-driven terminal reaper, while the row-less worktree (no status to scope on) is
    still reclaimed.
    """
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_done-agent-1",
                    "project": "awf_ws_done",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            )
        ),
    )
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        # ws_done carries a terminal row (-> "terminal"); ws_dead has no row (-> "missing").
        workspace_view=_ok_view(terminal={"ws_done"}),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    container_record = next(record for record in summary.records if record.kind == "container")
    assert container_record.classification == "terminal"
    worktree_record = next(record for record in summary.records if record.kind == "worktree")
    assert worktree_record.classification == "missing"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the row-less filter from the age guard
            row_less_only=True,
        )
    )

    assert result.status == "ok"
    # The terminal-row stack is left for the scope-honouring DB-row-driven reaper.
    assert teardown.calls == []
    # Only the row-less worktree is reclaimed.
    assert [outcome.kind for outcome in result.reaped] == ["worktree"]
    assert not (tmp_path / "git" / "worktrees" / "ws_dead").exists()


@pytest.mark.unit
def test_superseded_workspace_db_row_is_protected_from_row_less_sweep(tmp_path: Path) -> None:
    """A ``superseded`` workspace still has a DB row, so the orphan view must classify
    its resources as ``terminal`` (DB-row-driven reaper territory, under the
    failed/superseded preservation cap) — never ``missing``.

    ``superseded`` is a string-only terminal status that terminal GC and the CLI treat
    as eligible (``gc_classify.TERMINAL_WORKSPACE_GC_STATUSES``, ``--exclude-status
    superseded``). If the orphan view omitted it, the row would be filtered out of the
    id view, its container/network/volume/worktree would classify as ``missing``, and
    the on-demand ``row_less_only=True`` sweep would tear it down despite the operator
    scoping it out — exactly the hazard the row-less restriction exists to avoid
    (PRRT_kwDOSJAM6s6LC5a-).
    """
    from awf.service.orphan_resources import (
        KNOWN_WORKSPACE_STATUSES,
        reap_classified_orphans,
    )
    from awf.service.orphan_resources import (
        _workspace_view_from_rows as workspace_view_from_rows,
    )

    # The id-view query selects superseded rows and partitions them as terminal.
    assert "superseded" in KNOWN_WORKSPACE_STATUSES
    view = workspace_view_from_rows(
        [("ws_sup", "superseded", datetime(2020, 1, 1, tzinfo=UTC))],
        now=datetime(2026, 1, 1, tzinfo=UTC),
        min_retention_hours=24.0,
    )
    assert "ws_sup" in view.terminal_ids
    assert "ws_sup" not in view.active_ids

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_sup-agent-1",
                    "project": "awf_ws_sup",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            )
        ),
    )
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=empty_worktree_scan(),
        workspace_view=view,
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    container_record = next(record for record in summary.records if record.kind == "container")
    assert container_record.classification == "terminal"  # not "missing"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the row-less filter from the age guard
            row_less_only=True,
        )
    )

    assert result.status == "ok"
    # The superseded DB-record stack is left for the scope-honouring DB-row-driven reaper.
    assert teardown.calls == []
    assert result.reaped == ()
