"""Provisioner execution-claim epoch fencing tests (issue #421, D4/D7).

Exercises the lease-fencing path threaded through ``provision_claimed``:

- D4: the async pre-launch epoch verify aborts a fenced worker before the stack
  launch, without transitioning the workspace.
- D7: the terminal ``provisioning -> ready`` / ``-> failed`` transitions are
  epoch-CAS'd, so a fenced worker updates 0 rows and can neither steal the row
  to ``ready`` nor force it to ``failed``.
- The ``execution_claim_epoch=None`` path keeps the legacy behavior.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeProjectPaths
from awf.node.git_manager import GitManager, GitOperationError
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.postgres import postgres_test_engine


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def git_manager(tmp_path: Path) -> GitManager:
    return GitManager(tmp_path / "awf-work")


class _RecordingStackLauncher:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def launch(self, request: Any) -> object:
        self.requests.append(request)
        return ComposeProjectPaths(
            project_dir=Path("/tmp/awf-compose/ws_launcher"),
            compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
        )


async def _create_provisioning(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
    *,
    epoch: int,
    owner: str = "control-worker-current",
) -> str:
    """Create a workspace already claimed into ``provisioning`` at ``epoch``."""
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="fencing",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(
            ws,
            to=WorkspaceStatus.provisioning,
            reason_code="WORKER_CLAIMED",
        )
        ws.execution_claimed_by = owner
        ws.execution_claim_epoch = epoch
        await s.commit()
        return ws.id


async def _reload(session_factory: async_sessionmaker[AsyncSession], workspace_id: str) -> Any:
    async with session_factory() as s:
        return await WorkspaceRepository(s).get(workspace_id)


async def _advance_epoch(
    session_factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    """Simulate a later claimant reclaiming the row (epoch += 1)."""
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.execution_claim_epoch = ws.execution_claim_epoch + 1
        ws.execution_claimed_by = "control-worker-newer"
        await s.commit()


@pytest.mark.unit
async def test_fenced_worker_aborts_before_launch_without_transitioning(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
) -> None:
    # The row's epoch is already 2 (a later claimant), but this worker was
    # dispatched at epoch 1 -> the pre-launch verify fences it (D4).
    ws_id = await _create_provisioning(session_factory, origin_repo, epoch=2)
    launcher = _RecordingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )

    await provisioner.provision_claimed(ws_id, execution_claim_epoch=1)

    assert launcher.requests == []
    reloaded = await _reload(session_factory, ws_id)
    assert reloaded is not None
    # Aborted without transitioning: still provisioning, claim untouched.
    assert reloaded.status == WorkspaceStatus.provisioning.value
    assert reloaded.execution_claim_epoch == 2
    # The fenced worker never wrote to the row, so the existing claim is intact.
    assert reloaded.execution_claimed_by == "control-worker-current"


@pytest.mark.unit
async def test_matching_epoch_transitions_to_ready_with_metadata_intact(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
) -> None:
    ws_id = await _create_provisioning(session_factory, origin_repo, epoch=1)
    launcher = _RecordingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )

    await provisioner.provision_claimed(ws_id, execution_claim_epoch=1)

    assert len(launcher.requests) == 1
    reloaded = await _reload(session_factory, ws_id)
    assert reloaded is not None
    assert reloaded.status == WorkspaceStatus.ready.value
    # Clobber assertion: the epoch-CAS ready transition keeps placement metadata.
    assert reloaded.node_id == "test-node-01"
    assert reloaded.branch_name == f"awf/{ws_id}"
    assert reloaded.base_commit is not None
    assert reloaded.compose_project_name == f"awf_{ws_id}"
    assert reloaded.compose_file_path == "/tmp/awf-compose/ws_launcher/compose.yml"
    assert reloaded.execution_claim_epoch == 1


@pytest.mark.unit
async def test_fenced_ready_transition_updates_zero_rows_cannot_steal(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
) -> None:
    # The pre-launch verify passes (epoch still 1 at that point), but a later
    # claimant reclaims during launch (epoch -> 2). The terminal ready CAS then
    # updates 0 rows, so this worker cannot steal the row to ready (D7).
    ws_id = await _create_provisioning(session_factory, origin_repo, epoch=1)

    class _ReclaimingStackLauncher(_RecordingStackLauncher):
        async def launch(self, request: Any) -> object:
            await _advance_epoch(session_factory, request.workspace_id)
            return await super().launch(request)

    launcher = _ReclaimingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )

    await provisioner.provision_claimed(ws_id, execution_claim_epoch=1)

    assert len(launcher.requests) == 1
    reloaded = await _reload(session_factory, ws_id)
    assert reloaded is not None
    # Did NOT steal the row to ready; the newer claimant's epoch survives.
    assert reloaded.status == WorkspaceStatus.provisioning.value
    assert reloaded.execution_claim_epoch == 2
    assert reloaded.execution_claimed_by == "control-worker-newer"


class _FailingGit:
    def __init__(self, *, advance: bool, session_factory: Any, work_dir: Path) -> None:
        self._advance = advance
        self._session_factory = session_factory
        self.work_dir = work_dir

    async def add_worktree(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        base_branch: str,
        new_branch: str,
    ) -> object:
        del repo_url, base_branch, new_branch
        if self._advance:
            await _advance_epoch(self._session_factory, workspace_id)
        raise GitOperationError(
            operation="worktree add",
            returncode=128,
            stdout="",
            stderr="boom",
        )


@pytest.mark.unit
async def test_mark_failed_with_matching_epoch_populates_failure(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    origin_repo: Path,
) -> None:
    ws_id = await _create_provisioning(session_factory, origin_repo, epoch=3)
    provisioner = Provisioner(
        session_factory=session_factory,
        git=_FailingGit(  # type: ignore[arg-type]
            advance=False,
            session_factory=session_factory,
            work_dir=tmp_path / "awf-work",
        ),
        config=ProvisionerConfig(node_id="test-node-01"),
    )

    with pytest.raises(GitOperationError):
        await provisioner.provision_claimed(ws_id, execution_claim_epoch=3)

    reloaded = await _reload(session_factory, ws_id)
    assert reloaded is not None
    assert reloaded.status == WorkspaceStatus.failed.value
    assert reloaded.failure_reason == "infrastructure_failure"
    assert reloaded.failure_message is not None


@pytest.mark.unit
async def test_mark_failed_fenced_cannot_force_failed(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    origin_repo: Path,
) -> None:
    # The git op advances the epoch (a later claimant) before raising, so the
    # _mark_failed CAS updates 0 rows: a fenced worker cannot force the new
    # claimant's row to failed (D7).
    ws_id = await _create_provisioning(session_factory, origin_repo, epoch=3)
    provisioner = Provisioner(
        session_factory=session_factory,
        git=_FailingGit(  # type: ignore[arg-type]
            advance=True,
            session_factory=session_factory,
            work_dir=tmp_path / "awf-work",
        ),
        config=ProvisionerConfig(node_id="test-node-01"),
    )

    with pytest.raises(GitOperationError):
        await provisioner.provision_claimed(ws_id, execution_claim_epoch=3)

    reloaded = await _reload(session_factory, ws_id)
    assert reloaded is not None
    # Not forced to failed; the newer claimant's row survives.
    assert reloaded.status == WorkspaceStatus.provisioning.value
    assert reloaded.failure_reason is None
    assert reloaded.execution_claim_epoch == 4
    assert reloaded.execution_claimed_by == "control-worker-newer"


@pytest.mark.unit
async def test_epoch_none_path_transitions_to_ready_unguarded(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
) -> None:
    # The legacy path (no epoch) uses the unguarded transition and is unaffected
    # by any epoch drift.
    ws_id = await _create_provisioning(session_factory, origin_repo, epoch=1)
    await _advance_epoch(session_factory, ws_id)  # epoch now 2; ignored when None
    launcher = _RecordingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )

    await provisioner.provision_claimed(ws_id, execution_claim_epoch=None)

    assert len(launcher.requests) == 1
    reloaded = await _reload(session_factory, ws_id)
    assert reloaded is not None
    assert reloaded.status == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_verify_execution_claim_epoch_false_when_missing_or_not_provisioning(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
) -> None:
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    # Missing workspace -> fenced.
    assert await provisioner._verify_execution_claim_epoch("ws_missing", 1) is False  # noqa: SLF001

    # A workspace that has left ``provisioning`` -> fenced even if the epoch
    # matches (the claim no longer applies).
    ws_id = await _create_provisioning(session_factory, origin_repo, epoch=1)
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ws.status = WorkspaceStatus.failed.value
        await s.commit()
    assert await provisioner._verify_execution_claim_epoch(ws_id, 1) is False  # noqa: SLF001
