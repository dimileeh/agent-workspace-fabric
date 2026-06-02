"""Executor forge-support gate (issue #345 Phase 1).

A BitBucket forge must be detected and fail fast with ``FORGE_NOT_SUPPORTED``
BEFORE the agent runs, before push, and before ``gh pr create`` — never a crash
and never a silent mis-route to GitHub.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.profiles.models import WorkspaceProfile
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = PullRequestCreator(fake)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation,
        pr_creator=pr,
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={AgentRuntime.codex: "gpt-5"},
        ),
    )


def _bitbucket_resolved_profile() -> dict:
    return WorkspaceProfile.model_validate(
        {"name": "p", "docker": {"mode": "none"}, "forge": "bitbucket"}
    ).model_dump(mode="json", by_alias=True)


def _bitbucket_requested_profile() -> dict:
    """An explicit ``forge: bitbucket`` requested/repo-local profile snapshot."""
    return WorkspaceProfile.model_validate(
        {"name": "p", "docker": {"mode": "none"}, "forge": "bitbucket"}
    ).model_dump(mode="json", by_alias=True)


def _legacy_auto_resolved_profile() -> dict:
    """A legacy snapshot that predates the ``forge`` field reconstructs as ``auto``."""
    return WorkspaceProfile.model_validate({"name": "p", "docker": {"mode": "none"}}).model_dump(
        mode="json", by_alias=True
    )


_DEFAULT_RESOLVED_PROFILE = object()


async def _seed_ready_bitbucket_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_kind: str = "feature_branch_pr",
    resolved_profile: object = _DEFAULT_RESOLVED_PROFILE,
    repo_url: str = "git@bitbucket.org:workspace/repo.git",
    requested_profile: dict | None = None,
) -> str:
    snapshot = (
        _bitbucket_resolved_profile()
        if resolved_profile is _DEFAULT_RESOLVED_PROFILE
        else resolved_profile
    )
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=repo_url,
            branch_base="development",
            task_title="trivial",
            task_prompt="Add a docstring.",
            agent="codex",
            test_commands=["pytest -q"],
            requested_profile=requested_profile,
            resolved_profile=snapshot,  # type: ignore[arg-type]
            task_policy={},
            task_kind=task_kind,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await s.commit()
        (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


@pytest.mark.unit
@pytest.mark.parametrize("task_kind", ["feature_branch_pr", "sync_release_pr"])
async def test_bitbucket_workspace_fails_fast_with_forge_not_supported(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
    fake: FakeCommandRunner,
    task_kind: str,
) -> None:
    # The gate fires BEFORE non-feature dispatch, so sync_release_pr (whose
    # monitor handoff would otherwise raise an uncaught ForgeNotSupportedError
    # and strand the workspace in ``running``) also fails fast cleanly.
    ws_id = await _seed_ready_bitbucket_workspace(factory, task_kind=task_kind)

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "BitBucket forge support is not yet implemented" in (ws.failure_message or "")
        failed_event = next(
            event
            for event in reversed(ws.events)
            if event.event_type == "workspace.state_changed"
            and event.new_state == WorkspaceStatus.failed.value
        )
        assert failed_event.reason_code == "FORGE_NOT_SUPPORTED"

    # Crash/mis-route is explicitly not allowed: no GitHub PR creation runs.
    assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls), (
        "BitBucket workspace must fail before any gh pr create"
    )


@pytest.mark.unit
async def test_explicit_bitbucket_requested_profile_fails_fast_without_snapshot(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
    fake: FakeCommandRunner,
) -> None:
    # The gap the pre-resolution gate misses: a ready workspace with NO
    # resolved_profile snapshot and a GitHub ``repo_url`` (URL detection → github),
    # but whose requested/repo-local profile *explicitly* sets ``forge: bitbucket``.
    # The pre-resolution gate only sees the absent snapshot plus repo_url and returns
    # github, so the executor resolves+persists the explicit bitbucket forge (the
    # requested profile is consulted precisely because the snapshot is missing) and
    # would slip into the agent/push/gh pr create path. Re-gating the just-resolved
    # profile must fail fast with FORGE_NOT_SUPPORTED before any of that runs.
    ws_id = await _seed_ready_bitbucket_workspace(
        factory,
        resolved_profile=None,
        repo_url="https://github.com/owner/repo.git",
        requested_profile=_bitbucket_requested_profile(),
    )

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "BitBucket forge support is not yet implemented" in (ws.failure_message or "")
        failed_event = next(
            event
            for event in reversed(ws.events)
            if event.event_type == "workspace.state_changed"
            and event.new_state == WorkspaceStatus.failed.value
        )
        assert failed_event.reason_code == "FORGE_NOT_SUPPORTED"

    assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls), (
        "explicit-bitbucket workspace must fail before any gh pr create"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "resolved_profile",
    [
        pytest.param(None, id="missing-snapshot"),
        pytest.param(_legacy_auto_resolved_profile(), id="legacy-auto-snapshot"),
    ],
)
async def test_bitbucket_repo_url_fails_fast_when_snapshot_omits_concrete_forge(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
    fake: FakeCommandRunner,
    resolved_profile: dict | None,
) -> None:
    # A ready workspace whose ``resolved_profile`` snapshot is missing (None) or
    # legacy (forge reconstructs as ``auto``) still resolves+persists a profile
    # from ``repo_url`` before running. The gate must detect the BitBucket forge
    # from the repo URL rather than default the absent forge to GitHub — otherwise
    # the workspace slips past the fail-fast point into the agent/push path.
    ws_id = await _seed_ready_bitbucket_workspace(factory, resolved_profile=resolved_profile)

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "BitBucket forge support is not yet implemented" in (ws.failure_message or "")
        failed_event = next(
            event
            for event in reversed(ws.events)
            if event.event_type == "workspace.state_changed"
            and event.new_state == WorkspaceStatus.failed.value
        )
        assert failed_event.reason_code == "FORGE_NOT_SUPPORTED"

    assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls), (
        "BitBucket workspace must fail before any gh pr create"
    )
