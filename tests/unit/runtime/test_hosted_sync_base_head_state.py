"""Regression coverage for hosted SyncBase pushed-head monitor state."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.enums import OperationStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    SyncBase,
)
from awf.runtime.pr_monitor_runner import loop, remote_ops
from awf.runtime.pr_monitor_runner.agent_service_recovery import (
    _hosted_pr_identity_for_workspace,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_OLD_HEAD = "4ac31db6f749f18652716e9c83e24f130524205d"
_NEW_HEAD = "2bb16a374667250e685e5bc326d033039f37885d"
_PUSHED_HEAD_UNAVAILABLE = "SYNC_BASE_PUSHED_HEAD_UNAVAILABLE"


class _SuccessfulGitRunner:
    async def run(
        self,
        _args: list[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del env
        return CommandResult(returncode=0, stdout="", stderr="")


class _SyncBaseHarness:
    def __init__(
        self,
        *,
        worktrees_root: Path,
        push_result: _GitPushResult,
        resolved_head: str | None,
    ) -> None:
        self._worktrees_root = worktrees_root
        self._push_result = push_result
        self._resolved_head = resolved_head
        self.head_resolution_calls: list[Path] = []
        self._deps = SimpleNamespace(runner=_SuccessfulGitRunner())

    async def _repair_operation_start_head_result(
        self,
        **_kwargs: object,
    ) -> tuple[str, None]:
        return _OLD_HEAD, None

    async def _resolve_task_tag(self, _workspace_id: str) -> None:
        return None

    async def _fetch_base(self, **_kwargs: object) -> None:
        return None

    async def _protected_scope_push_block(self, **_kwargs: object) -> None:
        return None

    async def _post_action_pr_terminal_push_result_if_moot(
        self,
        **_kwargs: object,
    ) -> _GitPushResult | None:
        """#910 post-action re-check; this harness always models an OPEN PR."""
        return None

    async def _validated_git_push_result(self, **_kwargs: object) -> _GitPushResult:
        return self._push_result

    async def _rev_parse_head(self, worktree_path: Path) -> str | None:
        self.head_resolution_calls.append(worktree_path)
        return self._resolved_head


def _workspace_with_stale_hosted_identity() -> SimpleNamespace:
    return SimpleNamespace(
        repo_url="git@github.com:dimileeh/agent-workspace-fabric.git",
        pr_url="https://github.com/dimileeh/agent-workspace-fabric/pull/363",
        pr_number=363,
        branch_base="development",
        remote_push_branch="awf/ws_hosted",
        owned_paths=["src/awf"],
        monitor_last_commit_sha=_OLD_HEAD,
        task_policy={
            "pr_adoption": {
                "pr_number": 363,
                "base_ref": "development",
                "head_ref": "awf/ws_hosted",
                "head_sha": _OLD_HEAD,
            }
        },
    )


async def _run_sync_base(
    harness: _SyncBaseHarness,
    *,
    state: MonitorState,
) -> _GitPushResult:
    return await remote_ops._run_sync_base(
        harness,  # type: ignore[arg-type]
        workspace_id="ws_hosted",
        state=state,
        repo=SimpleNamespace(slug=lambda: "dimileeh/agent-workspace-fabric"),
        pr_number=363,
        pr_head_sha=_OLD_HEAD,
        base_branch="development",
        remote_branch="awf/ws_hosted",
        compose_project="awf_ws_hosted",
        compose_file=Path("compose.yml"),
    )


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_sync_base_push_advances_immediately_following_hosted_identity(
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_stale_hosted_identity()
    state = MonitorState(last_push_sha=_OLD_HEAD)
    harness = _SyncBaseHarness(
        worktrees_root=tmp_path,
        push_result=_GitPushResult(pushed=True, failed=False, returncode=0),
        resolved_head=_NEW_HEAD.upper(),
    )

    result = await _run_sync_base(harness, state=state)

    async def _load_workspace(_workspace_id: str) -> SimpleNamespace:
        return workspace

    identity = await _hosted_pr_identity_for_workspace(
        SimpleNamespace(_load_workspace=_load_workspace),
        "ws_hosted",
        state=state,
    )
    assert result.pushed is True
    assert state.last_push_sha == _NEW_HEAD
    assert identity["expected_head_sha"] == _NEW_HEAD
    assert workspace.monitor_last_commit_sha == _OLD_HEAD
    assert workspace.task_policy["pr_adoption"]["head_sha"] == _OLD_HEAD
    assert harness.head_resolution_calls == [tmp_path / "ws_hosted"]


@pytest.mark.unit
async def test_sync_base_pushed_head_survives_persistence_and_restart(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory, head_sha=_OLD_HEAD)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_last_commit_sha = _OLD_HEAD
        workspace.task_policy = {
            "pr_adoption": {
                "pr_number": workspace.pr_number,
                "base_ref": workspace.branch_base,
                "head_ref": workspace.remote_push_branch,
                "head_sha": _OLD_HEAD,
            }
        }
        await session.commit()

    persistence_runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "persistent-worktrees",
    )
    state = persistence_runner._load_state(await persistence_runner._load_workspace(workspace_id))
    sync_harness = _SyncBaseHarness(
        worktrees_root=tmp_path / "sync-worktrees",
        push_result=_GitPushResult(pushed=True, failed=False, returncode=0),
        resolved_head=_NEW_HEAD,
    )

    await remote_ops._run_sync_base(
        sync_harness,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        state=state,
        repo=SimpleNamespace(slug=lambda: "dimileeh/agent-workspace-fabric"),
        pr_number=363,
        pr_head_sha=_OLD_HEAD,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=Path("compose.yml"),
    )
    await persistence_runner._persist_state(workspace_id, state)

    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)
        assert persisted is not None
        assert persisted.monitor_last_commit_sha == _NEW_HEAD

    resumed_runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "resumed-worktrees",
    )
    resumed_workspace = await resumed_runner._load_workspace(workspace_id)
    resumed_state = resumed_runner._load_state(resumed_workspace)
    resumed_identity = await resumed_runner._hosted_pr_identity_for_workspace(
        workspace_id,
        state=resumed_state,
    )
    assert resumed_state.last_push_sha == _NEW_HEAD
    assert resumed_identity["expected_head_sha"] == _NEW_HEAD


@pytest.mark.parametrize(
    "push_result",
    [
        pytest.param(
            _GitPushResult(pushed=False, failed=True, returncode=1),
            id="failed",
        ),
        pytest.param(
            _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                reason_code="PROTECTED_SCOPE_REPAIR_FAILED",
                paused_into_blocked=True,
            ),
            id="protected-scope-paused",
        ),
        pytest.param(
            _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                recovered_by_resync=True,
            ),
            id="recovered-by-resync",
        ),
        pytest.param(
            _GitPushResult(pushed=False, failed=False, returncode=0),
            id="no-op",
        ),
    ],
)
@pytest.mark.unit
async def test_sync_base_non_push_outcomes_do_not_advance_head_or_resolve_head(
    push_result: _GitPushResult,
    tmp_path: Path,
) -> None:
    state = MonitorState(last_push_sha=_OLD_HEAD)
    harness = _SyncBaseHarness(
        worktrees_root=tmp_path,
        push_result=push_result,
        resolved_head=_NEW_HEAD,
    )

    result = await _run_sync_base(harness, state=state)

    assert result is push_result
    assert state.last_push_sha == _OLD_HEAD
    assert harness.head_resolution_calls == []


@pytest.mark.parametrize(
    ("resolved_head", "resolution"),
    [
        pytest.param(None, "unavailable", id="command-failed"),
        pytest.param("", "invalid", id="blank"),
        pytest.param("abc123", "invalid", id="short"),
        pytest.param("g" * 40, "invalid", id="non-hex"),
    ],
)
@pytest.mark.unit
async def test_sync_base_push_with_untrusted_local_head_fails_closed(
    resolved_head: str | None,
    resolution: str,
    tmp_path: Path,
) -> None:
    state = MonitorState(last_push_sha=_OLD_HEAD)
    harness = _SyncBaseHarness(
        worktrees_root=tmp_path,
        push_result=_GitPushResult(
            pushed=True,
            failed=False,
            returncode=0,
            stdout="push succeeded",
        ),
        resolved_head=resolved_head,
    )

    result = await _run_sync_base(harness, state=state)

    assert result.pushed is True
    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.reason_code == _PUSHED_HEAD_UNAVAILABLE
    assert state.last_push_sha == _OLD_HEAD
    assert result.failure_evidence()["phase"] == "post_sync_base_push_head_resolution"
    assert result.failure_evidence()["push_reported_success"] is True
    assert result.failure_evidence()["head_resolution"] == resolution
    assert harness.head_resolution_calls == [tmp_path / "ws_hosted"]


class _SyncBaseLoopHarness:
    def __init__(self, push_result: _GitPushResult) -> None:
        self.push_result = push_result
        self.finished: list[dict[str, object]] = []
        self.audits: list[dict[str, object]] = []
        self.terminations: list[dict[str, object]] = []
        self.progress_recorded = False
        self.staleness_refreshed = False

    async def _write_monitor_log(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _clear_workspace_attention(self, _workspace_id: str) -> None:
        return None

    async def _begin_monitor_operation(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(operation_id="op-sync-base")

    async def _run_sync_base(self, **_kwargs: object) -> _GitPushResult:
        return self.push_result

    async def _finish_monitor_operation(
        self,
        _operation: object,
        **kwargs: object,
    ) -> None:
        self.finished.append(dict(kwargs))

    async def _record_pr_monitor_audit_event(self, **kwargs: object) -> None:
        self.audits.append(dict(kwargs))

    async def _terminate_failed(self, _workspace_id: str, **kwargs: object) -> None:
        self.terminations.append(dict(kwargs))

    def _record_sync_base_progress(self, **_kwargs: object) -> None:
        self.progress_recorded = True

    async def _refresh_staleness_after_sync_base(self, **_kwargs: object) -> None:
        self.staleness_refreshed = True


def _behind_status() -> PRStatus:
    return PRStatus(
        number=363,
        head_sha=_OLD_HEAD,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=1,
        merge_state_status=MergeStateStatus.BEHIND,
    )


@pytest.mark.unit
async def test_sync_base_pushed_head_failure_preserves_structured_loop_evidence() -> None:
    details = {
        "phase": "post_sync_base_push_head_resolution",
        "push_reported_success": True,
        "head_resolution": "unavailable",
    }
    push_result = _GitPushResult(
        pushed=True,
        failed=True,
        returncode=1,
        stderr="SyncBase push succeeded but its local HEAD could not be verified",
        reason_code=_PUSHED_HEAD_UNAVAILABLE,
        details=details,
    )
    harness = _SyncBaseLoopHarness(push_result)

    terminal = await loop._execute(
        harness,  # type: ignore[arg-type]
        action=SyncBase(),
        workspace_id="ws_hosted",
        repo_url="git@github.com:dimileeh/agent-workspace-fabric.git",
        repo=SimpleNamespace(),  # type: ignore[arg-type]
        pr_number=363,
        status=_behind_status(),
        state=MonitorState(last_push_sha=_OLD_HEAD),
        base_branch="development",
        remote_branch="awf/ws_hosted",
        compose_project="awf_ws_hosted",
        compose_file=Path("compose.yml"),
        monitor_log=None,
    )

    assert terminal is True
    assert harness.finished[0]["status"] is OperationStatus.failed
    assert harness.finished[0]["result"] == {
        "status": "failed",
        "outcome": "sync_base_pushed_head_unavailable",
        "reason_code": _PUSHED_HEAD_UNAVAILABLE,
        "pushed": True,
    }
    assert harness.audits[0]["evidence"] == push_result.failure_evidence()
    assert harness.terminations[0]["details"] == push_result.failure_evidence()
    assert harness.progress_recorded is False
    assert harness.staleness_refreshed is False
