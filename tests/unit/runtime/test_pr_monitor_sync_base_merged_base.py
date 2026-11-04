"""``_run_sync_base`` records the base SHA it actually merged (#1167).

``sync_base_push`` audit events used to report ``workspace.base_commit`` — the
base recorded at adoption — which made a correct live-base merge look stale.
The sync-base envelope now carries the merged base: ``MERGE_HEAD`` on a conflict
(worktree-local and exact) and ``origin/<base>`` right after a clean merge.
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


@pytest.fixture
def _ownership_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_ops, "repair_agent_runtime_ownership", _ok)


@pytest.mark.unit
@pytest.mark.usefixtures("_ownership_ok")
async def test_clean_merge_reports_origin_base_resolved_at_merge_time(tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    cmd.respond_when(_is_rev_parse("origin/main"), stdout=f"{_MERGED_BASE}\n")

    result = await _sync(_runner(tmp_path, cmd))

    assert result.pushed is True
    assert result.merged_base_sha == _MERGED_BASE
    merge_index = next(
        i for i, c in enumerate(cmd.calls) if "merge" in c.args and "--no-edit" in c.args
    )
    rev_parse_index = next(
        i for i, c in enumerate(cmd.calls) if c.args[-2:] == ["rev-parse", "origin/main"]
    )
    assert rev_parse_index > merge_index


@pytest.mark.unit
@pytest.mark.usefixtures("_ownership_ok")
async def test_conflict_merge_reports_merge_head(tmp_path: Path) -> None:
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
    cmd = FakeCommandRunner()
    cmd.respond_when(_is_rev_parse("origin/main"), returncode=128, stderr="bad ref")

    with structlog.testing.capture_logs() as captured:
        result = await _sync(_runner(tmp_path, cmd))

    assert result.pushed is True
    assert result.merged_base_sha is None
    assert any(r.get("event") == "monitor.sync_base_merged_base_unresolved" for r in captured)
