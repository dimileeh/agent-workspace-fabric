"""Protocol-retry rollback: restore trusted Git config before cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import AsyncioSubprocessRunner
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protocol_retry_rollback_restores_excludesfile_before_cleanup(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e0yQG: restore trusted config before cleaning ignored residue.

    When a correction sets ``core.excludesFile`` to an agent-created file under
    ``.git`` and creates a matching untracked path, cleanup without ``-x`` leaves
    the path while the exclusion is active. Restoring the snapshot *after*
    cleanup would re-expose those bytes as untracked with no further check.
    """
    from awf.runtime.pr_monitor_runner import (
        comment_verdict,
        comment_verdict_residue_fingerprint,
    )

    worktree = tmp_path / "ws_excludesfile_order"
    worktree.mkdir()
    init_git_worktree(worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert comment_verdict_residue_fingerprint.remember_item_start_local_git_configs(worktree)

    excludes = worktree / ".git" / "agent-excludes"
    excludes.write_text("poisoned-residue.txt\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "--local", "core.excludesFile", str(excludes)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    residue = worktree / "poisoned-residue.txt"
    residue.write_text("rejected-correction-bytes\n", encoding="utf-8")

    # Under the poisoned excludesFile the path is ignored (not cleaned by -ffd).
    ignored = subprocess.run(
        ["git", "status", "--porcelain", "--ignored", "--", "poisoned-residue.txt"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "!!" in ignored or ignored.strip().startswith("!!")

    async def _rev_parse_head(_path: Path) -> str:
        return head

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=AsyncioSubprocessRunner(),
        ),
        _rev_parse_head=_rev_parse_head,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_excludesfile_order",
        worktree_path=worktree,
        item_start_head=head,
        state=None,
    )

    get_excludes = subprocess.run(
        ["git", "config", "--local", "--get", "core.excludesFile"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert get_excludes.returncode != 0
    assert not residue.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_item_start_git_config_snapshot_after_hooks_path_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e0yQN: snapshot after hook repair so rollback cannot re-poison.

    Pre-launch repair clears a poisoned ``core.hooksPath``. If the item-start
    local-config snapshot is taken before that repair, non-FIXED rollback
    restores the executable hook path the safety repair just removed.
    """
    from awf.adapters.base import AgentRunResult
    from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_rollback

    worktree = tmp_path / "ws_hooks_snapshot_order"
    worktree.mkdir()
    init_git_worktree(worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    poisoned_hooks = tmp_path / "poisoned-hooks"
    poisoned_hooks.mkdir()
    hook_script = poisoned_hooks / "pre-commit"
    hook_script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook_script.chmod(0o755)
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", str(poisoned_hooks)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    assert subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == str(poisoned_hooks)

    mirror_path = tmp_path / "mirror.git"
    mirror_path.mkdir()
    call_order: list[str] = []
    hooks_present_at_snapshot: list[bool] = []

    async def _ok_ownership(**_kwargs: object) -> bool:
        return True

    async def _repair_mirror_hooks_path(_path: Path) -> bool:
        call_order.append("repair")
        unset = subprocess.run(
            ["git", "config", "--local", "--unset-all", "core.hooksPath"],
            cwd=worktree,
            capture_output=True,
        )
        assert unset.returncode == 0
        return True

    real_remember = comment_verdict.remember_item_start_local_git_configs

    def _remember(path: Path) -> bool:
        call_order.append("remember")
        probe = subprocess.run(
            ["git", "config", "--local", "--get", "core.hooksPath"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        hooks_present_at_snapshot.append(probe.returncode == 0)
        return real_remember(path)

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok_ownership)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: mirror_path)
    monkeypatch.setattr(
        comment_verdict_rollback,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _remember)

    async def _rev_parse_head(_path: Path) -> str:
        return head

    async def _run_agent(**_kwargs: object) -> AgentRunResult:
        # Re-poison after launch so rollback would restore poison if the
        # item-start snapshot captured the pre-repair baseline.
        subprocess.run(
            ["git", "config", "--local", "core.hooksPath", str(poisoned_hooks)],
            cwd=worktree,
            check=True,
            capture_output=True,
        )
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FALSE POSITIVE: pre-existing behavior is correct",
            stderr="",
        )

    async def _commit_dirty(**_kwargs: object) -> bool:
        return False

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _workspace_runtime_context="",
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=AsyncioSubprocessRunner(),
        ),
        _rev_parse_head=_rev_parse_head,
        _run_monitor_agent_with_service_recovery=_run_agent,
        _commit_dirty_worktree=_commit_dirty,
        _provider_recovery_suppresses_cli=lambda _ws: _async_false(),
        _resolve_task_tag=lambda _ws: _async_none(),
        _handle_provider_agent_run_error=_noop_provider_error,
    )

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_hooks_snapshot_order",
        prompt="review item",
        commit_message="fix: review item",
        compose_project="awf_ws_hooks",
        compose_file=tmp_path / "compose.yml",
        operation_start_head=head,
        require_fix_evidence=False,
        commit_dirty_changes=False,
    )

    assert call_order == ["repair", "remember"]
    assert hooks_present_at_snapshot == [False]
    assert result.verdict == "false_positive"

    get_hooks = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert get_hooks.returncode != 0


async def _async_false() -> bool:
    return False


async def _async_none() -> None:
    return None


async def _noop_provider_error(*_args: object, **_kwargs: object) -> None:
    return None
