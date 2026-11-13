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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
    mark_provider_park_stranded_edits,
    provider_park_resume_kept_head,
    record_provider_park_stranded_edits_evidence,
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
