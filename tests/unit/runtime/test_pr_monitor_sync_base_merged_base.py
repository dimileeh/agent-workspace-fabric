"""``_run_sync_base`` records the base SHA it actually merged (#1167).

``sync_base_push`` audit events used to report ``workspace.base_commit`` — the
base recorded at adoption — which made a correct live-base merge look stale.
The sync-base envelope now carries the merged base: ``MERGE_HEAD`` on a conflict
(worktree-local and exact) and the completed merge commit's ``HEAD^2`` after a
clean merge — never the shared mirror's mutable ``origin/<base>``, which a
concurrent workspace's fetch can advance past the commit actually merged.
``HEAD^2`` is trusted only when ``HEAD^1`` is ``ORIG_HEAD`` and the commit is
new (not already reachable from ``origin/<base>``). A fast-forward instead
lands ``HEAD`` on the exact base commit merged, so that ``HEAD`` is recorded —
never a pre-existing commit's unrelated second parent. "Already up to date"
leaves ``HEAD`` unmoved and falls back.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner import remote_ops
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

_MERGED_BASE = "b" * 40
_PRE_MERGE_HEAD = "a" * 40
_MERGE_COMMIT = "d" * 40
_CLEAN_MERGE_REFS = ["rev-parse", "ORIG_HEAD", "HEAD", "HEAD^@"]
_HEAD_IN_BASE_ARGS = ["merge-base", "--is-ancestor", "HEAD", "origin/main"]


async def _open_pr(**_kwargs: object) -> None:
    return None


async def _no_block(**_kwargs: object) -> None:
    return None


async def _validated_push(**_kwargs: object) -> _GitPushResult:
    return _GitPushResult(pushed=True, failed=False, returncode=0)


class _Adapter:
    async def run(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout="", stderr="")


class _Runner(SimpleNamespace):
    async def _run_monitor_agent_with_service_recovery(self, **kwargs: Any) -> object:
        return await self._deps.adapter.run(**kwargs)


def _runner(tmp_path: Path, cmd: FakeCommandRunner) -> _Runner:
    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return "operation-start-sha", None

    async def _task_tag(_workspace_id: str) -> None:
        return None

    async def _false(_workspace_id: str) -> bool:
        return False

    async def _commit(**_kwargs: object) -> bool:
        return True

    return _Runner(
        _worktrees_root=tmp_path,
        _workspace_runtime_context="",
        _repair_operation_start_head_result=_start_head,
        _resolve_task_tag=_task_tag,
        _fetch_base=_no_block,
        _provider_recovery_suppresses_cli=_false,
        _commit_dirty_worktree=_commit,
        _protected_scope_push_block=_no_block,
        _post_action_pr_terminal_push_result_if_moot=_open_pr,
        _validated_git_push_result=_validated_push,
        _deps=SimpleNamespace(runner=cmd, adapter=_Adapter()),
    )


async def _sync(runner: _Runner) -> _GitPushResult:
    return await remote_ops._run_sync_base(
        runner,  # type: ignore[arg-type]
        workspace_id="ws-sync",
        repo=SimpleNamespace(slug=lambda: "owner/repo"),  # type: ignore[arg-type]
        pr_number=614,
        base_branch="main",
        remote_branch="awf/ws-sync",
        compose_project="proj",
        compose_file=Path("compose.yml"),
    )


def _is_rev_parse(ref: str) -> Any:
    return lambda args: args[-2:] == ["rev-parse", ref]


def _is_clean_merge_rev_parse(args: list[str]) -> bool:
    return args[-4:] == _CLEAN_MERGE_REFS


def _is_head_in_base(args: list[str]) -> bool:
    return args[-4:] == _HEAD_IN_BASE_ARGS


@pytest.fixture
def _ownership_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_ops, "repair_agent_runtime_ownership", _ok)


@pytest.mark.unit
@pytest.mark.usefixtures("_ownership_ok")
async def test_clean_merge_reports_merge_commit_second_parent(tmp_path: Path) -> None:
    """A concurrent fetch advancing the shared ``origin/<base>`` cannot skew the audit."""
    cmd = FakeCommandRunner()
    cmd.respond_when(
        _is_clean_merge_rev_parse,
        stdout=f"{_PRE_MERGE_HEAD}\n{_MERGE_COMMIT}\n{_PRE_MERGE_HEAD}\n{_MERGED_BASE.upper()}\n",
    )
    # The shared tracking ref has already moved past the commit this merge integrated.
    cmd.respond_when(_is_rev_parse("origin/main"), stdout=f"{'c' * 40}\n")
    # The merge commit is new: not yet reachable from the base.
    cmd.respond_when(_is_head_in_base, returncode=1)

    result = await _sync(_runner(tmp_path, cmd))

    assert result.pushed is True
    assert result.merged_base_sha == _MERGED_BASE
    merge_index = next(
        i for i, c in enumerate(cmd.calls) if "merge" in c.args and "--no-edit" in c.args
    )
    rev_parses = [(i, c.args) for i, c in enumerate(cmd.calls) if "rev-parse" in c.args]
    assert [args[-4:] for _i, args in rev_parses] == [_CLEAN_MERGE_REFS]
    assert rev_parses[0][0] > merge_index


@pytest.mark.unit
@pytest.mark.usefixtures("_ownership_ok")
async def test_conflict_merge_reports_merge_head(tmp_path: Path) -> None:
    """A conflicted merge reports MERGE_HEAD as the merged base."""
    cmd = FakeCommandRunner()
    cmd.respond_when(_is_rev_parse("MERGE_HEAD"), stdout=f"{_MERGED_BASE.upper()}\n")
    cmd.respond_when(_is_rev_parse("origin/main"), stdout=f"{'c' * 40}\n")
    cmd.queue_result()  # merge --abort
    cmd.queue_result(returncode=1, stderr="merge conflict")  # merge
    cmd.queue_result(stdout="UU src/conflict.py\n")  # status --porcelain

    result = await _sync(_runner(tmp_path, cmd))

    assert result.pushed is True
    assert result.merged_base_sha == _MERGED_BASE
    assert [c.args[-1] for c in cmd.calls if "rev-parse" in c.args] == ["MERGE_HEAD"]


@pytest.mark.unit
@pytest.mark.usefixtures("_ownership_ok")
async def test_unresolvable_merged_base_falls_back_to_none(tmp_path: Path) -> None:
    """An unresolvable merged base falls back to None and logs it."""
    cmd = FakeCommandRunner()
    # An unreadable merge result falls back to the audit's pre-#1167 default.
    cmd.respond_when(_is_clean_merge_rev_parse, returncode=128, stderr="bad revision")

    with structlog.testing.capture_logs() as captured:
        result = await _sync(_runner(tmp_path, cmd))

    assert result.pushed is True
    assert result.merged_base_sha is None
    assert any(r.get("event") == "monitor.sync_base_merged_base_unresolved" for r in captured)


@pytest.mark.unit
@pytest.mark.usefixtures("_ownership_ok")
@pytest.mark.parametrize(
    "parents",
    [
        # Fast-forward onto an ordinary base commit: no second parent at all.
        ("f" * 40,),
        # Fast-forward onto a base merge commit whose first parent is ORIG_HEAD.
        (_PRE_MERGE_HEAD, "9" * 40),
        # Fast-forward onto a base merge commit with unrelated parents.
        ("f" * 40, "9" * 40),
    ],
)
async def test_fast_forward_reports_new_head(tmp_path: Path, parents: tuple[str, ...]) -> None:
    """A fast-forward records the base commit it landed on, not the adoption-time base."""
    cmd = FakeCommandRunner()
    cmd.respond_when(
        _is_clean_merge_rev_parse,
        stdout="\n".join((_PRE_MERGE_HEAD, _MERGED_BASE.upper(), *parents)) + "\n",
    )
    # The shared tracking ref has already moved past the commit fast-forwarded to.
    cmd.respond_when(_is_rev_parse("origin/main"), stdout=f"{'c' * 40}\n")
    cmd.respond_when(_is_head_in_base, returncode=0)

    with structlog.testing.capture_logs() as captured:
        result = await _sync(_runner(tmp_path, cmd))

    assert result.pushed is True
    assert result.merged_base_sha == _MERGED_BASE
    assert sum(1 for c in cmd.calls if _is_head_in_base(c.args)) == 1
    assert not any(r.get("event") == "monitor.sync_base_merged_base_unresolved" for r in captured)


@pytest.mark.unit
@pytest.mark.usefixtures("_ownership_ok")
@pytest.mark.parametrize(
    ("head", "first_parent", "probes"),
    [
        # "Already up to date": HEAD is still ORIG_HEAD, an adopted merge commit.
        (_PRE_MERGE_HEAD, "f" * 40, 0),
        # A commit outside the base whose first parent is not ORIG_HEAD.
        (_MERGE_COMMIT, "f" * 40, 1),
    ],
)
async def test_merge_without_new_merge_commit_ignores_unrelated_second_parent(
    tmp_path: Path, head: str, first_parent: str, probes: int
) -> None:
    """A merge that creates no new merge commit ignores an unrelated second parent."""
    cmd = FakeCommandRunner()
    side_branch_tip = "9" * 40
    cmd.respond_when(
        _is_clean_merge_rev_parse,
        stdout=f"{_PRE_MERGE_HEAD}\n{head}\n{first_parent}\n{side_branch_tip}\n",
    )
    cmd.respond_when(_is_head_in_base, returncode=1)

    with structlog.testing.capture_logs() as captured:
        result = await _sync(_runner(tmp_path, cmd))

    assert result.pushed is True
    assert result.merged_base_sha is None
    assert sum(1 for c in cmd.calls if _is_head_in_base(c.args)) == probes
    unresolved = [
        r for r in captured if r.get("event") == "monitor.sync_base_merged_base_unresolved"
    ]
    assert len(unresolved) == 1
    assert unresolved[0]["ref"] == "HEAD^2"
    assert unresolved[0]["returncode"] == 0


@pytest.mark.unit
@pytest.mark.usefixtures("_ownership_ok")
async def test_ancestry_probe_failure_falls_back(tmp_path: Path) -> None:
    """A failed ancestry probe fails closed to the fallback instead of guessing."""
    cmd = FakeCommandRunner()
    cmd.respond_when(
        _is_clean_merge_rev_parse,
        stdout=f"{_PRE_MERGE_HEAD}\n{_MERGE_COMMIT}\n{_PRE_MERGE_HEAD}\n{'9' * 40}\n",
    )
    cmd.respond_when(
        _is_head_in_base, returncode=128, stderr="fatal: not a valid object name origin/main"
    )

    with structlog.testing.capture_logs() as captured:
        result = await _sync(_runner(tmp_path, cmd))

    assert result.pushed is True
    assert result.merged_base_sha is None
    unresolved = [
        r for r in captured if r.get("event") == "monitor.sync_base_merged_base_unresolved"
    ]
    assert len(unresolved) == 1
    assert unresolved[0]["ref"] == "HEAD^2"
    assert "not a valid object name" in unresolved[0]["stderr"]
