"""A managed-key park's stranded edits are committed before its resume repairs.

When a managed-key park stops a review repair and the preserving dirty sink then
fails, the park still parks with the agent's edits uncommitted
(PRRT_kwDOSJAM6s618cN-). Raising the budget resumes the monitor through an
operator hint, and that cycle's repair-start guard rejected the stranded edits as
``PRE_EXISTING_DIRTY_WORKTREE``, so the resume could never continue the repair
(PRRT_kwDOSJAM6s618pY4). The park now marks the monitor state, and the resumed
hint cycle finishes the failed sink before its guard runs — committing the edits,
or failing as ``REPAIR_DIRTY_COMMIT_FAILED`` when they still will not commit.
The marker leaves the workspace row before the resumed repair's agent runs, so a
restart mid-agent cannot pass the agent's own dirt off as the park's residue
(PRRT_kwDOSJAM6s619LVg). The resume commits only the paths the park recorded,
and only while their content still matches: anything added or changed while the
workspace sat parked keeps the resume parked for a human as
``PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES``, untouched (PRRT_kwDOSJAM6s61_WVy).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
)
from awf.runtime.pr_monitor_runner import provider_park_stranded_edits, remote_repair
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
    commit_provider_park_stranded_edits,
    mark_provider_park_stranded_edits,
    record_provider_park_stranded_edits_evidence,
)
from awf.runtime.pr_monitor_runner.remote_ops import _git_push_failure_outcome, _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    ProviderManagedKeyParkedError,
    ProviderRecoveryAuthError,
    _MonitorPolicyBlockedError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_PRESERVED_HEAD = "b" * 40
_WORKSPACE_ID = "ws_park_stranded"
_REMOTE_BRANCH = "awf/ws_park_stranded"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _dirty(paths: list[str]) -> _GitPushResult:
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="Repair worktree has pre-existing uncommitted changes",
        reason_code="PRE_EXISTING_DIRTY_WORKTREE",
        details={"phase": "repair_start", "paths": paths, "pushed": False},
    )


class _FakeRunner:
    """The two runner seams the resume step drives: the dirty guard and the sink."""

    def __init__(self, guard_results: list[_GitPushResult | None], *, commits: bool) -> None:
        self.guard_results = guard_results
        self.commits = commits
        self.guard_calls: list[dict[str, object]] = []
        self.sink_calls: list[dict[str, object]] = []

    async def _pre_existing_dirty_repair_worktree_result(
        self, **kwargs: object
    ) -> _GitPushResult | None:
        self.guard_calls.append(kwargs)
        return self.guard_results.pop(0)

    async def _commit_dirty_worktree(self, **kwargs: object) -> bool:
        self.sink_calls.append(kwargs)
        return self.commits


async def _resume(
    runner: object, state: MonitorState, tmp_path: Path, *, workspace_id: str = _WORKSPACE_ID
) -> _GitPushResult | None:
    return await commit_provider_park_stranded_edits(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / _WORKSPACE_ID,
        state=state,
        compose_project="awf_ws_park_stranded",
        compose_file=tmp_path / "compose.yml",
        remote_branch=_REMOTE_BRANCH,
        remote_push_url=None,
        operation_type="operator_hint_repair",
    )


async def _parked_state(
    tmp_path: Path, paths: tuple[str, ...] = ("stranded_fix.py",)
) -> MonitorState:
    """Mark a park and record the evidence of what it left dirty."""
    state = MonitorState()
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD)
    await record_provider_park_stranded_edits_evidence(
        _FakeRunner([_dirty(list(paths))], commits=True),
        workspace_id=_WORKSPACE_ID,
        worktree_path=tmp_path / _WORKSPACE_ID,
        state=state,
    )
    return state


def _write(tmp_path: Path, path: str, content: str) -> None:
    target = tmp_path / _WORKSPACE_ID / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


@pytest.mark.unit
def test_a_stateless_park_has_nothing_to_mark() -> None:
    mark_provider_park_stranded_edits(None, _PRESERVED_HEAD)

    state = MonitorState()
    mark_provider_park_stranded_edits(state, None)
    # An unreadable preserved HEAD still marks the stranded edits.
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == ""


@pytest.mark.unit
async def test_without_a_park_marker_the_resume_never_touches_the_worktree(
    tmp_path: Path,
) -> None:
    runner = _FakeRunner([], commits=True)

    assert await _resume(runner, MonitorState(), tmp_path) is None
    assert runner.guard_calls == []
    assert runner.sink_calls == []


@pytest.mark.unit
async def test_stranded_edits_are_committed_on_the_preserved_head(tmp_path: Path) -> None:
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = await _parked_state(tmp_path)
    runner = _FakeRunner([_dirty(["stranded_fix.py"]), None], commits=True)

    assert await _resume(runner, state, tmp_path) is None

    assert len(runner.sink_calls) == 1
    sink = runner.sink_calls[0]
    assert sink["workspace_id"] == _WORKSPACE_ID
    assert "managed provider key park" in str(sink["message"])
    assert sink["operation_start_head"] == _PRESERVED_HEAD
    assert sink["protected_scope_revert_remote_branch"] == _REMOTE_BRANCH
    assert sink["state"] is state
    # The sink stages nothing but the paths the park recorded.
    assert sink["only_paths"] == ("stranded_fix.py",)
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids
    assert PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_an_already_clean_worktree_just_drops_the_marker(tmp_path: Path) -> None:
    runner = _FakeRunner([None], commits=True)
    state = await _parked_state(tmp_path)

    assert await _resume(runner, state, tmp_path) is None
    assert runner.sink_calls == []
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_a_failed_row_clear_keeps_the_marker_for_the_next_resume(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clearing memory ahead of the row would leave a later in-process resume with
    # no marker to retry, while the row kept one a restart mid-agent reloads.
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            **(ws.monitor_threads_addressed or {}),
            PROVIDER_PARK_STRANDED_EDITS_STATE_KEY: _PRESERVED_HEAD,
            PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY: "{}",
        }
        await session.commit()

    def _faulting_factory() -> AsyncSession:
        session = factory()

        async def _commit() -> None:
            raise SQLAlchemyError("connection dropped")

        monkeypatch.setattr(session, "commit", _commit)
        return session

    runner = _FakeRunner([None, None], commits=True)
    runner._deps = SimpleNamespace(session_factory=_faulting_factory)  # type: ignore[attr-defined]
    state = await _parked_state(tmp_path)

    with pytest.raises(SQLAlchemyError):
        await _resume(runner, state, tmp_path, workspace_id=workspace_id)
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _PRESERVED_HEAD

    runner._deps = SimpleNamespace(session_factory=factory)  # type: ignore[attr-defined]
    assert await _resume(runner, state, tmp_path, workspace_id=workspace_id) is None
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in (ws.monitor_threads_addressed or {})
        assert PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY not in (
            ws.monitor_threads_addressed or {}
        )


@pytest.mark.unit
async def test_a_status_failure_is_reported_as_the_guard_reports_it(tmp_path: Path) -> None:
    status_failed = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=128,
        stderr="Could not inspect repair worktree before starting the agent.",
        reason_code="REPAIR_WORKTREE_STATUS_FAILED",
    )
    runner = _FakeRunner([status_failed], commits=True)
    state = await _parked_state(tmp_path)

    assert await _resume(runner, state, tmp_path) is status_failed
    assert runner.sink_calls == []
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _PRESERVED_HEAD


@pytest.mark.unit
async def test_edits_that_still_will_not_commit_fail_as_the_sink_failure(
    tmp_path: Path,
) -> None:
    runner = _FakeRunner(
        [_dirty(["stranded_fix.py"]), _dirty(["stranded_fix.py"])],
        commits=False,
    )
    state = await _parked_state(tmp_path)

    result = await _resume(runner, state, tmp_path)

    assert result is not None
    assert result.failed is True
    assert result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert "managed provider key park" in result.stderr
    assert result.details is not None
    assert result.details["paths"] == ["stranded_fix.py"]
    assert result.details["phase"] == "provider_park_resume"
    assert result.details["preserved_head"] == _PRESERVED_HEAD
    assert len(runner.sink_calls) == 1


def _assert_left_for_a_human(
    result: _GitPushResult | None,
    runner: _FakeRunner,
    *,
    extra_paths: tuple[str, ...] = (),
    changed_paths: tuple[str, ...] = (),
    committed_paths: tuple[str, ...] = (),
    sink_calls: int = 0,
) -> None:
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES"
    # Parked for a human, never a terminal monitor failure.
    assert result.terminal_monitor_failure is False
    assert _git_push_failure_outcome(result) == "repair_start_blocked"
    assert result.details is not None
    assert result.details["phase"] == "provider_park_resume"
    assert result.details["preserved_head"] == _PRESERVED_HEAD
    assert result.details["extra_paths"] == list(extra_paths)
    assert result.details["changed_paths"] == list(changed_paths)
    assert result.details["committed_paths"] == list(committed_paths)
    for path in (*extra_paths, *changed_paths, *committed_paths):
        assert path in result.stderr
    # The operator-facing message never denies a commit the sink already made.
    assert ("AWF committed none of them" in result.stderr) is not bool(committed_paths)
    assert len(runner.sink_calls) == sink_calls


@pytest.mark.unit
async def test_an_edit_made_while_parked_is_never_committed_with_the_stranded_edits(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = await _parked_state(tmp_path)
    _write(tmp_path, "operator_notes.py", "an operator's unrelated edit\n")
    runner = _FakeRunner([_dirty(["operator_notes.py", "stranded_fix.py"])], commits=True)

    result = await _resume(runner, state, tmp_path)

    _assert_left_for_a_human(result, runner, extra_paths=("operator_notes.py",))
    # Nothing is committed or discarded: both files are exactly as they were.
    worktree = tmp_path / _WORKSPACE_ID
    assert (worktree / "operator_notes.py").read_text() == "an operator's unrelated edit\n"
    assert (worktree / "stranded_fix.py").read_text() == "the agent's fix\n"
    # The marker stays: once a human settles the extra dirt, a later resume commits.
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _PRESERVED_HEAD
    assert PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY in state.threads_addressed_ids


@pytest.mark.unit
async def test_a_stranded_file_changed_while_parked_is_left_for_a_human(tmp_path: Path) -> None:
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = await _parked_state(tmp_path)
    _write(tmp_path, "stranded_fix.py", "the agent's fix\nplus an operator's line\n")
    runner = _FakeRunner([_dirty(["stranded_fix.py"])], commits=True)

    result = await _resume(runner, state, tmp_path)

    _assert_left_for_a_human(result, runner, changed_paths=("stranded_fix.py",))
    assert (tmp_path / _WORKSPACE_ID / "stranded_fix.py").read_text() == (
        "the agent's fix\nplus an operator's line\n"
    )


@pytest.mark.unit
async def test_a_stranded_file_reverted_while_parked_blocks_the_rest(tmp_path: Path) -> None:
    # Every recorded path must still hold the content the park left, not just the
    # ones still dirty: half of the park's edits never ride in on their own.
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    _write(tmp_path, "stranded_test.py", "the agent's test\n")
    state = await _parked_state(tmp_path, ("stranded_fix.py", "stranded_test.py"))
    (tmp_path / _WORKSPACE_ID / "stranded_test.py").unlink()
    runner = _FakeRunner([_dirty(["stranded_fix.py"])], commits=True)

    result = await _resume(runner, state, tmp_path)

    _assert_left_for_a_human(result, runner, changed_paths=("stranded_test.py",))


@pytest.mark.unit
async def test_a_file_that_appears_during_the_commit_is_left_for_a_human(
    tmp_path: Path,
) -> None:
    # The writer lock only fences AWF's own writers. Dirt a human adds while the
    # sink commits is never staged with the park's paths, and it parks the resume
    # rather than failing it as a stranded commit.
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = await _parked_state(tmp_path)
    runner = _FakeRunner(
        [_dirty(["stranded_fix.py"]), _dirty(["late_notes.py"])],
        commits=True,
    )

    result = await _resume(runner, state, tmp_path)

    _assert_left_for_a_human(
        result,
        runner,
        extra_paths=("late_notes.py",),
        committed_paths=("stranded_fix.py",),
        sink_calls=1,
    )
    assert runner.sink_calls[0]["only_paths"] == ("stranded_fix.py",)
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _PRESERVED_HEAD


@pytest.mark.unit
async def test_a_file_that_appears_during_a_failed_commit_claims_no_commit(
    tmp_path: Path,
) -> None:
    # The recorded edit is still dirty after a sink that committed nothing, so the
    # message must not report it as committed alongside the late dirt.
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = await _parked_state(tmp_path)
    runner = _FakeRunner(
        [_dirty(["stranded_fix.py"]), _dirty(["late_notes.py", "stranded_fix.py"])],
        commits=False,
    )

    result = await _resume(runner, state, tmp_path)

    _assert_left_for_a_human(result, runner, extra_paths=("late_notes.py",), sink_calls=1)


@pytest.mark.unit
async def test_a_long_list_of_unexpected_paths_is_summarized(tmp_path: Path) -> None:
    state = await _parked_state(tmp_path)
    extra = [f"generated/file_{index:02d}.py" for index in range(12)]
    runner = _FakeRunner([_dirty(extra)], commits=True)

    result = await _resume(runner, state, tmp_path)

    assert result is not None
    assert result.details is not None
    # The details keep every path; the operator-facing message names the first ten.
    assert result.details["extra_paths"] == extra
    assert "generated/file_09.py" in result.stderr
    assert "generated/file_10.py" not in result.stderr
    assert "and 2 more" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("evidence", [None, "not json", "[]"])
async def test_a_park_without_evidence_commits_nothing_it_finds_dirty(
    tmp_path: Path, evidence: str | None
) -> None:
    # A restart between the marker's write and the park sink, or a park whose
    # worktree could not be read, leaves no evidence to vouch for any dirt.
    state = MonitorState()
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD)
    if evidence is not None:
        state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY] = evidence
    runner = _FakeRunner([_dirty(["stranded_fix.py"])], commits=True)

    result = await _resume(runner, state, tmp_path)

    _assert_left_for_a_human(result, runner, extra_paths=("stranded_fix.py",))


@pytest.mark.unit
async def test_dirt_the_resume_cannot_read_is_left_for_a_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = await _parked_state(tmp_path)

    def _unreadable(_worktree_path: Path, _paths: list[str]) -> dict[str, str | None]:
        raise PermissionError("stranded_fix.py")

    monkeypatch.setattr(provider_park_stranded_edits, "_path_identities", _unreadable)
    runner = _FakeRunner([_dirty(["stranded_fix.py"])], commits=True)

    result = await _resume(runner, state, tmp_path)

    _assert_left_for_a_human(result, runner, changed_paths=("stranded_fix.py",))


@pytest.mark.unit
async def test_deletions_symlinks_and_executables_the_park_left_are_committed(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "run.sh", "#!/bin/sh\n")
    (tmp_path / _WORKSPACE_ID / "run.sh").chmod(0o755)
    (tmp_path / _WORKSPACE_ID / "latest").symlink_to("run.sh")
    paths = ("deleted.py", "latest", "run.sh")
    state = await _parked_state(tmp_path, paths)
    runner = _FakeRunner([_dirty(list(paths)), None], commits=True)

    assert await _resume(runner, state, tmp_path) is None
    assert len(runner.sink_calls) == 1
    assert runner.sink_calls[0]["only_paths"] == paths


@pytest.mark.unit
@pytest.mark.parametrize("change", ["exec_bit", "symlink_target", "recreated", "directory"])
async def test_a_changed_mode_link_or_kind_is_left_for_a_human(tmp_path: Path, change: str) -> None:
    _write(tmp_path, "run.sh", "#!/bin/sh\n")
    (tmp_path / _WORKSPACE_ID / "latest").symlink_to("run.sh")
    _write(tmp_path, "nested/README.md", "a nested repository\n")
    paths = {
        "exec_bit": "run.sh",
        "symlink_target": "latest",
        "recreated": "deleted.py",
        # A directory has no content identity to vouch for, even when unchanged.
        "directory": "nested",
    }
    path = paths[change]
    state = await _parked_state(tmp_path, (path,))
    if change == "exec_bit":
        (tmp_path / _WORKSPACE_ID / "run.sh").chmod(0o755)
    elif change == "symlink_target":
        (tmp_path / _WORKSPACE_ID / "latest").unlink()
        (tmp_path / _WORKSPACE_ID / "latest").symlink_to("nested/README.md")
    elif change == "recreated":
        _write(tmp_path, "deleted.py", "restored by an operator\n")
    runner = _FakeRunner([_dirty([path])], commits=True)

    result = await _resume(runner, state, tmp_path)

    _assert_left_for_a_human(result, runner, changed_paths=(path,))


@pytest.mark.unit
async def test_the_park_records_its_own_evidence_once(tmp_path: Path) -> None:
    unmarked = MonitorState()
    runner = _FakeRunner([], commits=True)
    await record_provider_park_stranded_edits_evidence(
        runner, workspace_id=_WORKSPACE_ID, worktree_path=tmp_path / _WORKSPACE_ID, state=unmarked
    )
    # Only a marked park records evidence.
    assert runner.guard_calls == []
    assert unmarked.threads_addressed_ids == {}

    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = await _parked_state(tmp_path)
    evidence = json.loads(
        state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY]
    )
    assert list(evidence) == ["stranded_fix.py"]
    assert str(evidence["stranded_fix.py"]).startswith("file:-:")

    # A second park sink keeps the evidence of what the park itself left.
    await record_provider_park_stranded_edits_evidence(
        runner, workspace_id=_WORKSPACE_ID, worktree_path=tmp_path / _WORKSPACE_ID, state=state
    )
    assert runner.guard_calls == []
    # A new park drops the old evidence for its own sink to record.
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD)
    assert PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_a_clean_park_records_empty_evidence(tmp_path: Path) -> None:
    state = MonitorState()
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD)

    await record_provider_park_stranded_edits_evidence(
        _FakeRunner([None], commits=True),
        workspace_id=_WORKSPACE_ID,
        worktree_path=tmp_path / _WORKSPACE_ID,
        state=state,
    )

    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY] == "{}"


class _UnreadableWorktreeRunner(_FakeRunner):
    async def _pre_existing_dirty_repair_worktree_result(
        self, **kwargs: object
    ) -> _GitPushResult | None:
        raise OSError("git could not be spawned")


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["status_failed", "spawn_failed"])
async def test_an_unreadable_worktree_records_no_evidence(tmp_path: Path, failure: str) -> None:
    state = MonitorState()
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD)
    status_failed = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=128,
        reason_code="REPAIR_WORKTREE_STATUS_FAILED",
    )
    runner = (
        _FakeRunner([status_failed], commits=True)
        if failure == "status_failed"
        else _UnreadableWorktreeRunner([], commits=True)
    )

    await record_provider_park_stranded_edits_evidence(
        runner, workspace_id=_WORKSPACE_ID, worktree_path=tmp_path / _WORKSPACE_ID, state=state
    )

    assert PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY not in state.threads_addressed_ids
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _PRESERVED_HEAD


@pytest.mark.unit
@pytest.mark.parametrize("parks", [True, False])
async def test_the_park_sink_records_the_evidence_before_it_persists_the_park(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parks: bool,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = MonitorState(started_at=0.0)
    workspace = SimpleNamespace(
        status=WorkspaceStatus.monitoring_pr.value,
        monitor_started_at=datetime.now(UTC),
        repo_url="git@github.com:dimileeh/aira-web.git",
        pr_number=42,
        branch_base="development",
        remote_push_branch=_REMOTE_BRANCH,
        task_kind="feature_branch_pr",
        branch_name=_REMOTE_BRANCH,
        task_policy={},
    )
    persisted: list[dict[str, str]] = []

    async def _execute(**_kwargs: object) -> bool:
        mark_provider_park_stranded_edits(state, _PRESERVED_HEAD)
        if parks:
            raise ProviderManagedKeyParkedError("PROVIDER_BUDGET_EXHAUSTED")
        raise ProviderRecoveryAuthError()

    async def _persist_state(_workspace_id: str, persisted_state: MonitorState) -> None:
        persisted.append(dict(persisted_state.threads_addressed_ids))

    async def _guard(**_kwargs: object) -> _GitPushResult:
        return _dirty(["stranded_fix.py"])

    monkeypatch.setattr(runner, "_open_monitor_log", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_write_monitor_log", AsyncMock())
    monkeypatch.setattr(runner, "_load_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(runner, "_load_state", lambda _ws: state)
    monkeypatch.setattr(
        runner,
        "_fetch_status_for_decision",
        AsyncMock(
            return_value=PRStatus(
                number=42,
                head_sha=_PRESERVED_HEAD,
                mergeable=MergeableState.MERGEABLE,
                check_state=CheckState.SUCCESS,
                unresolved_inline_threads=(),
                unresolved_review_comments=(),
                base_behind_count=0,
                merge_state_status=MergeStateStatus.CLEAN,
            )
        ),
    )
    monkeypatch.setattr(runner, "_execute", _execute)
    monkeypatch.setattr(runner, "_persist_state", _persist_state)
    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    terminate_failed = AsyncMock()
    monkeypatch.setattr(runner, "_terminate_failed", terminate_failed)

    await runner.run(  # type: ignore[attr-defined]
        workspace_id=_WORKSPACE_ID,
        compose_project="awf_ws_park_stranded",
        compose_file=tmp_path / "compose.yml",
    )

    terminate_failed.assert_awaited_once()
    assert len(persisted) == 1
    if not parks:
        # A broken-auth failure fails the workspace; there is no resume to vouch for.
        assert PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY not in persisted[0]
        return
    evidence = json.loads(persisted[0][PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY])
    assert list(evidence) == ["stranded_fix.py"]


def _hint_runner(factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> object:
    return make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )


async def _run_hint_cycle(
    runner: object,
    state: MonitorState,
    tmp_path: Path,
    *,
    workspace_id: str = _WORKSPACE_ID,
) -> _GitPushResult:
    assert state.pending_operator_hint is not None
    return await runner._run_operator_hint_cycle(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=_PRESERVED_HEAD,
        hint=state.pending_operator_hint,
        state=state,
        remote_branch=_REMOTE_BRANCH,
        compose_project="awf_ws_park_stranded",
        compose_file=tmp_path / "compose.yml",
    )


async def _budget_resume_state(tmp_path: Path) -> MonitorState:
    state = await _parked_state(tmp_path)
    state.pending_operator_hint = OperatorHint(
        reason="provider budget raised",
        operation_id="op_provider_budget",
        requested_at="2026-11-10T23:30:00+00:00",
        directive="Continue the task from where it stopped; do not redo completed work.",
    )
    return state


@pytest.mark.unit
async def test_budget_raise_resume_continues_past_the_dirty_guard(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    state = await _budget_resume_state(tmp_path)
    worktree = {"dirty": True}
    sink_calls: list[dict[str, object]] = []
    stop = _GitPushResult(pushed=False, failed=True, returncode=1, reason_code="STOP_HERE")

    async def _guard(**_kwargs: object) -> _GitPushResult | None:
        return _dirty(["stranded_fix.py"]) if worktree["dirty"] else None

    async def _sink(**kwargs: object) -> bool:
        sink_calls.append(kwargs)
        worktree["dirty"] = False
        return True

    async def _start_head(**_kwargs: object) -> tuple[str, _GitPushResult]:
        return (_PRESERVED_HEAD, stop)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)

    # Reaching the start-HEAD read proves the repair-start guard let the resume in.
    assert await _run_hint_cycle(runner, state, tmp_path) is stop
    assert len(sink_calls) == 1
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_the_marker_leaves_the_row_before_the_resumed_repair_runs(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The hint cycle only persists its state after the agent returns. A marker
    # dropped in memory alone would reload on a restart mid-agent, and the next
    # resume would commit the agent's leftover files as the park's residue.
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            **(ws.monitor_threads_addressed or {}),
            PROVIDER_PARK_STRANDED_EDITS_STATE_KEY: _PRESERVED_HEAD,
            PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY: "{}",
        }
        await session.commit()
    runner = _hint_runner(factory, tmp_path)
    state = await _budget_resume_state(tmp_path)
    worktree = {"dirty": True}
    rows_at_repair_start: list[dict[str, str]] = []
    stop = _GitPushResult(pushed=False, failed=True, returncode=1, reason_code="STOP_HERE")

    async def _guard(**_kwargs: object) -> _GitPushResult | None:
        return _dirty(["stranded_fix.py"]) if worktree["dirty"] else None

    async def _sink(**_kwargs: object) -> bool:
        worktree["dirty"] = False
        return True

    async def _start_head(**_kwargs: object) -> tuple[str, _GitPushResult]:
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            rows_at_repair_start.append(dict(ws.monitor_threads_addressed or {}))
        return (_PRESERVED_HEAD, stop)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)

    assert await _run_hint_cycle(runner, state, tmp_path, workspace_id=workspace_id) is stop
    assert len(rows_at_repair_start) == 1
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in rows_at_repair_start[0]
    assert PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY not in rows_at_repair_start[0]


@pytest.mark.unit
async def test_budget_raise_resume_reports_a_repeat_sink_failure_not_pre_existing_dirt(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    state = await _budget_resume_state(tmp_path)

    async def _guard(**_kwargs: object) -> _GitPushResult:
        return _dirty(["stranded_fix.py"])

    async def _sink(**_kwargs: object) -> bool:
        return False

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        raise AssertionError("the resume must not start a repair over stranded edits")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)

    result = await _run_hint_cycle(runner, state, tmp_path)

    assert result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert result.terminal_monitor_failure is True


@pytest.mark.unit
@pytest.mark.parametrize("pr_terminal", [False, True])
async def test_a_resume_sink_error_ends_the_resume_like_the_agents_own(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pr_terminal: bool,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    state = await _budget_resume_state(tmp_path)
    moot = _GitPushResult(pushed=False, failed=False, returncode=0, reason_code="PR_MERGED")

    async def _guard(**_kwargs: object) -> _GitPushResult:
        return _dirty(["stranded_fix.py"])

    async def _sink(**_kwargs: object) -> bool:
        raise _MonitorPolicyBlockedError("supply-chain policy blocked the stranded edits")

    async def _terminal_check(**_kwargs: object) -> _GitPushResult | None:
        return moot if pr_terminal else None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _terminal_check)

    result = await _run_hint_cycle(runner, state, tmp_path)

    if pr_terminal:
        # A PR that merged or closed meanwhile makes the resume moot; no human ping.
        assert result is moot
        assert state.pending_operator_hint is not None
        assert state.pending_operator_hint.status == "pending"
        return
    assert result.failed is False
    assert "supply-chain policy blocked the stranded edits" in result.stderr
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"


@pytest.mark.unit
@pytest.mark.parametrize("pr_terminal", [False, True])
async def test_budget_raise_resume_parks_unexpected_changes_for_a_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pr_terminal: bool,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = await _budget_resume_state(tmp_path)
    _write(tmp_path, "operator_notes.py", "an operator's unrelated edit\n")
    moot = _GitPushResult(pushed=False, failed=False, returncode=0, reason_code="PR_MERGED")

    async def _guard(**_kwargs: object) -> _GitPushResult:
        return _dirty(["operator_notes.py", "stranded_fix.py"])

    async def _sink(**_kwargs: object) -> bool:
        raise AssertionError("the resume must not commit over unexpected changes")

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        raise AssertionError("the resume must not start a repair over unexpected changes")

    async def _terminal_check(**_kwargs: object) -> _GitPushResult | None:
        return moot if pr_terminal else None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _terminal_check)

    result = await _run_hint_cycle(runner, state, tmp_path)

    assert state.pending_operator_hint is not None
    if pr_terminal:
        # A PR that merged or closed meanwhile makes the resume moot; no human ping.
        assert result is moot
        assert state.pending_operator_hint.status == "pending"
        return
    assert result.reason_code == "PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES"
    assert result.terminal_monitor_failure is False
    assert state.pending_operator_hint.status == "needs_human"
    assert "operator_notes.py" in (state.pending_operator_hint.status_reason or "")
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _PRESERVED_HEAD
    assert (tmp_path / _WORKSPACE_ID / "operator_notes.py").read_text() == (
        "an operator's unrelated edit\n"
    )


@pytest.mark.unit
async def test_the_sink_stages_only_the_paths_it_is_limited_to(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _ok(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_repair, "verify_head_object_exists", _ok)
    monkeypatch.setattr(remote_repair, "repair_agent_runtime_ownership", _ok)
    workspace_id = await seed_monitoring_workspace(factory)
    status = " M stranded_fix.py\n?? operator_notes.py\n"
    cmd = FakeCommandRunner()
    cmd.queue_result(stdout=status)  # status before the commit-side checks
    cmd.queue_result(stdout=status)  # status under the writer lock
    cmd.queue_result()  # add
    cmd.queue_result(returncode=1)  # diff --cached --quiet: something is staged
    cmd.queue_result()  # commit
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    (tmp_path / workspace_id).mkdir()
    protected_scope_statuses: list[object] = []

    async def _repair(**kwargs: object) -> object:
        protected_scope_statuses.append(kwargs["status_stdout"])
        return kwargs["status_stdout"]

    async def _violations(**kwargs: object) -> list[object]:
        protected_scope_statuses.append(kwargs["status_stdout"])
        return []

    monkeypatch.setattr(runner, "_repair_protected_scope_changes_before_commit", _repair)
    monkeypatch.setattr(runner, "_protected_scope_violations_for_status", _violations)

    committed = await runner._commit_dirty_worktree(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        message="fix: commit stranded edits",
        compose_project="awf_ws_park_stranded",
        compose_file=tmp_path / "compose.yml",
        task_tag=None,
        only_paths=("stranded_fix.py",),
    )

    assert committed is True
    add = next(call.args for call in cmd.calls if "add" in call.args)
    assert add[add.index("--") + 1 :] == ["stranded_fix.py"]
    # Protected-scope repair never sees, so never reverts, a path outside the limit.
    assert protected_scope_statuses == [" M stranded_fix.py\n", " M stranded_fix.py\n"]


@pytest.mark.unit
async def test_the_sink_keeps_its_limit_when_it_reenters_after_head_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The residue re-entry after HEAD-object recovery keeps ``only_paths``
    (PRRT_kwDOSJAM6s62ABOL): an unrelated path left dirty after the recovered
    commit's protected-scope repair is neither staged nor handed to repair."""
    head_checks = [False, True]

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return head_checks.pop(0)

    async def _ok(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _recover(*_args: object, **_kwargs: object) -> str:
        return "recovered_sha_12345"

    monkeypatch.setattr(remote_repair, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(remote_repair, "_recover_missing_head_object_from_filesystem", _recover)
    monkeypatch.setattr(remote_repair, "repair_agent_runtime_ownership", _ok)
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")
    residue = " M stranded_fix.py\n?? operator_notes.py\n"
    cmd = FakeCommandRunner()
    cmd.queue_result(stdout="live_head\n")  # rev-parse HEAD
    cmd.queue_result(stdout="M\0stranded_fix.py\0")  # recovered diff
    cmd.queue_result(stdout=residue)  # status after the recovered commit's repair
    cmd.queue_result(stdout=residue)  # re-entry: status before the commit-side checks
    cmd.queue_result(stdout=residue)  # re-entry: status under the writer lock
    cmd.queue_result()  # add
    cmd.queue_result(returncode=1)  # diff --cached --quiet: something is staged
    cmd.queue_result()  # commit
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    protected_scope_statuses: list[object] = []

    async def _repair(**kwargs: object) -> object:
        protected_scope_statuses.append(kwargs["status_stdout"])
        return kwargs["status_stdout"]

    async def _no_policy_block(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_repair_protected_scope_changes_before_commit", _repair)
    monkeypatch.setattr(runner, "_refresh_supply_chain_policy_before_push", _no_policy_block)

    committed = await runner._commit_dirty_worktree(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        message="fix: commit stranded edits",
        compose_project="awf_ws_park_stranded",
        compose_file=tmp_path / "compose.yml",
        operation_start_head="base_sha_12345",
        task_tag=None,
        only_paths=("stranded_fix.py",),
    )

    assert committed is True
    assert head_checks == []
    add = next(call.args for call in cmd.calls if "add" in call.args)
    assert add[add.index("--") + 1 :] == ["stranded_fix.py"]
    assert protected_scope_statuses == [" M stranded_fix.py\n", " M stranded_fix.py\n"]
