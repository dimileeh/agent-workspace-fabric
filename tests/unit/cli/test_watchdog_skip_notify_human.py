"""Tests for the NotifyHuman-aware respawn-skip in the stranded-PR watchdog.

PR #12 added the watchdog: every poll interval, it lists open ``awf/`` PRs
and respawns ``attach_feature_pr_monitor.py`` for any PR with no live
``run_awf.py`` process. That fix is correct for crash recovery, but it
respawned on EVERY missing-process scan — even when the previous monitor
exited intentionally with ``NotifyHuman`` (release PR with ``auto_merge=False``,
or a deferred-bot-comment-only situation). The agent kept being woken up
to address the same comments and re-defer them, in an infinite cycle.

The fix consults the defer-signal artifact (PR #9): when the LATEST
artifact for this PR shows ``terminal_action == "NotifyHuman"`` AND the
PR's current head_sha matches the artifact's recorded head_sha, skip the
respawn. New commits or any other terminal action falls through to
respawn — preserving crash recovery as the primary purpose of the
watchdog.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from awf.cli.watchdog import _should_skip_respawn


def _write_artifact(
    artifacts_root: Path,
    *,
    workspace_id: str,
    repo: str,
    pr_number: int,
    terminal_action: str,
    head_sha: str = "abc1234567890def",
    mtime: float | None = None,
) -> Path:
    """Helper: drop a defer-signal JSON exactly like the runner does."""
    artifacts_root.mkdir(parents=True, exist_ok=True)
    path = artifacts_root / f"{workspace_id}.defer-signal.json"
    payload = {
        "workspace_id": workspace_id,
        "repo": repo,
        "pr_number": pr_number,
        "terminal_action": terminal_action,
        "head_sha": head_sha,
        "merged": terminal_action == "Merge",
        "deferred_bot_items": [],
        "deferred_human_items": [],
    }
    path.write_text(json.dumps(payload))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestShouldSkipRespawn:
    def test_no_artifact_means_respawn(self, tmp_path: Path) -> None:
        """Empty artifacts dir → respawn (covers crash recovery — the
        primary purpose of the watchdog)."""
        artifacts_root = tmp_path / "artifacts"
        artifacts_root.mkdir()
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=artifacts_root,
            )
            is False
        )

    def test_missing_artifacts_dir_means_respawn(self, tmp_path: Path) -> None:
        """Even the artifacts directory may not exist yet (fresh host)."""
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=tmp_path / "does-not-exist",
            )
            is False
        )

    def test_notify_human_terminal_with_same_sha_skips(
        self, tmp_path: Path, caplog
    ) -> None:
        """The whole point of the fix: PR has a NotifyHuman artifact AND
        the head_sha hasn't moved → don't respawn."""
        artifacts_root = tmp_path / "artifacts"
        head = "deadbeefcafe1234"
        _write_artifact(
            artifacts_root,
            workspace_id="ws_abc",
            repo="dimileeh/aira-agent",
            pr_number=355,
            terminal_action="NotifyHuman",
            head_sha=head,
        )
        with caplog.at_level(logging.INFO):
            result = _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha=head,
                artifacts_root=artifacts_root,
            )
        assert result is True
        # Operator should be able to grep for this exact event.
        assert any(
            "watchdog.skipped_notify_human_terminal" in rec.getMessage()
            or "watchdog.skipped_notify_human_terminal" in str(rec.__dict__)
            for rec in caplog.records
        ), "expected watchdog.skipped_notify_human_terminal log line"

    def test_notify_human_terminal_with_different_sha_respawns(
        self, tmp_path: Path
    ) -> None:
        """New commits arrived since the NotifyHuman → re-engage."""
        artifacts_root = tmp_path / "artifacts"
        _write_artifact(
            artifacts_root,
            workspace_id="ws_abc",
            repo="dimileeh/aira-agent",
            pr_number=355,
            terminal_action="NotifyHuman",
            head_sha="oldsha0000000000",
        )
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="newsha1111111111",
                artifacts_root=artifacts_root,
            )
            is False
        )

    def test_merge_terminal_does_not_skip(self, tmp_path: Path) -> None:
        """A previously-merged PR being re-opened (or stale artifact for
        a different PR life cycle) should NOT silence the watchdog."""
        artifacts_root = tmp_path / "artifacts"
        _write_artifact(
            artifacts_root,
            workspace_id="ws_abc",
            repo="dimileeh/aira-agent",
            pr_number=355,
            terminal_action="Merge",
        )
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=artifacts_root,
            )
            is False
        )

    def test_abort_terminal_does_not_skip(self, tmp_path: Path) -> None:
        """Abort = upstream invariant violation / closed PR. The watchdog
        should still respawn because the PR is somehow open AND in an AWF
        state — the operator wants visibility."""
        artifacts_root = tmp_path / "artifacts"
        _write_artifact(
            artifacts_root,
            workspace_id="ws_abc",
            repo="dimileeh/aira-agent",
            pr_number=355,
            terminal_action="Abort",
        )
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=artifacts_root,
            )
            is False
        )

    def test_short_circuit_does_not_skip(self, tmp_path: Path) -> None:
        """ShortCircuitCompleted = PR was merged externally. If it's still
        open, the artifact is stale and respawn is correct."""
        artifacts_root = tmp_path / "artifacts"
        _write_artifact(
            artifacts_root,
            workspace_id="ws_abc",
            repo="dimileeh/aira-agent",
            pr_number=355,
            terminal_action="ShortCircuitCompleted",
        )
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=artifacts_root,
            )
            is False
        )

    def test_corrupted_artifact_treated_as_missing(self, tmp_path: Path) -> None:
        """A truncated / non-JSON artifact must not crash the watchdog —
        fall through to respawn."""
        artifacts_root = tmp_path / "artifacts"
        artifacts_root.mkdir()
        (artifacts_root / "ws_abc.defer-signal.json").write_text("{not valid json")
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=artifacts_root,
            )
            is False
        )

    def test_artifact_for_different_pr_is_ignored(self, tmp_path: Path) -> None:
        """A NotifyHuman artifact for PR 999 must NOT silence the watchdog
        when scanning PR 355 (cross-PR contamination guard)."""
        artifacts_root = tmp_path / "artifacts"
        _write_artifact(
            artifacts_root,
            workspace_id="ws_other",
            repo="dimileeh/aira-agent",
            pr_number=999,
            terminal_action="NotifyHuman",
        )
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=artifacts_root,
            )
            is False
        )

    def test_artifact_for_different_repo_is_ignored(self, tmp_path: Path) -> None:
        """PR number 100 in repo A and repo B can collide. The repo field
        is what disambiguates."""
        artifacts_root = tmp_path / "artifacts"
        _write_artifact(
            artifacts_root,
            workspace_id="ws_other",
            repo="dimileeh/aira-web",
            pr_number=355,
            terminal_action="NotifyHuman",
        )
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",  # different repo, same PR number
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=artifacts_root,
            )
            is False
        )

    def test_picks_latest_artifact_when_multiple(self, tmp_path: Path) -> None:
        """Two workspaces have driven PR 355 over its lifetime. The most
        recent artifact wins — older NotifyHuman should not silence a
        newer Abort, for example."""
        artifacts_root = tmp_path / "artifacts"
        now = time.time()
        # Older: NotifyHuman (would skip if it won)
        _write_artifact(
            artifacts_root,
            workspace_id="ws_old",
            repo="dimileeh/aira-agent",
            pr_number=355,
            terminal_action="NotifyHuman",
            head_sha="abc1234567890def",
            mtime=now - 3600,
        )
        # Newer: Abort (should NOT skip)
        _write_artifact(
            artifacts_root,
            workspace_id="ws_new",
            repo="dimileeh/aira-agent",
            pr_number=355,
            terminal_action="Abort",
            head_sha="abc1234567890def",
            mtime=now,
        )
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=artifacts_root,
            )
            is False
        )

    def test_picks_latest_artifact_when_multiple_notify_human(
        self, tmp_path: Path
    ) -> None:
        """Symmetric counter-test: newest IS NotifyHuman → skip wins."""
        artifacts_root = tmp_path / "artifacts"
        now = time.time()
        _write_artifact(
            artifacts_root,
            workspace_id="ws_old",
            repo="dimileeh/aira-agent",
            pr_number=355,
            terminal_action="Abort",
            head_sha="abc1234567890def",
            mtime=now - 3600,
        )
        _write_artifact(
            artifacts_root,
            workspace_id="ws_new",
            repo="dimileeh/aira-agent",
            pr_number=355,
            terminal_action="NotifyHuman",
            head_sha="abc1234567890def",
            mtime=now,
        )
        assert (
            _should_skip_respawn(
                owner="dimileeh",
                repo="aira-agent",
                pr_number=355,
                current_head_sha="abc1234567890def",
                artifacts_root=artifacts_root,
            )
            is True
        )
