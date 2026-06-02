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
from awf.common.commands import FakeCommandRunner
from awf.common.forge import ForgeNotSupportedError
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import ProfileMonitor, WorkspaceProfile
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
    async def test_legacy_bitbucket_snapshot_fails_via_url_aware_forge_resolver(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # Regression for PR review thread PRRT_kwDOSJAM6s6GRxm-: the release
        # handoff must build its forge client with the URL-aware resolver
        # (``concrete_forge_for_repo``), mirroring the worker PR-monitor factory
        # and the execution_flow forge gate. A *legacy* ``resolved_profile``
        # snapshot predates the ``forge`` field, so the reconstructed
        # ``profile.forge`` is the schema default ``"auto"``. Plain
        # ``concrete_forge("auto")`` normalizes to ``"github"`` and would silently
        # construct a GitHubClient for a BitBucket repo; the URL-aware resolver
        # detects bitbucket from ``workspace.repo_url`` and fails fast with
        # FORGE_NOT_SUPPORTED instead. The early execution_flow gate normally
        # pre-empts this path, so this defense-in-depth case is exercised by
        # invoking the handoff directly (the "slips past the early gate" scenario
        # the handoff's own ForgeNotSupportedError catch exists to cover).
        fake.queue_result(returncode=0)  # git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # git rev-list --count
        fake.queue_result(returncode=0)  # post-setup git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # post-setup git rev-list --count

        # Legacy snapshot: dumped before the ``forge`` field existed, so the key
        # is absent and reconstruction defaults ``profile.forge`` to ``"auto"``.
        legacy_profile = WorkspaceProfile(
            name="legacy-bitbucket",
            source="test",
            monitor=ProfileMonitor(
                initial_review_grace_period_seconds=30,
                non_check_reviewer_settle_seconds=10,
                non_check_reviewer_logins=[],
            ),
        ).model_dump(mode="json")
        legacy_profile.pop("forge", None)

        ws_id = await _seed_ready(
            factory,
            task_kind="sync_release_pr",
            auto_merge=False,
            task_policy=_release_sync_policy(),
            resolved_profile=legacy_profile,
        )

        # Repoint at a BitBucket repo (the unsupported forge under test) and put
        # the workspace in ``running``, the status the handoff expects.
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.repo_url = "git@bitbucket.org:x/y.git"
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED_RUNNING")
            await s.commit()

        def _monitor_factory(*_args: Any, **_kwargs: Any) -> object:
            raise AssertionError("monitor must not run after an unsupported-forge failure")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        # Profile/toolchain setup is orthogonal to the forge resolver under test;
        # stub it so the test stays focused on forge dispatch.
        async def _setup_ok(**_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(executor, "_run_monitor_handoff_profile_setup", _setup_ok)

        worktree_path = executor._config.worktrees_root / ws_id
        worktree_path.mkdir(parents=True, exist_ok=True)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None

        await executor._handoff_sync_release_pr_monitor(
            workspace_id=ws_id,
            workspace=ws,
            compose_project=f"awf_{ws_id}",
            compose_file=executor._config.compose_projects_root / ws_id / "compose.yml",
            worktree_path=worktree_path,
        )

        # Fail-fast forge construction means no gh PR call is ever attempted.
        assert all(c.args[:3] != ["gh", "pr", "create"] for c in fake.calls)
        assert all(c.args[:3] != ["gh", "pr", "list"] for c in fake.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.events[-1].reason_code == "FORGE_NOT_SUPPORTED"
            assert "sync_release_pr failed" in (ws.failure_message or "")
            assert "BitBucket forge support is not yet implemented" in (ws.failure_message or "")
