"""ControlWorker in-place provider-recovery (``recovering``) resume dispatch (#612).

A workspace that hit a retryable provider failure mid-run pauses into
``recovering`` (warm stack + execution claim preserved) with a cooldown deadline
in ``task_policy.provider_recovery_state.not_before``. Once the cooldown elapses
the worker re-claims it (``recovering -> running``), stashes + resets the
worktree to HEAD, and re-invokes the agent in place. These tests exercise the
cooldown gate, the epoch-fenced double-resume guard, the stash+reset, and the
teardown guards that keep the warm lease held while paused.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog.testing
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.redaction import REDACTION_MARKER
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine

WORKER_TEST_TIMEOUT_SECONDS = 60.0


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
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _TransitioningProvisioner:
    def __init__(self, worktree_path: Path | None = None) -> None:
        self._worktree_path = worktree_path

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:  # pragma: no cover - no requested workspaces in these tests
        del workspace_id, execution_claim_epoch

    def get_worktree_path(self, workspace_id: str) -> Path | None:
        del workspace_id
        return self._worktree_path


class _RecordingRecoveringExecutor:
    def __init__(self) -> None:
        self.resume_recovering_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:  # pragma: no cover
        del workspace_id

    async def resume_pr_monitor(self, workspace_id: str) -> None:  # pragma: no cover
        del workspace_id

    async def resume_recovering_execution(self, workspace_id: str, **_kwargs: object) -> None:
        self.resume_recovering_calls.append(workspace_id)


async def _create_recovering(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    *,
    not_before: datetime,
) -> str:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=str(origin),
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.recovering, reason_code="SEED")
        ws.task_policy = {
            "provider_recovery_state": {
                "action": "retry",
                "retry_attempt_number": 1,
                "not_before": not_before.isoformat(),
            }
        }
        ws.execution_claimed_by = None
        await s.commit()
        return ws.id


def _worker(
    session_factory: async_sessionmaker[AsyncSession],
    executor: _RecordingRecoveringExecutor,
    *,
    provisioner: _TransitioningProvisioner | None = None,
) -> ControlWorker:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner or _TransitioningProvisioner(),  # type: ignore[arg-type]
        executor=executor,
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    return worker


@pytest.mark.unit
async def test_list_resumable_recovering_cooldown_gate(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    now = datetime.now(UTC)
    cooled = await _create_recovering(
        session_factory, origin_repo, "cooled", not_before=now - timedelta(seconds=5)
    )
    await _create_recovering(
        session_factory, origin_repo, "still-cooling", not_before=now + timedelta(seconds=300)
    )
    worker = _worker(session_factory, _RecordingRecoveringExecutor())

    # Only the workspace whose cooldown has elapsed is selected.
    selected = await worker._list_resumable_recovering(now=now, limit=10)  # noqa: SLF001
    assert selected == [cooled]


@pytest.mark.unit
async def test_run_once_resumes_cooled_down_recovering_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "cooled",
        not_before=datetime.now(UTC) - timedelta(seconds=5),
    )
    executor = _RecordingRecoveringExecutor()
    worker = _worker(session_factory, executor)

    dispatched = await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
    await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

    assert dispatched == 1
    assert executor.resume_recovering_calls == [ws_id]
    # The resume CAS performed the recovering -> running transition.
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.running.value


@pytest.mark.unit
async def test_run_once_leaves_cooling_recovering_workspace_untouched(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "still-cooling",
        not_before=datetime.now(UTC) + timedelta(seconds=300),
    )
    executor = _RecordingRecoveringExecutor()
    worker = _worker(session_factory, executor)

    dispatched = await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
    await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

    assert dispatched == 0
    assert executor.resume_recovering_calls == []
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.recovering.value


@pytest.mark.unit
async def test_double_resume_epoch_fence_one_wins(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "fence",
        not_before=datetime.now(UTC) - timedelta(seconds=5),
    )
    worker = _worker(session_factory, _RecordingRecoveringExecutor())

    first = await worker._claim_recovering_for_resume(ws_id)  # noqa: SLF001
    # The second claim loses: the row already left ``recovering`` for ``running``.
    second = await worker._claim_recovering_for_resume(ws_id)  # noqa: SLF001

    assert first is True
    assert second is False
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.running.value
        assert ws.execution_claimed_by == worker._worker_id  # noqa: SLF001


@pytest.mark.unit
async def test_reset_recovering_worktree_stashes_dirt_and_resets_to_head(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A stalled-mid-edit worktree is reset to HEAD before the agent re-runs; the
    # uncommitted dirt is stashed first so partial work stays recoverable.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(["init", "-q", "-b", "development"], worktree)
    _git(["config", "user.name", "T"], worktree)
    _git(["config", "user.email", "t@t"], worktree)
    (worktree / "tracked.py").write_text("clean\n")
    _git(["add", "."], worktree)
    _git(["commit", "-q", "-m", "base"], worktree)
    # Dirty the worktree: modify a tracked file + add an untracked one.
    (worktree / "tracked.py").write_text("DIRTY EDIT\n")
    (worktree / "untracked.py").write_text("scratch\n")

    worker = _worker(
        session_factory,
        _RecordingRecoveringExecutor(),
        provisioner=_TransitioningProvisioner(worktree_path=worktree),
    )

    await worker._reset_recovering_worktree("ws_dirty")  # noqa: SLF001

    # Worktree is reset to the last clean commit.
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""
    assert (worktree / "tracked.py").read_text() == "clean\n"
    # The partial work is preserved as a stash entry (recoverable).
    stash_list = subprocess.run(
        ["git", "stash", "list"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "awf-recovering-resume-ws_dirty" in stash_list.stdout


@pytest.mark.unit
async def test_reset_recovering_worktree_clean_worktree_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A clean worktree (no uncommitted dirt) needs no stash; the reset-to-HEAD is
    # a harmless no-op and no stash entry is created.
    worktree = tmp_path / "clean"
    worktree.mkdir()
    _git(["init", "-q", "-b", "development"], worktree)
    _git(["config", "user.name", "T"], worktree)
    _git(["config", "user.email", "t@t"], worktree)
    (worktree / "tracked.py").write_text("clean\n")
    _git(["add", "."], worktree)
    _git(["commit", "-q", "-m", "base"], worktree)

    worker = _worker(
        session_factory,
        _RecordingRecoveringExecutor(),
        provisioner=_TransitioningProvisioner(worktree_path=worktree),
    )

    await worker._reset_recovering_worktree("ws_clean")  # noqa: SLF001

    stash_list = subprocess.run(
        ["git", "stash", "list"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert stash_list.stdout.strip() == ""
    assert (worktree / "tracked.py").read_text() == "clean\n"


@pytest.mark.unit
async def test_reset_recovering_worktree_skips_when_status_fails(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # ``git status`` fails on a non-git directory → the reset is skipped rather
    # than risk discarding unstashed work, and the function reports ``False`` so
    # the caller aborts the in-place retry instead of running on the dirty tree.
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    (not_a_repo / "scratch.py").write_text("DIRTY\n")

    worker = _worker(
        session_factory,
        _RecordingRecoveringExecutor(),
        provisioner=_TransitioningProvisioner(worktree_path=not_a_repo),
    )

    safe = await worker._reset_recovering_worktree("ws_no_git")  # noqa: SLF001

    # The file is untouched — no destructive reset ran — and the reset is unsafe.
    assert safe is False
    assert (not_a_repo / "scratch.py").read_text() == "DIRTY\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("event", "responses"),
    [
        # ``git status`` fails outright.
        (
            "worker.recovering_resume_status_failed",
            [(False, "", "fatal: clone https://x-access-token:ghp_SECRETTOKEN@github.com failed")],
        ),
        # ``git status`` reports dirt, then the stash push fails.
        (
            "worker.recovering_resume_stash_failed",
            [
                (True, "M tracked.py\n", ""),
                (
                    False,
                    "",
                    "fatal: stash https://x-access-token:ghp_SECRETTOKEN@github.com failed",
                ),
            ],
        ),
        # Clean worktree (no stash), then ``git reset --hard`` fails.
        (
            "worker.recovering_resume_reset_failed",
            [
                (True, "", ""),
                (
                    False,
                    "",
                    "fatal: reset https://x-access-token:ghp_SECRETTOKEN@github.com failed",
                ),
            ],
        ),
    ],
)
async def test_reset_recovering_worktree_failure_logs_carry_reason_code_and_redact(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    event: str,
    responses: list[tuple[bool, str, str]],
) -> None:
    # Each git-failure branch must log the abort reason_code (so logs match the
    # persisted restore event) and redact the raw git stderr (it can embed a
    # tokenized remote URL).
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    worker = _worker(
        session_factory,
        _RecordingRecoveringExecutor(),
        provisioner=_TransitioningProvisioner(worktree_path=worktree),
    )

    calls = iter(responses)

    async def _fake_git(*_args: object, **_kwargs: object) -> tuple[bool, str, str]:
        return next(calls)

    worker._run_preserved_active_git = _fake_git  # type: ignore[method-assign]  # noqa: SLF001

    with structlog.testing.capture_logs() as logs:
        safe = await worker._reset_recovering_worktree("ws_fail")  # noqa: SLF001

    assert safe is False
    entry = next(item for item in logs if item["event"] == event)
    assert entry["reason_code"] == "RECOVERING_RESUME_WORKTREE_RESET_ABORTED"
    assert "ghp_SECRETTOKEN" not in entry["error"]
    assert REDACTION_MARKER in entry["error"]


@pytest.mark.unit
async def test_reset_recovering_worktree_reports_safe_when_reset_completes(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A successful stash + reset (and the clean no-op case, with no worktree path)
    # all report ``True`` so the caller proceeds with the in-place retry.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(["init", "-q", "-b", "development"], worktree)
    _git(["config", "user.name", "T"], worktree)
    _git(["config", "user.email", "t@t"], worktree)
    (worktree / "tracked.py").write_text("clean\n")
    _git(["add", "."], worktree)
    _git(["commit", "-q", "-m", "base"], worktree)
    (worktree / "tracked.py").write_text("DIRTY EDIT\n")

    worker = _worker(
        session_factory,
        _RecordingRecoveringExecutor(),
        provisioner=_TransitioningProvisioner(worktree_path=worktree),
    )
    assert await worker._reset_recovering_worktree("ws_dirty") is True  # noqa: SLF001

    # No preserved worktree path → nothing to reset → still safe to resume.
    no_path_worker = _worker(
        session_factory,
        _RecordingRecoveringExecutor(),
        provisioner=_TransitioningProvisioner(worktree_path=None),
    )
    assert await no_path_worker._reset_recovering_worktree("ws_none") is True  # noqa: SLF001


@pytest.mark.unit
async def test_recovering_resume_aborts_when_worktree_reset_fails(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
    tmp_path: Path,
) -> None:
    # When the pre-run worktree reset cannot complete (here ``git status`` fails on
    # a non-git worktree that may still hold partial provider edits), the in-place
    # recovering retry is aborted: the executor is NOT re-invoked on the dirty tree,
    # the row is restored to ``recovering`` for a later safe resume, and the claim
    # is released so a capable worker can re-claim the cooled-down row.
    not_a_repo = tmp_path / "dirty-worktree"
    not_a_repo.mkdir()
    (not_a_repo / "partial.py").write_text("PARTIAL PROVIDER EDIT\n")

    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "reset-fails",
        not_before=datetime.now(UTC) - timedelta(seconds=5),
    )
    executor = _RecordingRecoveringExecutor()
    worker = _worker(
        session_factory,
        executor,
        provisioner=_TransitioningProvisioner(worktree_path=not_a_repo),
    )
    assert await worker._claim_recovering_for_resume(ws_id)  # noqa: SLF001

    await asyncio.wait_for(
        worker._safely_resume_recovering_claimed(ws_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    # The contaminated worktree was never handed to the executor.
    assert executor.resume_recovering_calls == []
    # The partial edits are preserved — no destructive reset ran.
    assert (not_a_repo / "partial.py").read_text() == "PARTIAL PROVIDER EDIT\n"
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        # Paused state restored; the claim released so the row can be re-claimed.
        assert ws.status == WorkspaceStatus.recovering.value
        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None


@pytest.mark.unit
async def test_recovering_resume_rearms_cooldown_when_worktree_reset_fails(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
    tmp_path: Path,
) -> None:
    # Regression (#647): when the pre-run worktree reset cannot complete the row is
    # restored to ``recovering`` — but its ``provider_recovery_state.not_before`` must
    # be re-armed into the future. Otherwise the cooldown stays in the past,
    # ``list_resumable_recovering_ids`` re-selects the same row every poll, and a
    # persistent ``git`` status/stash/reset failure busy-loops the executor slot
    # (consuming slots + log volume) instead of backing off for a later safe retry.
    not_a_repo = tmp_path / "dirty-worktree"
    not_a_repo.mkdir()
    (not_a_repo / "partial.py").write_text("PARTIAL PROVIDER EDIT\n")

    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "rearm-cooldown",
        not_before=datetime.now(UTC) - timedelta(seconds=5),
    )
    executor = _RecordingRecoveringExecutor()
    worker = _worker(
        session_factory,
        executor,
        provisioner=_TransitioningProvisioner(worktree_path=not_a_repo),
    )
    assert await worker._claim_recovering_for_resume(ws_id)  # noqa: SLF001

    before = datetime.now(UTC)
    await asyncio.wait_for(
        worker._safely_resume_recovering_claimed(ws_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    assert executor.resume_recovering_calls == []
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.recovering.value
        # The cooldown was re-armed into the future (default 300s provider cooldown),
        # while the rest of the recovery state is preserved.
        recovery_state = ws.task_policy["provider_recovery_state"]
        assert recovery_state["retry_attempt_number"] == 1
        rearmed = datetime.fromisoformat(recovery_state["not_before"])
        assert rearmed > before + timedelta(seconds=60)

    # The backed-off row is no longer immediately resumable: the cooldown gate now
    # excludes it until the re-armed deadline elapses, so it cannot be re-claimed
    # on the very next poll.
    selected = await worker._list_resumable_recovering(  # noqa: SLF001
        now=datetime.now(UTC), limit=10
    )
    assert selected == []


class _FailingRecoveringExecutor:
    """Executor whose recovering resume raises after the claim CAS (post-claim failure).

    Mimics a persistent transient executor/setup failure: the resume CAS already
    committed ``recovering -> running``, then ``resume_recovering_execution`` raises
    before moving the row onward, exercising the ``except Exception`` restore path.
    """

    def __init__(self) -> None:
        self.resume_recovering_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:  # pragma: no cover
        del workspace_id

    async def resume_pr_monitor(self, workspace_id: str) -> None:  # pragma: no cover
        del workspace_id

    async def resume_recovering_execution(self, workspace_id: str, **_kwargs: object) -> None:
        self.resume_recovering_calls.append(workspace_id)
        raise RuntimeError("post-claim executor failure")


@pytest.mark.unit
async def test_recovering_resume_rearms_cooldown_when_executor_resume_fails(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
    tmp_path: Path,
) -> None:
    # Regression (#647): when the worktree reset succeeds but the executor's
    # recovering resume raises after the claim CAS (a post-claim transient
    # executor/setup failure), the row is restored to ``recovering`` — and its
    # ``provider_recovery_state.not_before`` must be re-armed into the future, the
    # same way the worktree-reset-abort path backs off. Otherwise the cooldown
    # stays in the past and a *persistent* executor failure busy-loops the executor
    # slot, repeatedly re-claiming and failing every poll.
    worktree = tmp_path / "clean-worktree"
    worktree.mkdir()
    _git(["init", "-q", "-b", "development"], worktree)
    _git(["config", "user.name", "T"], worktree)
    _git(["config", "user.email", "t@t"], worktree)
    (worktree / "tracked.py").write_text("clean\n")
    _git(["add", "."], worktree)
    _git(["commit", "-q", "-m", "base"], worktree)

    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "executor-fails",
        not_before=datetime.now(UTC) - timedelta(seconds=5),
    )
    executor = _FailingRecoveringExecutor()
    worker = _worker(
        session_factory,
        executor,  # type: ignore[arg-type]
        provisioner=_TransitioningProvisioner(worktree_path=worktree),
    )
    assert await worker._claim_recovering_for_resume(ws_id)  # noqa: SLF001

    before = datetime.now(UTC)
    await asyncio.wait_for(
        worker._safely_resume_recovering_claimed(ws_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    # The executor was reached (clean reset succeeded) but raised post-claim.
    assert executor.resume_recovering_calls == [ws_id]
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.recovering.value
        # The claim was released so a capable worker can re-claim it later.
        assert ws.execution_claimed_by is None
        # The cooldown was re-armed into the future, preserving the rest of state.
        recovery_state = ws.task_policy["provider_recovery_state"]
        assert recovery_state["retry_attempt_number"] == 1
        rearmed = datetime.fromisoformat(recovery_state["not_before"])
        assert rearmed > before + timedelta(seconds=60)

    # The backed-off row is no longer immediately resumable.
    selected = await worker._list_resumable_recovering(  # noqa: SLF001
        now=datetime.now(UTC), limit=10
    )
    assert selected == []


@pytest.mark.unit
async def test_restore_recovering_resume_claim_rearms_cooldown_on_dispatch_abort(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # Regression (#647): the dispatch-abort revert (a claimed-but-undispatched row,
    # e.g. a failed ordered-decision write) routes through
    # ``_restore_recovering_resume_claim``. Like the worktree-reset-abort path it
    # must re-arm ``not_before`` into the future, else a persistent dispatch abort
    # re-selects the row every poll and busy-loops the executor slot.
    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "dispatch-abort",
        not_before=datetime.now(UTC) - timedelta(seconds=5),
    )
    worker = _worker(session_factory, _RecordingRecoveringExecutor())
    # The claim CAS commits ``recovering -> running`` and stamps the claim.
    assert await worker._claim_recovering_for_resume(ws_id)  # noqa: SLF001

    before = datetime.now(UTC)
    await worker._restore_recovering_resume_claim(  # noqa: SLF001
        ws_id, reason_code="WORKER_RECOVERING_RESUME_DISPATCH_ABORTED"
    )

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.recovering.value
        recovery_state = ws.task_policy["provider_recovery_state"]
        assert recovery_state["retry_attempt_number"] == 1
        rearmed = datetime.fromisoformat(recovery_state["not_before"])
        assert rearmed > before + timedelta(seconds=60)


@pytest.mark.unit
async def test_release_execution_claim_skips_recovering_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # T8: a ``recovering`` workspace keeps its execution claim as the warm-stack
    # lease — the dispatch finally must not release it.
    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "warm-lease",
        not_before=datetime.now(UTC) + timedelta(seconds=300),
    )
    worker = _worker(session_factory, _RecordingRecoveringExecutor())
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ws.execution_claimed_by = worker._worker_id  # noqa: SLF001
        await s.commit()

    await worker._release_execution_claim(ws_id, skip_if_blocked=True)  # noqa: SLF001

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.recovering.value
        # The claim was retained, not released.
        assert ws.execution_claimed_by == worker._worker_id  # noqa: SLF001


class _RepausingRecoveringExecutor:
    """Executor whose recovering resume re-pauses the row back into ``recovering``.

    Mimics ``enter_recovering_for_provider_failure``: a second retryable provider
    failure during the in-place resume transitions ``running -> recovering`` and
    returns normally (no exception), keeping the execution claim untouched — the
    same clean-return divert the real execution flow takes.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.resume_recovering_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:  # pragma: no cover
        del workspace_id

    async def resume_pr_monitor(self, workspace_id: str) -> None:  # pragma: no cover
        del workspace_id

    async def resume_recovering_execution(self, workspace_id: str, **_kwargs: object) -> None:
        self.resume_recovering_calls.append(workspace_id)
        async with self._session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.recovering, reason_code="REPAUSE")
            await s.commit()


@pytest.mark.unit
async def test_recovering_resume_repause_retains_execution_claim(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # #612 T8 regression: when an in-place recovering resume hits another retryable
    # provider failure and the executor re-pauses the row back into ``recovering``
    # (a clean return, not an error), the dispatch ``finally`` must retain the
    # warm-stack execution claim — mirroring ``_safely_execute_claimed`` for the
    # initial execution — so the next cooldown resume continues on the held claim.
    # Before the fix the ``finally`` released the claim unconditionally, dropping
    # the lease on the second and later cooldowns.
    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "repause",
        not_before=datetime.now(UTC) - timedelta(seconds=5),
    )
    executor = _RepausingRecoveringExecutor(session_factory)
    worker = _worker(session_factory, executor)

    # The pre-dispatch resume CAS acquires the claim and moves recovering -> running.
    assert await worker._claim_recovering_for_resume(ws_id)  # noqa: SLF001
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.running.value
        assert ws.execution_claimed_by == worker._worker_id  # noqa: SLF001

    await asyncio.wait_for(
        worker._safely_resume_recovering_claimed(ws_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    assert executor.resume_recovering_calls == [ws_id]
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        # The executor re-paused the row; the claim is retained as the warm lease.
        assert ws.status == WorkspaceStatus.recovering.value
        assert ws.execution_claimed_by == worker._worker_id  # noqa: SLF001


@pytest.mark.unit
async def test_recovering_resume_missing_executor_releases_claim(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # Counterpart guard: when the worker has no executor to drive the resume it
    # reverts the row to ``recovering`` and the ``finally`` MUST still release the
    # claim so a capable worker can re-claim it — the ``skip_if_blocked`` gate only
    # applies to a clean executor-driven re-pause, not a worker revert.
    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "no-executor",
        not_before=datetime.now(UTC) - timedelta(seconds=5),
    )
    worker = _worker(session_factory, _RecordingRecoveringExecutor())
    assert await worker._claim_recovering_for_resume(ws_id)  # noqa: SLF001
    worker._executor = None  # noqa: SLF001

    await asyncio.wait_for(
        worker._safely_resume_recovering_claimed(ws_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        # Paused state restored, but the claim was released (not stranded).
        assert ws.status == WorkspaceStatus.recovering.value
        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None


@pytest.mark.unit
async def test_recovering_is_not_a_terminal_runtime_release_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # T8: ``recovering`` is non-terminal, so the prompt terminal-runtime release
    # never tears down its warm stack.
    ws_id = await _create_recovering(
        session_factory,
        origin_repo,
        "non-terminal",
        not_before=datetime.now(UTC) + timedelta(seconds=300),
    )
    worker = _worker(session_factory, _RecordingRecoveringExecutor())

    candidate = await worker._load_terminal_runtime_candidate(ws_id)  # noqa: SLF001
    assert candidate is None
