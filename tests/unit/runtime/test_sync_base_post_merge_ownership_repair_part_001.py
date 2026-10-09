"""Base-sync merge commits must re-arm agent ownership of the shared mirror (#1033).

A clean sync-base merge is a root-side ref write into the shared bare mirror's
``refs/heads/<namespace>/`` (plus its reflog). If git had packed that namespace's
loose refs away, the write recreates the directory ``755 root:root`` — after the
attempt's ownership repair already ran — and the agent's next ``git commit``
fails with ``EACCES`` on the ref lock. The monitor therefore re-runs the cheap
refs/logs repair immediately after the merge commit, before the push.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor_runner import remote_ops
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult


async def _open_pr_post_action_recheck(**_kwargs: object) -> _GitPushResult | None:
    """#910 post-action PR re-check stub; this PR stays OPEN."""
    return None


class _FakeCommandRunner:
    """Record the git commands sync-base runs; every one succeeds."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def run(
        self,
        args: list[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run a fake git command."""
        del env
        if "merge" in args:
            self._events.append("git:" + " ".join(args[args.index("merge") :]))
        return CommandResult(returncode=0, stdout="", stderr="")


async def _repair_operation_start_head_result(
    *,
    workspace_id: str,
    worktree_path: Path,
    operation_type: str,
    fallback_head_sha: str | None = None,
) -> tuple[str, None]:
    """Stub the operation start-head anchor."""
    del workspace_id, worktree_path, operation_type, fallback_head_sha
    return "operation-start-sha", None


async def _resolve_task_tag(_workspace_id: str) -> str | None:
    """No Jira task tag in these tests."""
    return None


def _runner(tmp_path: Path, events: list[str], **overrides: object) -> SimpleNamespace:
    """Build the minimal sync-base runner surface."""

    async def _fetch_base(**_kwargs: object) -> None:
        events.append("fetch-base")

    async def _protected_scope_push_block(**_kwargs: object) -> None:
        events.append("protected-scope")

    async def _validated_git_push_result(**_kwargs: object) -> _GitPushResult:
        events.append("validated-push")
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    surface: dict[str, object] = {
        "_worktrees_root": tmp_path,
        "_repair_operation_start_head_result": _repair_operation_start_head_result,
        "_resolve_task_tag": _resolve_task_tag,
        "_fetch_base": _fetch_base,
        "_protected_scope_push_block": _protected_scope_push_block,
        "_post_action_pr_terminal_push_result_if_moot": _open_pr_post_action_recheck,
        "_validated_git_push_result": _validated_git_push_result,
        "_deps": SimpleNamespace(runner=_FakeCommandRunner(events)),
    }
    surface.update(overrides)
    return SimpleNamespace(**surface)


async def _run_sync_base(runner: SimpleNamespace, tmp_path: Path) -> _GitPushResult:
    """Drive ``_run_sync_base`` with a fixed, uninteresting PR shape."""
    return await remote_ops._run_sync_base(  # noqa: SLF001
        runner,
        workspace_id="ws-sync",
        repo=SimpleNamespace(slug=lambda: "owner/repo"),
        pr_number=614,
        base_branch="main",
        remote_branch="awf/ws-sync",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )


@pytest.mark.unit
async def test_clean_merge_repairs_mirror_ref_ownership_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge commit's ref write is followed by an ownership repair."""
    events: list[str] = []
    repair_kwargs: list[dict[str, object]] = []

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        repair_kwargs.append(kwargs)
        events.append("ownership-repair")
        return True

    monkeypatch.setattr(
        remote_ops,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    result = await _run_sync_base(_runner(tmp_path, events), tmp_path)

    assert result.pushed is True
    assert events == [
        "git:merge --abort",
        "fetch-base",
        "git:merge --no-edit origin/main",
        "ownership-repair",
        "protected-scope",
        "validated-push",
    ]
    assert [kwargs["reason"] for kwargs in repair_kwargs] == ["sync_base_post_merge_ref_write"]
    assert repair_kwargs[0]["workspace_id"] == "ws-sync"
    assert repair_kwargs[0]["worktree_path"] == tmp_path / "ws-sync"


@pytest.mark.unit
async def test_failed_post_merge_ownership_repair_blocks_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed repair is reason-coded and terminal, never pushed over."""
    events: list[str] = []

    async def _failing_repair(**_kwargs: object) -> bool:
        events.append("ownership-repair")
        return False

    async def _unexpected_protected_scope(**_kwargs: object) -> None:
        pytest.fail("sync-base must stop before protected-scope checks")

    async def _unexpected_validated_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("sync-base must not push with an unrepaired shared mirror")

    monkeypatch.setattr(remote_ops, "repair_agent_runtime_ownership", _failing_repair)

    result = await _run_sync_base(
        _runner(
            tmp_path,
            events,
            _protected_scope_push_block=_unexpected_protected_scope,
            _validated_git_push_result=_unexpected_validated_push,
        ),
        tmp_path,
    )

    assert result.pushed is False
    assert result.failed is True
    assert result.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert "after the sync-base merge" in (result.stderr or "")
    assert events == [
        "git:merge --abort",
        "fetch-base",
        "git:merge --no-edit origin/main",
        "ownership-repair",
    ]
