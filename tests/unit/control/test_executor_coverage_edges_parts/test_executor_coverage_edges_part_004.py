"""Focused branch-coverage tests for executor git helper behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import FakeCommandRunner
from awf.control.executor import git_methods as executor_git_methods
from awf.control.executor import git_ops as executor_git_ops
from awf.control.executor import quality_methods as executor_quality_methods
from awf.control.executor.git_ops import (
    _agent_git_writability_preflight_script,
    _git_error_indicates_missing_head_object,
    _GitObjectRecoveryResult,
    _read_ref_sha,
    _recover_missing_head_from_filesystem,
)
from awf.db.enums import WorkspaceStatus
from tests.unit.control.test_executor_coverage_edges_parts.test_executor_coverage_edges_part_003 import (
    _executor_with_runner,
)


def _fake_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    mirror = tmp_path / "mirror.git"
    linked_git_dir = mirror / "worktrees" / "ws_missing_head"
    linked_git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    return mirror, worktree


@pytest.mark.unit
def test_git_error_indicates_missing_head_object() -> None:
    assert _git_error_indicates_missing_head_object("fatal: bad object HEAD\n")
    assert _git_error_indicates_missing_head_object("fatal: not a valid object name HEAD\n")
    assert not _git_error_indicates_missing_head_object("fatal: not a git repository\n")


@pytest.mark.unit
def test_agent_git_writability_preflight_script_exercises_object_and_ref_writes() -> None:
    script = _agent_git_writability_preflight_script("ws_preflight")

    assert "git status --porcelain" in script
    assert "git hash-object -w --stdin" in script
    assert 'git cat-file -e "$blob^{blob}"' in script
    assert 'git update-ref "$ref" HEAD' in script
    assert 'git update-ref -d "$ref"' in script


@pytest.mark.unit
async def test_agent_git_writability_preflight_runs_inside_agent_container(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / ".git").write_text("gitdir: /tmp/mirror/worktrees/ws_preflight\n")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    ok = await executor._run_agent_git_writability_preflight(
        workspace_id="ws_preflight",
        compose_project="awf_ws_preflight",
        compose_file=compose_file,
        worktree_path=worktree_path,
    )

    assert ok is True
    assert runner.calls
    call = runner.calls[0]
    assert call.input_bytes == b""
    assert call.args[:2] == ["docker", "compose"]
    assert call.args[call.args.index("-p") + 1] == "awf_ws_preflight"
    assert call.args[call.args.index("-f") + 1] == str(compose_file)
    assert "agent_git_writability_preflight" not in " ".join(call.args[:10])
    assert "git hash-object -w --stdin" in " ".join(call.args)


@pytest.mark.unit
async def test_agent_git_writability_preflight_skips_non_provisioned_fakes(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    assert await executor._run_agent_git_writability_preflight(
        workspace_id="ws_no_git",
        compose_project="awf_ws_no_git",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
    )

    (worktree_path / ".git").write_text("gitdir: /tmp/mirror/worktrees/ws_no_git\n")
    assert await executor._run_agent_git_writability_preflight(
        workspace_id="ws_no_compose",
        compose_project="awf_ws_no_compose",
        compose_file=tmp_path / "missing-compose.yml",
        worktree_path=worktree_path,
    )
    assert runner.calls == []


@pytest.mark.unit
async def test_agent_git_writability_preflight_fails_when_repair_fails(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / ".git").write_text("gitdir: /tmp/mirror/worktrees/ws_repair_fail\n")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    executor._repair_agent_git_ownership = AsyncMock(return_value=False)  # type: ignore[method-assign]
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    ok = await executor._run_agent_git_writability_preflight(
        workspace_id="ws_repair_fail",
        compose_project="awf_ws_repair_fail",
        compose_file=compose_file,
        worktree_path=worktree_path,
    )

    assert ok is False
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    assert runner.calls == []


@pytest.mark.unit
async def test_agent_git_writability_preflight_records_container_failure(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="fatal: cannot write object")
    executor = _executor_with_runner(runner, tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / ".git").write_text("gitdir: /tmp/mirror/worktrees/ws_git_fail\n")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    ok = await executor._run_agent_git_writability_preflight(
        workspace_id="ws_git_fail",
        compose_project="awf_ws_git_fail",
        compose_file=compose_file,
        worktree_path=worktree_path,
    )

    assert ok is False
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["reason_code"] == "GIT_AGENT_WRITABILITY_FAILED"
    assert kwargs["details"]["stderr"] == "fatal: cannot write object"


@pytest.mark.unit
async def test_repair_agent_git_ownership_reports_repair_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("cannot repair")

    monkeypatch.setattr(executor_git_ops, "repair_agent_writable_worktree", _raise)

    assert not await executor._repair_agent_git_ownership(
        workspace_id="ws_repair_exception",
        worktree_path=tmp_path / "worktree",
        reason="test",
    )


@pytest.mark.unit
def test_read_ref_sha_returns_none_for_missing_ref(tmp_path: Path) -> None:
    assert _read_ref_sha(tmp_path, "refs/heads/missing") is None


@pytest.mark.unit
def test_read_ref_sha_reads_packed_ref_when_loose_ref_is_missing(tmp_path: Path) -> None:
    sha = "a" * 40
    (tmp_path / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{sha} refs/heads/awf/ws_packed\n",
        encoding="utf-8",
    )

    assert _read_ref_sha(tmp_path, "refs/heads/awf/ws_packed") == sha


@pytest.mark.unit
def test_read_ref_sha_returns_none_when_packed_refs_cannot_be_read(tmp_path: Path) -> None:
    (tmp_path / "packed-refs").mkdir()

    assert _read_ref_sha(tmp_path, "refs/heads/awf/ws_missing") is None


@pytest.mark.unit
async def test_missing_head_recovery_returns_none_without_linked_mirror(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()

    result = await _recover_missing_head_from_filesystem(
        runner=runner,
        workspace_id="ws_no_mirror",
        worktree_path=tmp_path,
        base_commit="a" * 40,
        branch_name="awf/ws_no_mirror",
    )

    assert result is None
    assert runner.calls == []


@pytest.mark.unit
async def test_missing_head_recovery_recommits_filesystem_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror, worktree = _fake_linked_worktree(tmp_path)
    branch_dir = mirror / "refs" / "heads" / "awf"
    branch_dir.mkdir(parents=True)
    broken_head = "b" * 40
    (branch_dir / "ws_missing_head").write_text(f"{broken_head}\n", encoding="utf-8")
    recovered_head = "c" * 40
    runner = FakeCommandRunner()
    for returncode, stdout, stderr in [
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (1, "", ""),
        (0, "", ""),
        (0, recovered_head, ""),
    ]:
        runner.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(
        executor_git_ops,
        "repair_agent_writable_worktree",
        lambda *_args: None,
    )

    result = await _recover_missing_head_from_filesystem(
        runner=runner,
        workspace_id="ws_missing_head",
        worktree_path=worktree,
        base_commit="a" * 40,
        branch_name="awf/ws_missing_head",
    )

    assert result == _GitObjectRecoveryResult(
        broken_head_sha=broken_head,
        recovered_head_sha=recovered_head,
    )
    assert len(runner.calls) == 7


@pytest.mark.unit
async def test_missing_head_recovery_commit_prepends_task_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filesystem-recovery commit subject carries the workspace task tag."""
    mirror, worktree = _fake_linked_worktree(tmp_path)
    branch_dir = mirror / "refs" / "heads" / "awf"
    branch_dir.mkdir(parents=True)
    (branch_dir / "ws_missing_head").write_text(f"{'b' * 40}\n", encoding="utf-8")
    runner = FakeCommandRunner()
    for returncode, stdout, stderr in [
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (1, "", ""),
        (0, "", ""),
        (0, "c" * 40, ""),
    ]:
        runner.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(
        executor_git_ops,
        "repair_agent_writable_worktree",
        lambda *_args: None,
    )

    result = await _recover_missing_head_from_filesystem(
        runner=runner,
        workspace_id="ws_missing_head",
        worktree_path=worktree,
        base_commit="a" * 40,
        branch_name="awf/ws_missing_head",
        task_tag="PROJ-9",
    )

    assert result is not None
    commit_call = next(call for call in runner.calls if "commit" in call.args)
    subject = commit_call.args[commit_call.args.index("-m") + 1]
    assert subject == "PROJ-9 awf: recover ws_missing_head from missing git object"


@pytest.mark.unit
async def test_missing_head_recovery_returns_success_when_index_matches_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror, worktree = _fake_linked_worktree(tmp_path)
    branch_dir = mirror / "refs" / "heads" / "awf"
    branch_dir.mkdir(parents=True)
    broken_head = "b" * 40
    (branch_dir / "ws_missing_head").write_text(f"{broken_head}\n", encoding="utf-8")
    recovered_head = "a" * 40
    runner = FakeCommandRunner()
    for returncode, stdout, stderr in [
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, f"{recovered_head}\n", ""),
    ]:
        runner.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(
        executor_git_ops,
        "repair_agent_writable_worktree",
        lambda *_args: None,
    )

    result = await _recover_missing_head_from_filesystem(
        runner=runner,
        workspace_id="ws_missing_head",
        worktree_path=worktree,
        base_commit=recovered_head,
        branch_name="awf/ws_missing_head",
    )

    assert result == _GitObjectRecoveryResult(
        broken_head_sha=broken_head,
        recovered_head_sha=recovered_head,
    )
    assert len(runner.calls) == 6


@pytest.mark.unit
@pytest.mark.parametrize(
    ("queued", "expected_call_count"),
    [
        ([(1, "", "base missing")], 1),
        ([(0, "", ""), (1, "", "update failed")], 2),
        ([(0, "", ""), (0, "", ""), (1, "", "reset failed")], 3),
        ([(0, "", ""), (0, "", ""), (0, "", ""), (1, "", "add failed")], 4),
        ([(0, "", ""), (0, "", ""), (0, "", ""), (0, "", ""), (2, "", "diff failed")], 5),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (1, "", "commit failed"),
            ],
            6,
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (0, "", ""),
                (1, "", "head failed"),
            ],
            7,
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (0, "", ""),
                (0, "", ""),
            ],
            7,
        ),
    ],
)
async def test_missing_head_recovery_returns_none_for_each_unrecoverable_step(
    tmp_path: Path,
    queued: list[tuple[int, str, str]],
    expected_call_count: int,
) -> None:
    _mirror, worktree = _fake_linked_worktree(tmp_path)
    runner = FakeCommandRunner()
    for returncode, stdout, stderr in queued:
        runner.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)

    result = await _recover_missing_head_from_filesystem(
        runner=runner,
        workspace_id="ws_missing_head",
        worktree_path=worktree,
        base_commit="a" * 40,
        branch_name="awf/ws_missing_head",
    )

    assert result is None
    assert len(runner.calls) == expected_call_count


@pytest.mark.unit
async def test_missing_head_recovery_marks_failed_when_base_commit_is_unavailable(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    ok = await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_missing_base",
        worktree_path=tmp_path / "worktree",
        base_commit=None,
        branch_name="awf/ws_missing_base",
        from_status=WorkspaceStatus.running,
        stage="post_agent_commit",
        error=RuntimeError("bad object HEAD"),
    )

    assert ok is False
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    assert executor._mark_failed.await_args.kwargs["reason_code"] == "GIT_OBJECT_MISSING"  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_missing_head_recovery_can_return_false_without_marking_failed(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    ok = await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_missing_base_cleanup",
        worktree_path=tmp_path / "worktree",
        base_commit=None,
        branch_name="awf/ws_missing_base_cleanup",
        from_status=WorkspaceStatus.running,
        stage="agent_run_cleanup_failure",
        error=RuntimeError("bad object HEAD"),
        mark_failed_on_failure=False,
    )

    assert ok is False
    executor._mark_failed.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_missing_head_recovery_marks_failed_when_filesystem_recovery_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    async def _raise(**_kwargs: object) -> None:
        raise RuntimeError("recovery exploded")

    monkeypatch.setattr(executor_git_methods, "_recover_missing_head_from_filesystem", _raise)

    ok = await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_recovery_raises",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        branch_name="awf/ws_recovery_raises",
        from_status=WorkspaceStatus.running,
        stage="agent_run",
        error=RuntimeError("bad object HEAD"),
    )

    assert ok is False
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    assert "could not run filesystem recovery" in executor._mark_failed.await_args.kwargs["message"]  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_missing_head_recovery_marks_failed_when_filesystem_recovery_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    async def _recover(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(executor_git_methods, "_recover_missing_head_from_filesystem", _recover)

    ok = await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_recovery_none",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        branch_name="awf/ws_recovery_none",
        from_status=WorkspaceStatus.running,
        stage="post_agent_commit",
        error=RuntimeError("bad object HEAD"),
    )

    assert ok is False
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    assert "could not rebuild" in executor._mark_failed.await_args.kwargs["message"]  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_missing_head_recovery_marks_failed_when_event_recording_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    executor._record_git_object_recovery_event = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("event failed")
    )
    recovery = _GitObjectRecoveryResult(
        strategy="filesystem_recommit",
        broken_head_sha="b" * 40,
        recovered_head_sha="c" * 40,
    )

    async def _recover(**_kwargs: object) -> _GitObjectRecoveryResult:
        return recovery

    monkeypatch.setattr(executor_git_methods, "_recover_missing_head_from_filesystem", _recover)

    ok = await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_event_failure",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        branch_name="awf/ws_event_failure",
        from_status=WorkspaceStatus.running,
        stage="post_agent_commit",
        error=RuntimeError("bad object HEAD"),
    )

    assert ok is False
    executor._record_git_object_recovery_event.assert_awaited_once()  # type: ignore[attr-defined]
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    assert "could not record" in executor._mark_failed.await_args.kwargs["message"]  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_missing_head_recovery_returns_true_after_recording_recovery_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._record_git_object_recovery_event = AsyncMock()  # type: ignore[method-assign]
    recovery = _GitObjectRecoveryResult(
        strategy="filesystem_recommit",
        broken_head_sha="b" * 40,
        recovered_head_sha="c" * 40,
    )

    async def _recover(**_kwargs: object) -> _GitObjectRecoveryResult:
        return recovery

    monkeypatch.setattr(executor_git_methods, "_recover_missing_head_from_filesystem", _recover)

    assert await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_recovered",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        branch_name="awf/ws_recovered",
        from_status=WorkspaceStatus.running,
        stage="post_agent_commit",
        error=RuntimeError("bad object HEAD"),
    )
    executor._record_git_object_recovery_event.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_record_git_object_recovery_event_persists_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    recorded: list[tuple[str, str, dict[str, object]]] = []
    commits: list[bool] = []
    workspace = object()

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            commits.append(True)

    class _Repo:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == "ws_recovery_event"
            return workspace

        async def add_event(
            self,
            ws: object,
            *,
            event_type: str,
            reason_code: str,
            payload: dict[str, object],
        ) -> None:
            assert ws is workspace
            recorded.append((event_type, reason_code, payload))

    monkeypatch.setattr(executor_git_methods, "WorkspaceRepository", _Repo)
    executor._session_factory = lambda: _Session()  # type: ignore[method-assign]

    await executor._record_git_object_recovery_event(
        workspace_id="ws_recovery_event",
        stage="post_agent_commit",
        recovery=_GitObjectRecoveryResult(
            broken_head_sha="b" * 40,
            recovered_head_sha="c" * 40,
        ),
    )

    assert commits == [True]
    assert recorded == [
        (
            "workspace.git_object_missing_recovered",
            "GIT_OBJECT_MISSING_RECOVERED",
            {
                "stage": "post_agent_commit",
                "strategy": "filesystem_tree_commit",
                "broken_head_sha": "b" * 40,
                "recovered_head_sha": "c" * 40,
            },
        )
    ]


@pytest.mark.unit
async def test_committed_paths_since_raises_on_git_diff_failure(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=2, stderr="diff exploded")
    executor = _executor_with_runner(runner, tmp_path)

    with pytest.raises(RuntimeError, match="diff exploded"):
        await executor._committed_paths_since(tmp_path, "a" * 40)


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_fails_when_no_paths_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    async def _changed_paths(*_args: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr(executor_quality_methods, "committed_changed_paths_since", _changed_paths)

    assert not await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_no_paths",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    assert "recovered no committed paths" in executor._mark_failed.await_args.kwargs["message"]  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_stops_on_plan_only_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._fail_if_plan_only_paths = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def _changed_paths(*_args: object, **_kwargs: object) -> list[str]:
        return ["plans/TASK_PLAN.md"]

    monkeypatch.setattr(executor_quality_methods, "committed_changed_paths_since", _changed_paths)

    assert not await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_plan_only",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )
    executor._fail_if_plan_only_paths.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_blocks_protected_policy_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    executor.enter_blocked_for_protected_violation = AsyncMock(return_value=True)  # type: ignore[method-assign]
    executor._fail_if_plan_only_paths = AsyncMock(return_value=False)  # type: ignore[method-assign]
    executor._active_operator_grant_specs = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def _changed_paths(*_args: object, **_kwargs: object) -> list[str]:
        return ["pyproject.toml"]

    async def _diffs(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"pyproject.toml": "+fail_under = 70"}

    monkeypatch.setattr(executor_quality_methods, "committed_changed_paths_since", _changed_paths)
    monkeypatch.setattr(
        executor_quality_methods, "protected_file_diffs_for_committed_paths", _diffs
    )
    monkeypatch.setattr(
        executor_quality_methods,
        "find_protected_quality_gate_changes",
        lambda **_kwargs: ["policy-change"],
    )

    assert not await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_policy_change",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
        execution_owner_id="owner-recovery",
    )
    # A protected violation now pauses the workspace for an operator decision
    # instead of terminally failing.
    executor._mark_failed.assert_not_awaited()  # type: ignore[attr-defined]
    executor.enter_blocked_for_protected_violation.assert_awaited_once()  # type: ignore[attr-defined]
    block_kwargs = executor.enter_blocked_for_protected_violation.await_args.kwargs  # type: ignore[attr-defined]
    assert block_kwargs["from_status"] == WorkspaceStatus.running
    assert block_kwargs["resume_phase"] == "post_agent_commit_recovery_verify"
    # The block transition is owner-gated so a stale executor that lost its
    # claim cannot clobber a newer claimant (epoch-guarded CAS, mirrors the
    # primary site in execution_flow.py).
    assert block_kwargs["execution_owner_id"] == "owner-recovery"


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_fails_when_head_not_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="not ancestor")
    executor = _executor_with_runner(runner, tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    executor._fail_if_plan_only_paths = AsyncMock(return_value=False)  # type: ignore[method-assign]
    executor._active_operator_grant_specs = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def _changed_paths(*_args: object, **_kwargs: object) -> list[str]:
        return ["src/app.py"]

    async def _diffs(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {}

    monkeypatch.setattr(executor_quality_methods, "committed_changed_paths_since", _changed_paths)
    monkeypatch.setattr(
        executor_quality_methods, "protected_file_diffs_for_committed_paths", _diffs
    )
    monkeypatch.setattr(
        executor_quality_methods,
        "find_protected_quality_gate_changes",
        lambda **_kwargs: [],
    )

    assert not await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_not_descendant",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    assert "does not descend" in executor._mark_failed.await_args.kwargs["message"]  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_accepts_non_policy_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    executor._fail_if_plan_only_paths = AsyncMock(return_value=False)  # type: ignore[method-assign]
    executor._active_operator_grant_specs = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def _changed_paths(*_args: object, **_kwargs: object) -> list[str]:
        return ["src/app.py"]

    async def _diffs(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {}

    monkeypatch.setattr(executor_quality_methods, "committed_changed_paths_since", _changed_paths)
    monkeypatch.setattr(
        executor_quality_methods, "protected_file_diffs_for_committed_paths", _diffs
    )
    monkeypatch.setattr(
        executor_quality_methods,
        "find_protected_quality_gate_changes",
        lambda **_kwargs: [],
    )

    assert await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_descendant",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )
    assert runner.calls[0].args[-3:] == ["--is-ancestor", "a" * 40, "HEAD"]


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_wrapper_marks_infra_failure(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._verify_recovered_post_agent_commit = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("verification exploded")
    )
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    assert not await executor._verify_recovered_post_agent_commit_or_mark_failed(
        workspace_id="ws_verify_wrapper",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    assert "verification failed" in executor._mark_failed.await_args.kwargs["message"]  # type: ignore[attr-defined]
