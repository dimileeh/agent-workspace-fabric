"""Post-push reuse tip-resolution coverage split from part_004."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.forge_lifecycle import PullRequestLifecycle, PullRequestSnapshot
from awf.control.executor import pr_open_step as _pr_open_step
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_004 import (
    _LifecycleForgeClient,
)


class TestPostPushReuseTipResolution:
    @pytest.mark.unit
    async def test_snapshot_contains_pushed_tip_equality(self) -> None:
        tip = "Ab" + ("c" * 38)
        assert await _pr_open_step._pr_snapshot_contains_pushed_tip(
            snapshot=PullRequestSnapshot(
                PullRequestLifecycle.open,
                "head",
                head_sha=tip.lower(),
            ),
            pushed_head_sha=tip,
        )
        assert not await _pr_open_step._pr_snapshot_contains_pushed_tip(
            snapshot=PullRequestSnapshot(PullRequestLifecycle.open, "head", head_sha=""),
            pushed_head_sha=tip,
        )
        assert not await _pr_open_step._pr_snapshot_contains_pushed_tip(
            snapshot=PullRequestSnapshot(PullRequestLifecycle.open, "head", head_sha=tip),
            pushed_head_sha=None,
        )

    @pytest.mark.unit
    async def test_snapshot_contains_pushed_tip_when_live_head_descends(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tip = "b" * 40
        descendant = "d" * 40
        runner = FakeCommandRunner()
        # Live tip object missing locally → fetch, then ancestor check succeeds.
        runner.queue_result(returncode=1)  # cat-file miss
        runner.queue_result(returncode=0)  # fetch descendant
        runner.queue_result(returncode=0)  # merge-base --is-ancestor

        assert await _pr_open_step._pr_snapshot_contains_pushed_tip(
            snapshot=PullRequestSnapshot(
                PullRequestLifecycle.open,
                "head",
                head_sha=descendant,
            ),
            pushed_head_sha=tip,
            runner=runner,
            worktree_path=tmp_path,
            fetch_remote="origin",
        )
        assert any("cat-file" in call.args for call in runner.calls)
        assert any("fetch" in call.args for call in runner.calls)
        assert any(
            "merge-base" in call.args and "--is-ancestor" in call.args for call in runner.calls
        )

    @pytest.mark.unit
    async def test_snapshot_rejects_when_descendant_fetch_fails(
        self,
        tmp_path: Path,
    ) -> None:
        tip = "b" * 40
        descendant = "d" * 40
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1)  # cat-file miss
        runner.queue_result(returncode=1)  # fetch fails

        assert not await _pr_open_step._pr_snapshot_contains_pushed_tip(
            snapshot=PullRequestSnapshot(
                PullRequestLifecycle.open,
                "head",
                head_sha=descendant,
            ),
            pushed_head_sha=tip,
            runner=runner,
            worktree_path=tmp_path,
        )
        assert any("fetch" in call.args for call in runner.calls)
        assert not any("merge-base" in call.args for call in runner.calls)

    @pytest.mark.unit
    async def test_snapshot_rejects_when_live_head_does_not_descend(
        self,
        tmp_path: Path,
    ) -> None:
        tip = "b" * 40
        rewritten = "c" * 40
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0)  # cat-file hit
        runner.queue_result(returncode=1)  # not an ancestor (force-push / rewrite)

        assert not await _pr_open_step._pr_snapshot_contains_pushed_tip(
            snapshot=PullRequestSnapshot(
                PullRequestLifecycle.open,
                "head",
                head_sha=rewritten,
            ),
            pushed_head_sha=tip,
            runner=runner,
            worktree_path=tmp_path,
        )

    @pytest.mark.unit
    async def test_resolve_keeps_open_when_local_tip_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        forge = _LifecycleForgeClient(lifecycle=PullRequestLifecycle.open, head_sha="a" * 40)
        monkeypatch.setattr(_pr_open_step, "_POST_PUSH_TIP_RETRY_DELAY_SECONDS", 0.0)
        disposition, snapshot = await _pr_open_step._resolve_post_push_reuse(
            forge_client=forge,
            repo=SimpleNamespace(),  # unused when no retry
            pr_number=1,
            snapshot=PullRequestSnapshot(PullRequestLifecycle.open, "head", head_sha="a" * 40),
            pushed_head_sha=None,
        )
        assert disposition is _pr_open_step._PostPushReuseDisposition.keep
        assert snapshot.lifecycle is PullRequestLifecycle.open
        assert forge.snapshot_calls == 0

    @pytest.mark.unit
    async def test_resolve_keeps_open_when_live_head_is_descendant(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tip = "b" * 40
        descendant = "d" * 40
        forge = _LifecycleForgeClient(lifecycle=PullRequestLifecycle.open, head_sha=descendant)

        async def _descends(**_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(_pr_open_step, "_live_head_descends_from_pushed", _descends)
        disposition, snapshot = await _pr_open_step._resolve_post_push_reuse(
            forge_client=forge,
            repo=SimpleNamespace(),
            pr_number=1,
            snapshot=PullRequestSnapshot(
                PullRequestLifecycle.open,
                "head",
                head_sha=descendant,
            ),
            pushed_head_sha=tip,
            runner=FakeCommandRunner(),
            worktree_path=tmp_path,
        )
        assert disposition is _pr_open_step._PostPushReuseDisposition.keep
        assert snapshot.head_sha == descendant
        assert forge.snapshot_calls == 0

    @pytest.mark.unit
    async def test_resolve_keeps_merged_when_live_head_is_descendant(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tip = "b" * 40
        descendant = "d" * 40

        async def _descends(**_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(_pr_open_step, "_live_head_descends_from_pushed", _descends)
        disposition, snapshot = await _pr_open_step._resolve_post_push_reuse(
            forge_client=_LifecycleForgeClient(),
            repo=SimpleNamespace(),
            pr_number=1,
            snapshot=PullRequestSnapshot(
                PullRequestLifecycle.merged,
                "head",
                head_sha=descendant,
            ),
            pushed_head_sha=tip,
            runner=FakeCommandRunner(),
            worktree_path=tmp_path,
        )
        assert disposition is _pr_open_step._PostPushReuseDisposition.keep
        assert snapshot.lifecycle is PullRequestLifecycle.merged

    @pytest.mark.unit
    async def test_resolve_replaces_when_retry_sees_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        forge = _LifecycleForgeClient(
            snapshots=[
                PullRequestSnapshot(PullRequestLifecycle.closed, "head", head_sha="c" * 40),
            ]
        )

        async def _no_delay(_seconds: float) -> None:
            return None

        monkeypatch.setattr(_pr_open_step, "_POST_PUSH_TIP_RETRY_DELAY_SECONDS", 0.0)
        monkeypatch.setattr(_pr_open_step.asyncio, "sleep", _no_delay)
        disposition, snapshot = await _pr_open_step._resolve_post_push_reuse(
            forge_client=forge,
            repo=SimpleNamespace(),
            pr_number=1,
            snapshot=PullRequestSnapshot(
                PullRequestLifecycle.open,
                "head",
                head_sha="a" * 40,
            ),
            pushed_head_sha="b" * 40,
        )
        assert disposition is _pr_open_step._PostPushReuseDisposition.open_replacement
        assert snapshot.lifecycle is PullRequestLifecycle.closed
        assert forge.snapshot_calls == 1
