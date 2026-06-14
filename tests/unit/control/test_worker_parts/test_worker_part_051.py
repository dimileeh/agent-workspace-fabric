"""ControlWorker tests — prompt terminal-runtime release on terminal transition.

Covers issues **#583** and **#584**: the worker must release a terminal
workspace's runtime (compose ``down`` removing the agent+postgres containers and
the per-ws ``-net`` network, plus the Claude auth-overlay umount) *promptly* on
the terminal transition, reusing the existing per-candidate teardown, instead of
waiting for the ~1h periodic reaper interval.

These use the real Provisioner-style helpers against real Postgres + the real
``WorkspaceRepository`` (mirroring ``test_worker_part_040``), with an injected
fake ``_runtime_cleaner`` recording the compose-``down`` calls.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.cleanup import (
    COMPOSE_DOWN_SUCCEEDED,
    WorkspaceCleanupResult,
    WorkspaceCleanupStepResult,
)
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.postgres import postgres_test_engine

WORKER_TEST_TIMEOUT_SECONDS = 300.0


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


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.resume_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:
        self.calls.append(workspace_id)

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.resume_calls.append(workspace_id)


class _RecordingRuntimeCleaner:
    def __init__(self, result: WorkspaceCleanupResult | None = None) -> None:
        self.result = result or WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        )
        self.calls: list[dict[str, object]] = []

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "repo_url": repo_url,
                "compose_project_name": compose_project_name,
                "compose_file_path": compose_file_path,
                "worktree_host_path": worktree_host_path,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            }
        )
        return self.result


def _build_worker(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    cleaner: _RecordingRuntimeCleaner | None = None,
    executor: _RecordingExecutor | None = None,
) -> ControlWorker:
    git = GitManager(tmp_path / "awf-work")
    prov = Provisioner(
        session_factory=session_factory,
        git=git,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    return ControlWorker(
        session_factory=session_factory,
        provisioner=prov,
        executor=executor or _RecordingExecutor(),
        runtime_cleaner=cleaner,
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=0,
            terminal_runtime_release_scan_interval_seconds=0.0,
        ),
    )


async def _create_terminal_execution(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    status: WorkspaceStatus,
    *,
    node_id: str | None = None,
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
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        ws.node_id = node_id
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        if status == WorkspaceStatus.failed:
            ws.failure_reason = "infrastructure_failure"
            ws.failure_message = "seed failure"
            await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="SEED")
        elif status == WorkspaceStatus.cancelled:
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="SEED")
        else:
            await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
            await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
            assert status == WorkspaceStatus.completed
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="SEED")
        await s.commit()
        return ws.id


async def _create_requested(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
) -> str:
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=str(origin),
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await s.commit()
        return ws.id


async def _list_released_events(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[object]:
    async with session_factory() as s:
        return await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )


class TestPromptTerminalRuntimeRelease:
    @pytest.mark.unit
    async def test_prompt_release_on_completion_tears_down_and_reclaims_overlay(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        """#584: a completed workspace's runtime is released promptly — compose
        ``down`` runs and the auth-overlay teardown path is exercised (recorded in
        the release event), so the overlay is reclaimed as the workspace finishes
        rather than on the ~1h interval."""
        workspace_id = await _create_terminal_execution(
            session_factory, origin_repo, "prompt-complete", WorkspaceStatus.completed
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = _build_worker(session_factory, tmp_path, cleaner=cleaner)

        await worker._release_terminal_runtime_promptly(workspace_id)  # noqa: SLF001

        assert cleaner.calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": str(origin_repo),
                "compose_project_name": f"awf_{workspace_id}",
                "compose_file_path": Path(f"/tmp/awf/{workspace_id}/compose.yml"),
                "worktree_host_path": None,
                "remove_volumes": False,
                "remove_worktree": False,
            }
        ]
        released = await _list_released_events(session_factory, workspace_id)
        assert len(released) == 1
        # The auth-overlay teardown path ran (no overlay work dir wired → ``None``),
        # proving the same per-candidate release the periodic reaper runs fired here.
        assert released[0].payload is not None  # type: ignore[attr-defined]
        assert "auth_overlay_unmounted" in released[0].payload  # type: ignore[attr-defined]
        assert released[0].payload["auth_overlay_unmounted"] is None  # type: ignore[attr-defined]

    @pytest.mark.unit
    async def test_prompt_release_on_cancel_stop_stack_runs_compose_down_despite_marker(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        """#583: a cancelled (``--stop-stack``) workspace already carries a
        service-pre-emitted ``terminal_runtime_released`` event (recorded after a
        ``docker stop`` only — Exited containers + ``-net`` network still present).
        The prompt release must STILL run the real ``compose down`` (the marker
        proves only a stop), and must not write a duplicate release event."""
        workspace_id = await _create_terminal_execution(
            session_factory, origin_repo, "prompt-cancel", WorkspaceStatus.cancelled
        )
        # Service-style pre-emitted release marker (stop only, no ``down``).
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.add_event(
                ws,
                event_type="workspace.terminal_runtime_released",
                reason_code="TERMINAL_RUNTIME_RELEASED",
                payload={
                    "compose_project_name": ws.compose_project_name,
                    "workspace_status": ws.status,
                    "cleanup": {"status": "skipped"},
                },
            )
            await s.commit()
        cleaner = _RecordingRuntimeCleaner()
        worker = _build_worker(session_factory, tmp_path, cleaner=cleaner)

        await worker._release_terminal_runtime_promptly(workspace_id)  # noqa: SLF001

        # The real ``compose down`` ran despite the pre-existing stop-only marker,
        # removing the Exited containers + the ``-net`` network.
        assert len(cleaner.calls) == 1
        assert cleaner.calls[0]["workspace_id"] == workspace_id
        # No duplicate release event written (idempotent at the event layer).
        released = await _list_released_events(session_factory, workspace_id)
        assert len(released) == 1

    @pytest.mark.unit
    async def test_prompt_release_on_failure_releases_runtime_preserving_failure_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        """A failed workspace runs the SAME per-candidate release the periodic
        reaper already runs for ``failed`` (compose ``down`` + overlay umount
        attempt). The fix only changes WHEN that release fires, never WHAT it
        preserves: the failure reason/message are untouched, and the auth-dir
        preservation policy (owned by the separate terminal GC reaper) is unchanged."""
        workspace_id = await _create_terminal_execution(
            session_factory, origin_repo, "prompt-failed", WorkspaceStatus.failed
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = _build_worker(session_factory, tmp_path, cleaner=cleaner)

        await worker._release_terminal_runtime_promptly(workspace_id)  # noqa: SLF001

        assert len(cleaner.calls) == 1
        assert cleaner.calls[0]["workspace_id"] == workspace_id
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message == "seed failure"
        released = await _list_released_events(session_factory, workspace_id)
        assert len(released) == 1

    @pytest.mark.unit
    async def test_prompt_release_noop_when_already_released_by_worker(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        """Re-invoking the prompt release after the periodic reaper already
        released the workspace is safe: the cleaner's ``down`` is idempotent and no
        second ``terminal_runtime_released`` event is written (marker honored at the
        event layer), and it never raises."""
        workspace_id = await _create_terminal_execution(
            session_factory, origin_repo, "prompt-already", WorkspaceStatus.completed
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = _build_worker(session_factory, tmp_path, cleaner=cleaner)

        # First release (as the periodic reaper would have done).
        await worker._release_terminal_runtime_promptly(workspace_id)  # noqa: SLF001
        # Prompt release fires again on the terminal transition.
        await worker._release_terminal_runtime_promptly(workspace_id)  # noqa: SLF001

        # Still exactly one release event despite two release passes.
        released = await _list_released_events(session_factory, workspace_id)
        assert len(released) == 1

    @pytest.mark.unit
    async def test_prompt_release_noop_when_not_terminal(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        """A non-terminal workspace is not a release candidate, so the prompt
        release does no teardown and writes no event (the hook is safe to add to
        every wrapper finally — it only does work for an actually-terminal ws)."""
        workspace_id = await _create_requested(session_factory, origin_repo, "prompt-requested")
        cleaner = _RecordingRuntimeCleaner()
        worker = _build_worker(session_factory, tmp_path, cleaner=cleaner)

        await worker._release_terminal_runtime_promptly(workspace_id)  # noqa: SLF001

        assert cleaner.calls == []
        released = await _list_released_events(session_factory, workspace_id)
        assert released == []

    @pytest.mark.unit
    async def test_prompt_release_noop_when_foreign_node(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        """A terminal workspace owned by another node is skipped — the prompt
        release must not tear down a runtime that may live on a sibling node
        (concurrency/claim model respected)."""
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "prompt-foreign",
            WorkspaceStatus.completed,
            node_id="some-other-node",
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = _build_worker(session_factory, tmp_path, cleaner=cleaner)

        await worker._release_terminal_runtime_promptly(workspace_id)  # noqa: SLF001

        assert cleaner.calls == []
        released = await _list_released_events(session_factory, workspace_id)
        assert released == []

    @pytest.mark.unit
    async def test_prompt_release_noop_when_workspace_missing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """An unknown workspace id loads no candidate and is a clean no-op."""
        cleaner = _RecordingRuntimeCleaner()
        worker = _build_worker(session_factory, tmp_path, cleaner=cleaner)

        await worker._release_terminal_runtime_promptly("ws_does_not_exist")  # noqa: SLF001

        assert cleaner.calls == []


class TestPromptTerminalRuntimeReleaseHookWiring:
    @pytest.mark.unit
    async def test_safely_execute_claimed_triggers_prompt_release_after_claim_release(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """``_safely_execute_claimed`` fires the prompt release exactly once, after
        the execution claim is released (covers running/validating/pushing →
        completed/failed and a running ws cancelled while the worker executes it)."""
        worker = _build_worker(session_factory, tmp_path)
        sequence: list[str] = []
        prompt_calls: list[str] = []

        async def _execute(workspace_id: str) -> None:
            sequence.append(f"execute:{workspace_id}")

        async def _release_claim(workspace_id: str) -> None:
            sequence.append(f"release_claim:{workspace_id}")

        async def _prompt_release(workspace_id: str) -> None:
            sequence.append(f"prompt:{workspace_id}")
            prompt_calls.append(workspace_id)

        worker._safely_execute = _execute  # type: ignore[method-assign]
        worker._release_execution_claim = _release_claim  # type: ignore[method-assign]
        worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

        await asyncio.wait_for(
            worker._safely_execute_claimed("ws_exec"),  # noqa: SLF001
            timeout=WORKER_TEST_TIMEOUT_SECONDS,
        )

        assert prompt_calls == ["ws_exec"]
        assert sequence == [
            "execute:ws_exec",
            "release_claim:ws_exec",
            "prompt:ws_exec",
        ]

    @pytest.mark.unit
    async def test_safely_provision_claimed_triggers_prompt_release_after_claim_release(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """``_safely_provision_claimed`` fires the prompt release after the claim
        release on its outer finally (covers provision failure → ``failed``)."""
        worker = _build_worker(session_factory, tmp_path)
        sequence: list[str] = []
        prompt_calls: list[str] = []

        async def _epoch(workspace_id: str) -> int | None:
            # ``None`` short-circuits the provision body; the outer finally still
            # runs, so the claim release + prompt release fire.
            del workspace_id
            return None

        async def _release_after_cancel(workspace_id: str) -> None:
            sequence.append(f"release_claim:{workspace_id}")

        async def _prompt_release(workspace_id: str) -> None:
            sequence.append(f"prompt:{workspace_id}")
            prompt_calls.append(workspace_id)

        worker._read_execution_claim_epoch = _epoch  # type: ignore[method-assign]
        worker._release_execution_claim_after_cancellation = (  # type: ignore[method-assign]
            _release_after_cancel
        )
        worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

        await asyncio.wait_for(
            worker._safely_provision_claimed("ws_prov"),  # noqa: SLF001
            timeout=WORKER_TEST_TIMEOUT_SECONDS,
        )

        assert prompt_calls == ["ws_prov"]
        assert sequence == [
            "release_claim:ws_prov",
            "prompt:ws_prov",
        ]

    @pytest.mark.unit
    async def test_safely_resume_claimed_pr_monitor_triggers_prompt_release_after_claim_release(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """``_safely_resume_claimed_pr_monitor`` fires the prompt release after the
        monitoring-pr claim release (covers monitoring_pr → completed on merge and
        → failed on abort)."""
        worker = _build_worker(session_factory, tmp_path)
        sequence: list[str] = []
        prompt_calls: list[str] = []

        async def _resume(workspace_id: str, *, recovery_operation_id: str | None = None) -> bool:
            del recovery_operation_id
            sequence.append(f"resume:{workspace_id}")
            return True

        async def _release_monitor_claim(workspace_id: str) -> None:
            sequence.append(f"release_claim:{workspace_id}")

        async def _prompt_release(workspace_id: str) -> None:
            sequence.append(f"prompt:{workspace_id}")
            prompt_calls.append(workspace_id)

        worker._safely_resume_pr_monitor = _resume  # type: ignore[method-assign]
        worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
        worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

        await asyncio.wait_for(
            worker._safely_resume_claimed_pr_monitor("ws_monitor"),  # noqa: SLF001
            timeout=WORKER_TEST_TIMEOUT_SECONDS,
        )

        assert prompt_calls == ["ws_monitor"]
        assert sequence == [
            "resume:ws_monitor",
            "release_claim:ws_monitor",
            "prompt:ws_monitor",
        ]
