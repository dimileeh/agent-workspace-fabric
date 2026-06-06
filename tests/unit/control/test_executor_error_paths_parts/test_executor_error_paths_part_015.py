"""Executor forge-support gate coverage for the sync_release_pr handoff.

Split out of ``test_executor_error_paths_part_007`` to keep that module under the
first-party line limit. These cases share the release-sync scaffolding defined in
part 007 (``_make_executor``, ``_seed_ready``, ``_release_sync_policy``) and cover
the defense-in-depth FORGE_NOT_SUPPORTED handling once the early execution_flow
gate is bypassed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.bitbucket_client import BITBUCKET_AUTH_NOT_CONFIGURED, BitBucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.forge import ForgeNotSupportedError
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_007 import (
    _make_executor,
    _release_sync_policy,
    _seed_ready,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


class TestSyncReleasePrHandoffForgeGate:
    @pytest.mark.unit
    async def test_pr_adoption_unsupported_forge_fails_cleanly_before_monitor(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # Defense-in-depth: the early forge gate in execution_flow normally
        # stops an unsupported forge from ever reaching the release handoff, but
        # the ``gh`` client is still constructed via ``make_forge_client`` inside
        # the PR-adoption try-block. If construction raises
        # ``ForgeNotSupportedError`` here, the handoff must mark the workspace
        # failed with FORGE_NOT_SUPPORTED rather than let the error propagate
        # uncaught and strand the workspace in ``running``.
        fake.queue_result(returncode=0)  # git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # git rev-list --count
        fake.queue_result(returncode=0)  # post-setup git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # post-setup git rev-list --count

        def _raise_forge_not_supported(*_args: Any, **_kwargs: Any) -> Any:
            raise ForgeNotSupportedError(
                message="BitBucket forge support is not yet implemented (test)."
            )

        monkeypatch.setattr(
            "awf.control.executor.monitor_handoff.make_forge_client",
            _raise_forge_not_supported,
        )

        ws_id = await _seed_ready(
            factory,
            task_kind="sync_release_pr",
            auto_merge=False,
            task_policy=_release_sync_policy(),
        )

        def _monitor_factory(*_args: Any, **_kwargs: Any) -> object:
            raise AssertionError("monitor must not run after an unsupported-forge failure")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.execute(ws_id)

        assert all(c.args[:3] != ["gh", "pr", "create"] for c in fake.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.events[-1].reason_code == "FORGE_NOT_SUPPORTED"
            assert "sync_release_pr failed" in (ws.failure_message or "")
            assert "BitBucket forge support is not yet implemented" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_pr_adoption_bitbucket_auth_error_fails_cleanly_before_monitor(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # BitBucket is now a supported forge, so ``make_forge_client("bitbucket")``
        # builds the client via ``BitBucketClient.from_env()``, which raises
        # ``BitBucketClientError`` (reason_code BITBUCKET_AUTH_NOT_CONFIGURED) when
        # credentials are missing. The release handoff must map that to a
        # reason-coded workspace failure instead of letting it propagate uncaught
        # and strand the workspace in ``running``.
        fake.queue_result(returncode=0)  # git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # git rev-list --count
        fake.queue_result(returncode=0)  # post-setup git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # post-setup git rev-list --count

        def _raise_bitbucket_auth_error(*_args: Any, **_kwargs: Any) -> Any:
            raise BitBucketClientError(
                operation="bitbucket auth",
                status=None,
                body="BITBUCKET_API_TOKEN is required.",
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )

        monkeypatch.setattr(
            "awf.control.executor.monitor_handoff.make_forge_client",
            _raise_bitbucket_auth_error,
        )

        ws_id = await _seed_ready(
            factory,
            task_kind="sync_release_pr",
            auto_merge=False,
            task_policy=_release_sync_policy(),
        )

        def _monitor_factory(*_args: Any, **_kwargs: Any) -> object:
            raise AssertionError("monitor must not run after a forge auth failure")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.execute(ws_id)

        assert all(c.args[:3] != ["gh", "pr", "create"] for c in fake.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.events[-1].reason_code == BITBUCKET_AUTH_NOT_CONFIGURED
            assert "sync_release_pr failed" in (ws.failure_message or "")

    # NOTE (issue #345 Part 2): the former
    # ``test_legacy_bitbucket_snapshot_fails_via_url_aware_forge_resolver`` was
    # removed. It asserted the release handoff's URL-aware re-gate fails fast with
    # FORGE_NOT_SUPPORTED for a BitBucket repo. Part 2 flips the gate — BitBucket is
    # now supported — so that scenario can no longer occur, and no
    # profile-constructible / URL-detectable forge is unsupported any more (only a
    # genuinely-unknown forge trips the gate, which the schema/URL detection cannot
    # produce). The defense-in-depth FORGE_NOT_SUPPORTED handling stays covered by
    # ``test_pr_adoption_unsupported_forge_fails_cleanly_before_monitor`` above
    # (which stubs the factory to raise ForgeNotSupportedError directly). The
    # BitBucket-construction-error handling gap (consumers catch GitHubClientError /
    # ForgeNotSupportedError, not BitBucketClientError) is now closed for the
    # release handoff by
    # ``test_pr_adoption_bitbucket_auth_error_fails_cleanly_before_monitor`` above.
