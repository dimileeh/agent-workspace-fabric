"""A baseline-park resume keeps the fix-pass baseline commit on the branch.

Split out of ``test_pr_monitor_provider_park_stranded_edits`` (whose fakes it
reuses) to keep that module under the first-party line budget. When a transient
park lands after a pre-push fix pass, the resumed HEAD must still descend from
that pass's unpushed baseline; a rewritten HEAD is re-parented onto it, and a
resume that cannot keep it fails closed (PRRT_kwDOSJAM6s63M6rz).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
    mark_provider_park_stranded_edits,
    provider_park_resume_kept_head,
    record_provider_park_stranded_edits_evidence,
    refresh_provider_park_stranded_edits_evidence,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace
from tests.unit.runtime.test_pr_monitor_provider_park_stranded_edits import (
    _PRESERVED_HEAD,
    _WORKSPACE_ID,
    _dirty,
    _FakeRunner,
    _resume,
    _write,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


_BASELINE_HEAD = "f" * 40
_REPARENTED_HEAD = "c" * 40
_RESUME_COMMIT_HEAD = "a" * 40


class _BaselineRunner(_FakeRunner):
    """A resume runner whose resumed HEAD the fix-pass ancestry check reads through git."""

    def __init__(
        self,
        guard_results: list[_GitPushResult | None],
        cmd: FakeCommandRunner,
        *,
        head: str | None,
    ) -> None:
        super().__init__(guard_results, commits=True)
        self._deps = SimpleNamespace(runner=cmd)
        self._rev_parse_head = AsyncMock(return_value=head)
        self._resolve_task_tag = AsyncMock(return_value="AWF-1234")


async def _baseline_parked_state(tmp_path: Path) -> MonitorState:
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = MonitorState()
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD, baseline_head=_BASELINE_HEAD)
    await record_provider_park_stranded_edits_evidence(
        _FakeRunner([_dirty(["stranded_fix.py"])], commits=True),
        workspace_id=_WORKSPACE_ID,
        worktree_path=tmp_path / _WORKSPACE_ID,
        state=state,
    )
    return state


def _git_args(cmd: FakeCommandRunner) -> list[list[str]]:
    return [call.args[call.args.index("-C") + 2 :] for call in cmd.calls]


@pytest.mark.unit
def test_a_later_park_never_inherits_an_earlier_baseline() -> None:
    state = MonitorState()
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD, baseline_head=_BASELINE_HEAD)
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY] == (
        _BASELINE_HEAD
    )

    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD)

    assert PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize("head", [_BASELINE_HEAD, _PRESERVED_HEAD])
async def test_a_resumed_head_on_the_fix_pass_baseline_is_kept(tmp_path: Path, head: str) -> None:
    state = await _baseline_parked_state(tmp_path)
    cmd = FakeCommandRunner()
    cmd.queue_result()  # merge-base --is-ancestor: HEAD descends from the baseline.
    runner = _BaselineRunner([_dirty(["stranded_fix.py"]), None], cmd, head=head)

    assert await _resume(runner, state, tmp_path) is None

    assert _git_args(cmd) == (
        [] if head == _BASELINE_HEAD else [["merge-base", "--is-ancestor", _BASELINE_HEAD, head]]
    )
    assert PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_a_rewritten_resumed_head_is_reparented_onto_the_fix_pass_baseline(
    tmp_path: Path,
) -> None:
    # The fix agent amended the unpushed baseline commit away before the transient
    # park; publishing that tip would drop it from the branch (PRRT_kwDOSJAM6s63M6rz).
    state = await _baseline_parked_state(tmp_path)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1)  # merge-base --is-ancestor: not a descendant.
    cmd.queue_result(stdout="tree-resumed\n")
    cmd.queue_result(stdout="tree-baseline\n")
    cmd.queue_result(stdout="fix: the agent's amended fix\n")
    cmd.queue_result(stdout=f"{_REPARENTED_HEAD}\n")
    cmd.queue_result()  # reset --hard onto the reparented commit.
    runner = _BaselineRunner([_dirty(["stranded_fix.py"]), None], cmd, head=_PRESERVED_HEAD)

    assert await _resume(runner, state, tmp_path) is None

    commit_tree = _git_args(cmd)[4]
    assert commit_tree[commit_tree.index("commit-tree") :][:4] == [
        "commit-tree",
        "tree-resumed",
        "-p",
        _BASELINE_HEAD,
    ]
    assert commit_tree[-1] == "AWF-1234 fix: the agent's amended fix"
    assert _git_args(cmd)[5] == ["reset", "--hard", _REPARENTED_HEAD]
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids
    assert PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_a_rewrite_with_no_net_change_is_reset_to_the_fix_pass_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Like every re-parent git step, the reset ignores process object-lookup
    # overrides (PRRT_kwDOSJAM6s63OA3P).
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/elsewhere/objects")
    state = await _baseline_parked_state(tmp_path)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1)
    cmd.queue_result(stdout="same-tree\n")
    cmd.queue_result(stdout="same-tree\n")
    cmd.queue_result()
    runner = _BaselineRunner([_dirty(["stranded_fix.py"]), None], cmd, head=_PRESERVED_HEAD)

    assert await _resume(runner, state, tmp_path) is None

    assert _git_args(cmd)[-1] == ["reset", "--hard", _BASELINE_HEAD]
    reset_env = cmd.calls[-1].env
    assert reset_env is not None
    assert "GIT_OBJECT_DIRECTORY" not in reset_env
    assert PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize("net_change", [True, False])
@pytest.mark.parametrize("resumed_head", [_PRESERVED_HEAD, _RESUME_COMMIT_HEAD])
async def test_a_rewritten_park_head_names_its_replacement_durably(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, net_change: bool, resumed_head: str
) -> None:
    # The rewrite discards the parked SHA, so a baseline-park resume must look for
    # the commit that replaced it instead (PRRT_kwDOSJAM6s63OA3D). When the resume
    # committed stranded edits first, the rewritten HEAD is that commit, yet the
    # replacement is still keyed to the parked SHA (PRRT_kwDOSJAM6s63OyKh).
    workspace_id = await seed_monitoring_workspace(factory)
    state = await _baseline_parked_state(tmp_path)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1)  # merge-base --is-ancestor: not a descendant.
    if net_change:
        cmd.queue_result(stdout="tree-resumed\n")
        cmd.queue_result(stdout="tree-baseline\n")
        cmd.queue_result(stdout="fix: the agent's amended fix\n")
        cmd.queue_result(stdout=f"{_REPARENTED_HEAD}\n")
    else:
        cmd.queue_result(stdout="same-tree\n")
        cmd.queue_result(stdout="same-tree\n")
    cmd.queue_result()  # reset --hard
    runner = _BaselineRunner([_dirty(["stranded_fix.py"]), None], cmd, head=resumed_head)
    runner._deps = SimpleNamespace(runner=cmd, session_factory=factory)

    assert await _resume(runner, state, tmp_path, workspace_id=workspace_id) is None

    replacement = _REPARENTED_HEAD if net_change else _BASELINE_HEAD
    assert provider_park_resume_kept_head(state, _PRESERVED_HEAD) == replacement
    # Only the parked SHA the rewrite replaced maps to the replacement.
    assert provider_park_resume_kept_head(state, "d" * 40) == "d" * 40
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        reloaded = MonitorState(threads_addressed_ids=dict(ws.monitor_threads_addressed or {}))
    assert provider_park_resume_kept_head(reloaded, _PRESERVED_HEAD) == replacement
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in reloaded.threads_addressed_ids

    # The next park starts without the previous resume's rewrite.
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD)
    assert PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize(
    "record",
    [None, "not json", json.dumps(["list"]), json.dumps({"from": _PRESERVED_HEAD, "to": ""})],
)
def test_an_unusable_rewrite_record_keeps_the_parked_head(record: str | None) -> None:
    state = MonitorState()
    if record is not None:
        state.threads_addressed_ids[PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY] = record

    assert provider_park_resume_kept_head(state, _PRESERVED_HEAD) == _PRESERVED_HEAD
    assert provider_park_resume_kept_head(state, None) is None


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["head_unreadable", "commit_tree", "reset_to_baseline"])
async def test_a_resumed_head_that_cannot_keep_the_baseline_fails_closed(
    tmp_path: Path, failure: str
) -> None:
    state = await _baseline_parked_state(tmp_path)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1)
    if failure == "commit_tree":
        cmd.queue_result(stdout="tree-resumed\n")
        cmd.queue_result(stdout="tree-baseline\n")
        cmd.queue_result(stdout="fix: the agent's amended fix\n")
        cmd.queue_result(returncode=128, stderr="fatal: commit-tree failed")
    else:
        cmd.queue_result(stdout="same-tree\n")
        cmd.queue_result(stdout="same-tree\n")
        cmd.queue_result(returncode=128, stderr="fatal: reset failed")
    head = None if failure == "head_unreadable" else _PRESERVED_HEAD
    runner = _BaselineRunner([_dirty(["stranded_fix.py"]), None], cmd, head=head)

    result = await _resume(runner, state, tmp_path)

    assert result is not None
    assert result.reason_code == "PRE_PUSH_VALIDATION_REPARENT_FAILED"
    assert result.terminal_monitor_failure is True
    assert result.details is not None
    assert result.details["phase"] == "provider_park_resume"
    assert result.details["fix_start_head"] == _BASELINE_HEAD
    assert result.details["head"] == head
    if failure == "head_unreadable":
        assert cmd.calls == []
    # The marker stays: nothing is published, and the next resume re-checks.
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY] == (
        _BASELINE_HEAD
    )


_REPARKED_HEAD = "e" * 40


@pytest.mark.unit
async def test_a_reparked_sink_rekeys_the_rewrite_to_the_reparked_head(tmp_path: Path) -> None:
    # The resumed sink's protected-scope repair self-committed before another
    # baseline outage re-parked it, and the re-park event records that new HEAD.
    # The marker must name it too, or the next resume keys its re-parent to the
    # first park's SHA and the baseline-park resume still requires the
    # reparented-away commit (PRRT_kwDOSJAM6s63SI0A).
    state = await _baseline_parked_state(tmp_path)
    reparker = _BaselineRunner([_dirty(["stranded_fix.py"])], FakeCommandRunner(), head=None)
    reparker._persist_state = AsyncMock()
    reparker._rev_parse_head = AsyncMock(return_value=_REPARKED_HEAD)

    await refresh_provider_park_stranded_edits_evidence(
        reparker, workspace_id=_WORKSPACE_ID, worktree_path=tmp_path / _WORKSPACE_ID, state=state
    )

    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _REPARKED_HEAD
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY] == (
        _BASELINE_HEAD
    )
    # No ownership boundary, so no delta anchor is invented.
    assert PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY not in state.threads_addressed_ids
    reparker._persist_state.assert_awaited_once_with(_WORKSPACE_ID, state)

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1)  # merge-base --is-ancestor: not a descendant.
    cmd.queue_result(stdout="tree-resumed\n")
    cmd.queue_result(stdout="tree-baseline\n")
    cmd.queue_result(stdout="fix: the agent's amended fix\n")
    cmd.queue_result(stdout=f"{_REPARENTED_HEAD}\n")
    cmd.queue_result()  # reset --hard onto the reparented commit.
    runner = _BaselineRunner([_dirty(["stranded_fix.py"]), None], cmd, head=_REPARKED_HEAD)

    assert await _resume(runner, state, tmp_path) is None
    assert provider_park_resume_kept_head(state, _REPARKED_HEAD) == _REPARENTED_HEAD


@pytest.mark.unit
@pytest.mark.parametrize("reparked_head", [_REPARKED_HEAD, None])
async def test_a_reparked_sink_keeps_the_original_delta_anchor(
    tmp_path: Path, reparked_head: str | None
) -> None:
    # Re-keying the marker's HEAD must not move a boundary-only park's delta
    # anchor, which defaulted to the first park's HEAD; an unreadable HEAD keeps
    # the marker as it was.
    state = MonitorState()
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD, owned_paths=["stranded_fix.py"])
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    reparker = _BaselineRunner([_dirty(["stranded_fix.py"])], FakeCommandRunner(), head=None)
    reparker._persist_state = AsyncMock()
    reparker._rev_parse_head = AsyncMock(return_value=reparked_head)

    await refresh_provider_park_stranded_edits_evidence(
        reparker, workspace_id=_WORKSPACE_ID, worktree_path=tmp_path / _WORKSPACE_ID, state=state
    )

    threads_addressed = state.threads_addressed_ids
    assert threads_addressed[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == (
        reparked_head or _PRESERVED_HEAD
    )
    assert threads_addressed.get(PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY) == (
        _PRESERVED_HEAD if reparked_head else None
    )
    assert json.loads(threads_addressed[PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY]) == [
        "stranded_fix.py"
    ]


@pytest.mark.unit
@pytest.mark.parametrize("unreadable", ["status_failed", "hash_failed"])
async def test_a_reparked_sink_that_cannot_rerecord_keeps_the_earlier_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    # A refresh that records nothing must not leave the marker with no evidence:
    # the next resume would reject the park's own dirt as unexpected. The earlier
    # fingerprints are still checked against every recorded path's content, so
    # keeping them never commits a path that changed since.
    state = await _baseline_parked_state(tmp_path)
    earlier = state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY]
    status_failed = _GitPushResult(
        pushed=False, failed=True, returncode=128, reason_code="REPAIR_WORKTREE_STATUS_FAILED"
    )
    guard = status_failed if unreadable == "status_failed" else _dirty(["stranded_fix.py"])
    if unreadable == "hash_failed":

        def _unreadable(*_args: object) -> dict[str, str | None]:
            raise PermissionError("unreadable")

        monkeypatch.setattr(
            "awf.runtime.pr_monitor_runner.provider_park_stranded_edits._path_identities",
            _unreadable,
        )
    reparker = _BaselineRunner([guard], FakeCommandRunner(), head=_REPARKED_HEAD)
    reparker._persist_state = AsyncMock()

    await refresh_provider_park_stranded_edits_evidence(
        reparker, workspace_id=_WORKSPACE_ID, worktree_path=tmp_path / _WORKSPACE_ID, state=state
    )

    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY] == earlier
    reparker._persist_state.assert_awaited_once_with(_WORKSPACE_ID, state)


class _WorkerRestartError(Exception):
    """The worker dies after the rewrite moved HEAD but before the marker drop."""


@pytest.mark.unit
@pytest.mark.parametrize("net_change", [True, False])
async def test_a_restart_after_the_rewrite_still_names_its_replacement(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    net_change: bool,
) -> None:
    # The intent reaches the row before HEAD moves, so a retry that finds HEAD
    # already on the baseline still records what replaced the parked SHA, and a
    # directiveless resume never looks for the removed commit (PRRT_kwDOSJAM6s63UyWC).
    workspace_id = await seed_monitoring_workspace(factory)
    state = await _baseline_parked_state(tmp_path)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = dict(state.threads_addressed_ids)
        await session.commit()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1)  # merge-base --is-ancestor: not a descendant.
    if net_change:
        cmd.queue_result(stdout="tree-resumed\n")
        cmd.queue_result(stdout="tree-baseline\n")
        cmd.queue_result(stdout="fix: the agent's amended fix\n")
        cmd.queue_result(stdout=f"{_REPARENTED_HEAD}\n")
    else:
        cmd.queue_result(stdout="same-tree\n")
        cmd.queue_result(stdout="same-tree\n")
    cmd.queue_result()  # reset --hard
    runner = _BaselineRunner([_dirty(["stranded_fix.py"]), None], cmd, head=_PRESERVED_HEAD)
    runner._deps = SimpleNamespace(runner=cmd, session_factory=factory)
    drop_marker = AsyncMock(side_effect=_WorkerRestartError)
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.provider_park_stranded_edits."
        "_drop_marker_from_workspace_row",
        drop_marker,
    )

    with pytest.raises(_WorkerRestartError):
        await _resume(runner, state, tmp_path, workspace_id=workspace_id)
    monkeypatch.undo()

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        restarted = MonitorState(threads_addressed_ids=dict(ws.monitor_threads_addressed or {}))
    assert restarted.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == (
        _PRESERVED_HEAD
    )
    replacement = _REPARENTED_HEAD if net_change else _BASELINE_HEAD
    retry_cmd = FakeCommandRunner()
    retry_cmd.queue_result()  # merge-base --is-ancestor: HEAD now descends.
    retry = _BaselineRunner([None], retry_cmd, head=replacement)
    retry._deps = SimpleNamespace(runner=retry_cmd, session_factory=factory)

    assert await _resume(retry, restarted, tmp_path, workspace_id=workspace_id) is None

    assert provider_park_resume_kept_head(restarted, _PRESERVED_HEAD) == replacement
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        reloaded = MonitorState(threads_addressed_ids=dict(ws.monitor_threads_addressed or {}))
    assert provider_park_resume_kept_head(reloaded, _PRESERVED_HEAD) == replacement
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in reloaded.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize("row", ["write_fault", "missing"])
async def test_the_rewrite_intent_reaches_the_row_before_head_moves(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
) -> None:
    # A fault writing the intent stops the resume before any git op moves HEAD; a
    # workspace row that is gone has nothing to record and does not block it.
    workspace_id = await seed_monitoring_workspace(factory) if row == "write_fault" else "ws_gone"
    state = await _baseline_parked_state(tmp_path)

    def _faulting_factory() -> AsyncSession:
        session = factory()

        async def _commit() -> None:
            raise SQLAlchemyError("connection dropped")

        monkeypatch.setattr(session, "commit", _commit)
        return session

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1)  # merge-base --is-ancestor: not a descendant.
    cmd.queue_result(stdout="same-tree\n")
    cmd.queue_result(stdout="same-tree\n")
    cmd.queue_result()  # reset --hard
    runner = _BaselineRunner([_dirty(["stranded_fix.py"]), None], cmd, head=_PRESERVED_HEAD)
    runner._deps = SimpleNamespace(
        runner=cmd, session_factory=_faulting_factory if row == "write_fault" else factory
    )

    if row == "write_fault":
        with pytest.raises(SQLAlchemyError):
            await _resume(runner, state, tmp_path, workspace_id=workspace_id)
        assert _git_args(cmd) == [["merge-base", "--is-ancestor", _BASELINE_HEAD, _PRESERVED_HEAD]]
        assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == (
            _PRESERVED_HEAD
        )
    else:
        assert await _resume(runner, state, tmp_path, workspace_id=workspace_id) is None
        assert _git_args(cmd)[-1] == ["reset", "--hard", _BASELINE_HEAD]
        assert provider_park_resume_kept_head(state, _PRESERVED_HEAD) == _BASELINE_HEAD


@pytest.mark.unit
@pytest.mark.parametrize(
    "record",
    [
        "not json",
        json.dumps({"to": _REPARENTED_HEAD}),
        json.dumps({"from": _PRESERVED_HEAD, "to": _REPARENTED_HEAD}),
    ],
)
async def test_a_descending_head_only_completes_an_open_rewrite_intent(
    tmp_path: Path, record: str
) -> None:
    # A finished or unreadable record is never re-pointed at whatever HEAD the
    # next resume finds.
    state = await _baseline_parked_state(tmp_path)
    state.threads_addressed_ids[PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY] = record
    cmd = FakeCommandRunner()
    cmd.queue_result()  # merge-base --is-ancestor: HEAD descends from the baseline.
    runner = _BaselineRunner([None], cmd, head=_RESUME_COMMIT_HEAD)

    assert await _resume(runner, state, tmp_path) is None

    assert state.threads_addressed_ids[PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY] == record
