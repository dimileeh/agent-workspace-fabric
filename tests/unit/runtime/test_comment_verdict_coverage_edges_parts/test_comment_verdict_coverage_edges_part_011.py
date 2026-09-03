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
