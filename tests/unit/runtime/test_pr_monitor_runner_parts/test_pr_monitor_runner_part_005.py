"""Unit tests for focused ``pr_monitor_runner`` behavior.

Most cases cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). Focused
runtime-path regressions live here when the unit suite needs to cover a
specific merge-gate branch without running the full monitor integration loop.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_mock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.models import Operation, Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.planning import CONFORMANCE_REQUIRES_AWF_VALIDATION
from awf.runtime.pr_monitor import (
    AddressComments,
    CheckFailure,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.gates import _MergeGateResult
from awf.runtime.pr_monitor_runner.helpers import (
    _infer_service_work_dir,
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_state_for_persistence,
    _initial_review_grace_state_for_runtime,
    _initial_review_grace_wait_seconds,
    _initial_review_grace_wall_seconds,
    _initial_review_grace_wall_started_value_from_datetime,
    _merge_rejection_reason,
    _notify_human_reason,
    _target_reconcile_payload,
    _with_ci_failures,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryRetryError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor import _status


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class PersistCheckingSleep(RecordedSleep):
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
        state_key: str,
        expected_value: str,
    ) -> None:
        super().__init__()
        self._factory = factory
        self._workspace_id = workspace_id
        self._state_key = state_key
        self._expected_value = expected_value

    async def __call__(self, seconds: float) -> None:
        async with self._factory() as session:
            workspace = await WorkspaceRepository(session).get(self._workspace_id)
            assert workspace is not None
            assert workspace.monitor_threads_addressed[self._state_key] == self._expected_value
        await super().__call__(seconds)


class PersistCheckingCommandRunner(FakeCommandRunner):
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
        state_key: str,
        expected_value: str,
    ) -> None:
        super().__init__()
        self._factory = factory
        self._workspace_id = workspace_id
        self._state_key = state_key
        self._expected_value = expected_value

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        if args[:3] == ["gh", "run", "rerun"]:
            async with self._factory() as session:
                workspace = await WorkspaceRepository(session).get(self._workspace_id)
                assert workspace is not None
                assert (
                    workspace.monitor_threads_addressed.get(self._state_key) == self._expected_value
                )
        return await super().run(args, input_bytes=input_bytes, cwd=cwd)


def _monitor_runner(
    tmp_path: Path,
    fake: FakeCommandRunner,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    workspace_runtime_context: str = "",
) -> PullRequestMonitorRunner:
    return PullRequestMonitorRunner(
        session_factory=session_factory or object(),  # type: ignore[arg-type]
        runner=fake,
        adapter=object(),  # type: ignore[arg-type]
        gh=object(),  # type: ignore[arg-type]
        worktrees_root=tmp_path / "work" / "git" / "worktrees",
        workspace_runtime_context=workspace_runtime_context,
    )


def _green_status(*, pr_number: int = 42, head_sha: str = "abc1234567890def") -> PRStatus:
    return PRStatus(
        number=pr_number,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


class _CommandIterable:
    def __iter__(self) -> Iterator[object]:
        return iter(("pytest -q", object(), "ruff check ."))


def _gh_pr_merge_calls(cmd: FakeCommandRunner) -> list[list[str]]:
    return [call.args for call in cmd.calls if call.args[:3] == ["gh", "pr", "merge"]]


class _CapturingGH:
    def __init__(self, status: PRStatus | None = None) -> None:
        self.status = status or _green_status()
        self.base_behind_counts: list[int] = []
        self.failing_log_requests: list[tuple[RepoRef, int, str, tuple[str, ...]]] = []
        self.posted_comments: list[tuple[RepoRef, int, str]] = []
        self.post_errors: list[GitHubClientError] = []

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
    ) -> PRStatus:
        del repo, pr_number
        self.base_behind_counts.append(base_behind_count)
        return replace(self.status, base_behind_count=base_behind_count)

    async def fetch_failing_check_logs(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        head_sha: str,
        pytest_fallback_commands: Sequence[str] = (),
    ) -> tuple[CheckFailure, ...]:
        self.failing_log_requests.append(
            (repo, pr_number, head_sha, tuple(pytest_fallback_commands))
        )
        return ()

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        if self.post_errors:
            raise self.post_errors.pop(0)
        self.posted_comments.append((repo, pr_number, body))


def _provider_recovery_policy(
    *,
    fallback_agent: str = "codex",
    fallback_provider: str = "openai",
    fallback_model: str = "gpt-5.3-codex",
    max_same_provider_retries: int = 1,
) -> dict[str, object]:
    return {
        "fallbacks": [
            {
                "agent": fallback_agent,
                "provider": fallback_provider,
                "model": fallback_model,
            }
        ],
        "max_fallback_attempts": 1,
        "max_same_provider_retries": max_same_provider_retries,
        "cooldown_seconds": 600,
        "circuit_breaker": {
            "failure_threshold": 2,
            "cooldown_seconds": 900,
        },
    }


async def _configure_provider_monitor_workspace(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    agent: str = "gemini",
    model: str = "gemini-2.5-pro",
    fallback_agent: str = "codex",
    fallback_provider: str = "openai",
    fallback_model: str = "gpt-5.3-codex",
    max_same_provider_retries: int = 1,
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.agent = agent
        workspace.auto_merge = False
        workspace.initial_review_grace_period_seconds = 75
        workspace.task_policy = {
            "agent_model": model,
            "provider_recovery": _provider_recovery_policy(
                fallback_agent=fallback_agent,
                fallback_provider=fallback_provider,
                fallback_model=fallback_model,
                max_same_provider_retries=max_same_provider_retries,
            ),
            "pr_monitor": {"review_grace_seconds": 75},
        }
        await session.commit()


async def _provider_recovery_snapshot(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> tuple[dict[str, object], list[dict[str, object]], list[Operation], list[str]]:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        source_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.provider_recovery_requested"
        ]
        operations = list((await session.execute(select(Operation))).scalars())
        requested_ids = list(
            (
                await session.execute(
                    select(Workspace.id).where(Workspace.status == WorkspaceStatus.requested.value)
                )
            ).scalars()
        )
        return (
            dict(workspace.task_policy),
            [dict(event.payload or {}) for event in source_events],
            operations,
            requested_ids,
        )


async def _mark_refactor_task(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    auto_merge: bool = True,
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.task_class = TaskClass.refactor_task.value
        workspace.auto_merge = auto_merge
        await session.commit()


async def _dispatch_merge_recovery(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    workspace_id: str,
    pr_number: int,
    head_sha: str,
    sleep_fn: RecordedSleep | None = None,
) -> bool:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn or RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    return await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )


class TestNotificationAndGraceHelpers:
    @pytest.mark.unit
    def test_notify_human_reason_prioritizes_blocking_conditions(self) -> None:
        blocking_review = ReviewComment(
            comment_id="C-block",
            body_excerpt="external policy gate",
            author="review-bot",
            blocks_merge=True,
        )
        deferred_review = ReviewComment(
            comment_id="C-human",
            body_excerpt="please inspect",
            author="human",
        )
        deferred_state = MonitorState(threads_addressed_ids={"C-human": "defer"})

        assert "merge-blocking changes-requested review" in (
            _notify_human_reason(_status(reviews=(blocking_review,)), MonitorState()) or ""
        )
        assert "required protection" in (
            _notify_human_reason(
                _status(merge_state_status=MergeStateStatus.BLOCKED),
                MonitorState(),
            )
            or ""
        )
        assert (
            _notify_human_reason(
                _status(
                    reviews=(deferred_review,),
                    merge_state_status=MergeStateStatus.BLOCKED,
                ),
                deferred_state,
            )
            == "human review feedback was deferred by the agent and remains unresolved"
        )
        assert (
            _notify_human_reason(
                _status(reviews=(deferred_review,)),
                deferred_state,
            )
            == "human review feedback was deferred by the agent and remains unresolved"
        )
        assert _notify_human_reason(_status(), MonitorState()) is None

    @pytest.mark.unit
    def test_initial_review_grace_state_converts_between_wall_and_monotonic_time(self) -> None:
        pr_number = 42
        started_key = _initial_review_grace_started_key(pr_number)
        done_key = _initial_review_grace_done_key(pr_number)
        wall_started = datetime(2026, 4, 27, 12, 0, tzinfo=UTC).timestamp()
        runtime_state = {started_key: f"{wall_started:.6f}"}
        persisted_state = {started_key: "900.000000"}

        assert _initial_review_grace_wall_seconds(object()) is None
        assert _initial_review_grace_wall_seconds("not-a-number") is None
        assert _initial_review_grace_wall_seconds("123.0") is None
        assert _initial_review_grace_wall_seconds(wall_started) == wall_started
        assert (
            _initial_review_grace_wall_started_value_from_datetime(
                datetime(2026, 4, 27, 12, 0),
            )
            == f"{wall_started:.6f}"
        )

        converted_runtime = _initial_review_grace_state_for_runtime(
            runtime_state,
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started + 30.0,
        )
        legacy_runtime = _initial_review_grace_state_for_runtime(
            {started_key: "900.0"},
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started,
            legacy_monotonic_fallback=875.0,
        )
        converted_persistence = _initial_review_grace_state_for_persistence(
            persisted_state,
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started + 100.0,
        )
        invalid_persistence = _initial_review_grace_state_for_persistence(
            {started_key: "invalid"},
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started,
        )
        unchanged_persistence = _initial_review_grace_state_for_persistence(
            {},
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started,
        )

        assert converted_runtime[started_key] == "970.000000"
        assert legacy_runtime[started_key] == "875.000000"
        assert converted_persistence[started_key] == f"{wall_started:.6f}"
        assert invalid_persistence[started_key] == "invalid"
        assert unchanged_persistence == {}

        waiting = MonitorState(started_at=10.0)
        assert (
            _initial_review_grace_wait_seconds(
                waiting,
                pr_number=pr_number,
                now=12.0,
                grace_seconds=10.0,
                poll_interval_seconds=3.0,
            )
            == 3.0
        )
        assert waiting.threads_addressed_ids[started_key] == "10.000000"

        invalid_started = MonitorState(
            started_at=20.0,
            threads_addressed_ids={started_key: "not-float"},
        )
        assert (
            _initial_review_grace_wait_seconds(
                invalid_started,
                pr_number=pr_number,
                now=35.0,
                grace_seconds=10.0,
                poll_interval_seconds=5.0,
            )
            == 0.0
        )
        assert invalid_started.threads_addressed_ids[started_key] == "20.000000"
        assert invalid_started.threads_addressed_ids[done_key] == "elapsed"


class TestMiscMonitorHelpers:
    @pytest.mark.unit
    def test_merge_rejection_reason_and_service_work_dir_edges(self) -> None:
        assert _merge_rejection_reason("") == "GitHub rejected the merge attempt"
        assert _merge_rejection_reason(" ! [rejected] main -> main ") == (
            "GitHub rejected the merge attempt: ! [rejected] main -> main"
        )
        assert _infer_service_work_dir(Path("/srv/awf/git/worktrees")) == Path("/srv/awf")
        assert _infer_service_work_dir(Path("/srv/awf/worktrees")) == Path("/srv/awf")

    @pytest.mark.unit
    def test_target_reconcile_payload_accepts_dict_to_dict_and_fallback_objects(self) -> None:
        class _DictResult:
            def to_dict(self) -> dict[str, object]:
                return {"status": "clean"}

        class _BadDictResult:
            def to_dict(self) -> str:
                return "not a dict"

            def __str__(self) -> str:
                return "bad dict result"

        assert _target_reconcile_payload({"status": "updated"}) == {"status": "updated"}
        assert _target_reconcile_payload(_DictResult()) == {"status": "clean"}
        assert _target_reconcile_payload(_BadDictResult()) == {"result": "bad dict result"}
        assert _target_reconcile_payload(SimpleNamespace(status="unknown")) == {
            "result": "namespace(status='unknown')"
        }

    @pytest.mark.unit
    def test_ci_failure_replacement_preserves_status_shape(self) -> None:
        failure = CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom")
        updated = _with_ci_failures(_status(), (failure,))

        assert updated.ci_failures == (failure,)
        assert updated.head_sha == "abc123"

    @pytest.mark.unit
    async def test_write_monitor_log_swallows_sink_failures(
        self,
        tmp_path: Path,
    ) -> None:
        class _FailingSink:
            async def write(self, _payload: str) -> None:
                raise OSError("disk full")

        runner = _monitor_runner(tmp_path, FakeCommandRunner())

        await runner._write_monitor_log(_FailingSink(), {"event": "test"})  # type: ignore[arg-type]

    @pytest.mark.unit
    async def test_commit_dirty_worktree_branches(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        async def run_case(
            workspace_id: str,
            queued: list[dict[str, object]],
            *,
            make_worktree: bool = True,
        ) -> bool:
            fake = FakeCommandRunner()
            for result in queued:
                fake.queue_result(**result)
            runner = _monitor_runner(tmp_path, fake, session_factory=factory)
            worktree = runner._worktrees_root / workspace_id
            if make_worktree:
                worktree.mkdir(parents=True, exist_ok=True)
            return await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message="awf: monitor dirty worktree",
            )

        assert await run_case("ws_missing", [], make_worktree=False) is False
        assert (
            await run_case(
                "ws_status_failed",
                [{"returncode": 1, "stderr": "not a git repo"}],
            )
            is False
        )
        assert (
            await run_case(
                "ws_clean",
                [{"returncode": 0, "stdout": ""}],
            )
            is False
        )
        assert (
            await run_case(
                "ws_add_failed",
                [
                    {"returncode": 0, "stdout": " M file.py\n"},
                    {"returncode": 0, "stdout": " M file.py\n"},
                    {"returncode": 1, "stderr": "add failed"},
                ],
            )
            is False
        )
        assert (
            await run_case(
                "ws_stage_status_failed",
                [
                    {"returncode": 0, "stdout": " M file.py\n"},
                    {"returncode": 1, "stderr": "status failed"},
                ],
            )
            is False
        )
        assert (
            await run_case(
                "ws_cached_clean",
                [
                    {"returncode": 0, "stdout": " M file.py\n"},
                    {"returncode": 0, "stdout": " M file.py\n"},
                    {"returncode": 0},
                    {"returncode": 0},
                ],
            )
            is False
        )
        assert (
            await run_case(
                "ws_commit_failed",
                [
                    {"returncode": 0, "stdout": " M file.py\n"},
                    {"returncode": 0, "stdout": " M file.py\n"},
                    {"returncode": 0},
                    {"returncode": 1},
                    {"returncode": 1, "stderr": "commit failed"},
                ],
            )
            is False
        )
        assert (
            await run_case(
                "ws_committed",
                [
                    {"returncode": 0, "stdout": " M file.py\n"},
                    {"returncode": 0, "stdout": " M file.py\n"},
                    {"returncode": 0},
                    {"returncode": 1},
                    {"returncode": 0},
                ],
            )
            is True
        )

    @pytest.mark.unit
    async def test_commit_dirty_worktree_prepends_task_tag(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """The monitor dirty-worktree commit carries the workspace's Jira issue key."""
        workspace_id = await seed_monitoring_workspace(factory)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.task_tag = "PROJ-123"
            await session.commit()

        fake = FakeCommandRunner()
        for result in (
            {"returncode": 0, "stdout": " M file.py\n"},  # status --porcelain (dirty)
            {"returncode": 0, "stdout": " M file.py\n"},  # status --untracked-files=all
            {"returncode": 0},  # add -A -- <stage_paths>
            {"returncode": 1},  # diff --cached --quiet (staged changes present)
            {"returncode": 0},  # commit
        ):
            fake.queue_result(**result)
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="awf: monitor dirty worktree",
        )

        assert committed is True
        commit_calls = [call for call in fake.calls if "commit" in call.args and "-m" in call.args]
        assert commit_calls, "expected a git commit invocation"
        message = commit_calls[-1].args[commit_calls[-1].args.index("-m") + 1]
        assert message == "PROJ-123 awf: monitor dirty worktree"

    @pytest.mark.unit
    async def test_commit_dirty_worktree_excludes_untracked_agent_runtime_memory(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Pre-existing reviewer subagent memory is never staged into the PR commit.

        The pre-existing-dirty guard lets a repair run when the only dirt is an
        untracked ``.claude/agent-memory/`` file; the commit path must apply the
        same exclusion so ``git add`` never re-stages that memory alongside a real
        fix (PRRT_kwDOSJAM6s6JXd4I).
        """
        workspace_id = "ws_memory_exclusion"
        fake = FakeCommandRunner()
        dirty = " M src/awf/foo.py\n?? .claude/agent-memory/reviewer/bug.md\n"
        for result in (
            {"returncode": 0, "stdout": dirty},  # status --porcelain
            {"returncode": 0, "stdout": dirty},  # status --untracked-files=all
            {"returncode": 0},  # add -A -- <stage_paths>
            {"returncode": 1},  # diff --cached --quiet (staged changes present)
            {"returncode": 0},  # commit
        ):
            fake.queue_result(**result)
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="awf: monitor dirty worktree",
        )

        assert committed is True
        add_calls = [call for call in fake.calls if "add" in call.args]
        assert add_calls, "expected a git add invocation"
        add_args = add_calls[-1].args
        assert "src/awf/foo.py" in add_args
        assert not any(arg.startswith(".claude/agent-memory") for arg in add_args)

    @pytest.mark.unit
    async def test_commit_dirty_worktree_memory_only_skips_commit_side_effects(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Memory-only dirt short-circuits before any commit-side effects.

        The initial dirty check runs with ``--untracked-files=all`` and drops the
        untracked agent-runtime artifact, so a worktree dirtied only by reviewer
        subagent memory returns False after a single ``git status`` — never entering
        the supply-chain policy refresh, agent-runtime ownership repair, or
        protected-scope repair (which can launch the agent CLI) that the
        pre-existing-dirty guard and staging logic intentionally skip. Cursor BUGBOT
        follow-up to PRRT_kwDOSJAM6s6JXd4I: the plain ``--porcelain`` check used here
        previously let memory-only dirt fall through into those side effects.
        """
        workspace_id = "ws_memory_only"
        fake = FakeCommandRunner()
        # ``--untracked-files=all`` enumerates the leaf path; the agent-runtime filter
        # then drops it, leaving nothing PR-worthy.
        fake.queue_result(returncode=0, stdout="?? .claude/agent-memory/reviewer/bug.md\n")
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="awf: monitor dirty worktree",
        )

        assert committed is False
        # Exactly one git command — the initial ``--untracked-files=all`` dirty
        # check — runs: no second status, no add, no commit, no protected-scope work.
        assert len(fake.calls) == 1
        assert fake.calls[0].args[-3:] == ["status", "--porcelain", "--untracked-files=all"]
        assert not any("add" in call.args for call in fake.calls)
        assert not any("commit" in call.args for call in fake.calls)

    @pytest.mark.unit
    async def test_commit_dirty_worktree_truncates_subject_to_72(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """The monitor dirty-worktree commit subject is capped at 72 chars after tagging.

        Parity with every other AWF-authored commit subject (executor agent/recovery
        commits, post-validation conformance) which all apply ``[:72]`` after the tag.
        """
        workspace_id = await seed_monitoring_workspace(factory)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.task_tag = "PROJ-123"
            await session.commit()

        fake = FakeCommandRunner()
        for result in (
            {"returncode": 0, "stdout": " M file.py\n"},  # status --porcelain (dirty)
            {"returncode": 0, "stdout": " M file.py\n"},  # status --untracked-files=all
            {"returncode": 0},  # add -A -- <stage_paths>
            {"returncode": 1},  # diff --cached --quiet (staged changes present)
            {"returncode": 0},  # commit
        ):
            fake.queue_result(**result)
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

        long_subject = "fix: address PR review comment " + "x" * 80
        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=long_subject,
        )

        assert committed is True
        commit_calls = [call for call in fake.calls if "commit" in call.args and "-m" in call.args]
        assert commit_calls, "expected a git commit invocation"
        message = commit_calls[-1].args[commit_calls[-1].args.index("-m") + 1]
        assert len(message) == 72
        assert message == ("PROJ-123 " + long_subject)[:72]


@pytest.mark.unit
async def test_monitor_recovery_dispatch_records_operation_with_pr_and_sha_context(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 77
    head_sha = "d" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)

    terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )

    assert terminal is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
        recovery_events = [
            event for event in workspace.events if event.event_type == "monitor.recovery_dispatched"
        ]
        state_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.state_changed"
            and event.reason_code == "RECOVERY_DISPATCH"
        ]
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    operation = operations[0]
    assert operation.type == "validate"
    assert operation.status == OperationStatus.pending.value
    assert operation.idempotency_key is not None
    assert operation.idempotency_key.startswith("pr_monitor:validate_only:")
    assert len(operation.idempotency_key) <= 128
    assert operation.payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "validate_only",
        "requested_action": "validate",
        "reason": "Required validation tier has not passed for this merge candidate.",
        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        "stale_reason": "validation_insufficient_tier",
        "recovery_mode": "validate_only",
        "pr_number": pr_number,
        "pr_url": f"https://github.com/dimileeh/aira-web/pull/{pr_number}",
        "source_head_sha": head_sha,
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
    }
    assert len(recovery_events) == 1
    assert recovery_events[0].reason_code == "RECOVERY_DISPATCH"
    assert recovery_events[0].payload == {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reason": "validation_insufficient_tier",
        "req_action": "validate",
        "recovery_mode": "validate_only",
    }
    assert len(state_events) == 1
    assert state_events[0].old_state == WorkspaceStatus.monitoring_pr.value
    assert state_events[0].new_state == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_monitor_recovery_dispatch_preserves_planning_validation_handoff_context(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 78
    head_sha = "e" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)
    plan_path = f"docs/awf-plans/{workspace_id}.md"
    report_path = f"docs/awf-plans/{workspace_id}.conformance.json"
    async with factory() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        await workspace_repo.add_event(
            workspace,
            event_type="workspace.planning_conformance_requires_awf_validation",
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
            payload={
                "summary": "AWF validation evidence is required before conformance can pass.",
                "gaps": ["AWF-owned validation evidence is missing for the pytest gate."],
                "report_reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "plan_path": plan_path,
                "report_path": report_path,
                "iteration": 1,
                "max_iterations": 3,
            },
        )
        await session.commit()

    terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is True
    assert len(operations) == 1
    assert operations[0].payload["conformance"] == {
        "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
        "report_reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
        "summary": "AWF validation evidence is required before conformance can pass.",
        "gaps": ["AWF-owned validation evidence is missing for the pytest gate."],
        "plan_path": plan_path,
        "report_path": report_path,
        "iteration": 1,
        "max_iterations": 3,
    }


@pytest.mark.unit
async def test_monitor_recovery_dispatch_omits_satisfied_planning_validation_handoff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 79
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)
    async with factory() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        await workspace_repo.add_event(
            workspace,
            event_type="workspace.planning_conformance_requires_awf_validation",
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
            payload={
                "summary": "AWF validation evidence is required before conformance can pass.",
                "gaps": ["AWF-owned validation evidence is missing for the pytest gate."],
                "report_reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "plan_path": f"docs/awf-plans/{workspace_id}.md",
                "report_path": f"docs/awf-plans/{workspace_id}.conformance.json",
                "iteration": 0,
                "max_iterations": 3,
            },
        )
        await workspace_repo.add_event(
            workspace,
            event_type="workspace.post_validation_conformance_satisfied",
            reason_code="PLAN_CONFORMANCE_SATISFIED",
            payload={
                "summary": "validation evidence satisfied the plan",
                "plan_path": f"docs/awf-plans/{workspace_id}.md",
                "report_path": f"docs/awf-plans/{workspace_id}.conformance.json",
                "validation_run_id": "val-resolved",
            },
        )
        await session.commit()

    terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is True
    assert len(operations) == 1
    assert "conformance" not in operations[0].payload


@pytest.mark.unit
async def test_monitor_runner_loads_persisted_state_on_resume(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 91
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha="f" * 40,
    )
    monitor_started_at = datetime.now(UTC) - timedelta(minutes=12)
    review_started_at = datetime.now(UTC) - timedelta(minutes=7)
    grace_started_key = _initial_review_grace_started_key(pr_number)
    persisted_threads = {
        "thread-1": "fix_committed",
        "thread-2": "defer",
        grace_started_key: _initial_review_grace_wall_started_value_from_datetime(
            review_started_at
        ),
    }
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_iter_count = 8
        workspace.monitor_threads_addressed = dict(persisted_threads)
        workspace.monitor_last_commit_sha = "e" * 40
        workspace.monitor_started_at = monitor_started_at
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    before = time.monotonic()
    workspace = await runner._load_workspace(workspace_id)
    state = runner._load_state(workspace)
    after = time.monotonic()

    assert state.iter_count == 8
    assert state.last_push_sha == "e" * 40
    assert state.threads_addressed_ids["thread-1"] == "fix_committed"
    assert state.threads_addressed_ids["thread-2"] == "defer"

    monitor_elapsed = (datetime.now(UTC) - monitor_started_at).total_seconds()
    assert before - monitor_elapsed - 1 <= state.started_at <= after - monitor_elapsed + 1

    grace_elapsed = (datetime.now(UTC) - review_started_at).total_seconds()
    grace_runtime_started = float(state.threads_addressed_ids[grace_started_key])
    assert before - grace_elapsed - 1 <= grace_runtime_started <= after - grace_elapsed + 1


@pytest.mark.unit
async def test_validation_recovery_dispatch_is_idempotent_for_duplicate_tick_replay(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 78
    head_sha = "e" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)

    first_terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    replay_sleep = RecordedSleep()
    replay_terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
        sleep_fn=replay_sleep,
    )

    assert first_terminal is True
    assert replay_terminal is False
    assert replay_sleep.calls == [60]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
        recovery_events = [
            event for event in workspace.events if event.event_type == "monitor.recovery_dispatched"
        ]
    recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
    wait_operations = [
        op
        for op in operations
        if op.type == OperationType.monitor_state.value
        and op.payload.get("reason_code") == "RECOVERY_IN_PROGRESS"
    ]
    assert len(recovery_operations) == 1
    assert len(wait_operations) == 1
    assert recovery_operations[0].idempotency_key is not None
    assert recovery_operations[0].idempotency_key.startswith("pr_monitor:validate_only:")
    assert len(recovery_operations[0].idempotency_key) <= 128
    assert wait_operations[0].status == OperationStatus.succeeded.value
    assert wait_operations[0].payload["action"] == "recovery_wait"
    assert wait_operations[0].payload["requested_action"] == "validate"
    assert wait_operations[0].payload["wait_seconds"] == 60
    assert wait_operations[0].payload["recovery_mode"] == "validate_only"
    assert wait_operations[0].payload["stale_reason"] == "validation_insufficient_tier"
    assert wait_operations[0].result == {
        "status": "succeeded",
        "outcome": "wait_elapsed",
        "slept_seconds": 60,
    }
    assert len(recovery_events) == 1


@pytest.mark.unit
async def test_late_validation_recovery_callback_records_stale_ready_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 79
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)
    async with factory() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        await workspace_repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code="TEST_READY_AFTER_RECOVERY_DISPATCH",
        )
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

    terminal = await runner._handle_merge_gate_blocker(
        gate=_MergeGateResult(
            workspace=workspace,
            stale_reason="validation_insufficient_tier",
            req_action="validate",
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        stale_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.stale_callback_ignored"
        ]
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert len(stale_events) == 1
    assert stale_events[0].reason_code == "STALE_CALLBACK_IGNORED"
    assert stale_events[0].payload["callback_action"] == "recovery_dispatch"
    assert stale_events[0].payload["actual_status"] == WorkspaceStatus.ready.value
    assert [op for op in operations if op.type == OperationType.validate.value] == []


@pytest.mark.unit
async def test_review_comment_provider_failure_records_retry_and_ignores_comment(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        max_same_provider_retries=1,
    )

    mocker.patch(
        "awf.runtime.pr_monitor_runner.provider_ops.create_provider_recovery_attempt_row",
        return_value=None,
    )

    adapter = FakeAdapter()
    adapter.queue(
        returncode=1,
        stderr="Gemini RESOURCE_EXHAUSTED: provider is temporarily overloaded",
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)

    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    c = ReviewComment(
        comment_id="C_provider",
        body_excerpt="please fix",
        author="bot",
    )
    status = _status(reviews=(c,))
    state = MonitorState(started_at=0.0)

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._execute(
            action=AddressComments(threads=(), review_comments=(c,)),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=status,
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    assert "C_provider" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_comment_repair_idle_timeout_uses_in_place_monitor_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        max_same_provider_retries=0,
    )

    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while addressing comments",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    comment = ReviewComment(
        comment_id="C_idle_timeout",
        body_excerpt="please fix",
        author="review-bot",
    )
    state = MonitorState(started_at=0.0)

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._execute(
            action=AddressComments(threads=(), review_comments=(comment,)),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_status(reviews=(comment,)),
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    source_policy, recovery_events, operations, requested_ids = await _provider_recovery_snapshot(
        factory,
        workspace_id,
    )
    comment_ops = [operation for operation in operations if operation.type == "comment_repair"]

    assert "C_idle_timeout" not in state.threads_addressed_ids
    assert requested_ids == []
    assert source_policy["provider_recovery_state"]["action"] == "fallback"
    assert source_policy["provider_recovery_state"]["target_agent"] == "codex"
    assert len(recovery_events) == 1
    assert "new_workspace_id" not in recovery_events[0]
    assert recovery_events[0]["provider_recovery"]["decision_reason_code"] == (
        "PROVIDER_FALLBACK_SELECTED"
    )
    assert len(comment_ops) == 1
    assert comment_ops[0].status == OperationStatus.failed.value
    assert comment_ops[0].result["outcome"] == "provider_retry"


@pytest.mark.unit
async def test_provider_failure_stale_callback_is_deterministic(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(factory, workspace_id)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(
            workspace,
            to=WorkspaceStatus.completed,
            reason_code="TEST_COMPLETED",
        )
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    exc = AgentRunError(
        agent=AgentRuntime.claude_code,
        result=CommandResult(
            returncode=1,
            stdout="",
            stderr="Gemini RESOURCE_EXHAUSTED: provider is temporarily overloaded",
        ),
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        details={"provider": "google", "model": "gemini-2.5-pro"},
    )

    action = await runner._record_provider_agent_run_error(workspace_id, exc)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        event_types = [event.event_type for event in workspace.events]

    assert action == "deterministic"
    assert "workspace.stale_callback_ignored" in event_types
    assert "workspace.provider_recovery_requested" not in event_types


@pytest.mark.unit
async def test_review_comment_deterministic_failure_is_marked_addressed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        returncode=1,
        stderr="Syntax error: invalid character",
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)

    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    c = ReviewComment(
        comment_id="C_deterministic",
        body_excerpt="please fix syntax",
        author="bot",
    )
    status = _status(reviews=(c,))
    state = MonitorState(started_at=0.0)

    terminal = await runner._execute(
        action=AddressComments(threads=(), review_comments=(c,)),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert "C_deterministic" in state.threads_addressed_ids
    assert state.threads_addressed_ids["C_deterministic"] == "agent_failed"
