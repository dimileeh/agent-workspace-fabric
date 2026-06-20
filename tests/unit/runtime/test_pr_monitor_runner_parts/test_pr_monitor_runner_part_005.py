"""Unit tests for focused ``pr_monitor_runner`` behavior.

Most cases cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). Focused
runtime-path regressions live here when the unit suite needs to cover a
specific merge-gate branch without running the full monitor integration loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.runtime.pr_monitor_runner.remote_repair as remote_repair
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import (
    TaskClass,
    WorkspaceStatus,
)
from awf.db.models import Operation, Workspace
from awf.db.repositories import (
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
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
    _MonitorHeadObjectMissingError,
    _MonitorPolicyBlockedError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor import _status


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture(autouse=True)
def _mock_verify_head_object_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )


@pytest.mark.unit
async def test_resolve_worktree_branch_ref_strips_git_object_lookup_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    calls: list[dict[str, object]] = []

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"refs/heads/agent-work\n", b""

    async def _create_subprocess_exec(*args: object, **kwargs: object) -> _Proc:
        calls.append({"args": args, "env": kwargs.get("env")})
        return _Proc()

    monkeypatch.setattr(remote_repair.asyncio, "create_subprocess_exec", _create_subprocess_exec)

    branch_ref = await remote_repair._resolve_worktree_branch_ref(tmp_path)

    assert branch_ref == "refs/heads/agent-work"
    assert calls
    assert calls[0]["args"][-2:] == ("symbolic-ref", "HEAD")
    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert "GIT_OBJECT_DIRECTORY" not in env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env


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


@pytest.mark.unit
async def test_repair_operation_start_head_rejects_dangling_no_mirror_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    fallback_head = "f" * 40
    fake = FakeCommandRunner()
    fake.queue_result(returncode=1, stderr="HEAD is unreadable")
    fake.queue_result(returncode=1, stderr="fallback is missing")
    runner = _monitor_runner(tmp_path, fake)
    worktree_path = tmp_path / "repair-worktree"
    worktree_path.mkdir()

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    monkeypatch.setattr(remote_repair, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(
        remote_repair,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )

    operation_start_head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_dangling_fallback",
        worktree_path=worktree_path,
        operation_type="comment_repair",
        fallback_head_sha=fallback_head,
    )

    assert operation_start_head == ""
    assert result is not None
    assert result.failed is True
    assert result.details["fallback_head_sha"] == fallback_head
    assert result.details["fallback_source"] == "status"
    assert fake.calls[1].args[-3:] == ["cat-file", "-e", f"{fallback_head}^{{commit}}"]
    assert fake.calls[1].env is not None
    assert "GIT_OBJECT_DIRECTORY" not in fake.calls[1].env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in fake.calls[1].env


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
    async def test_commit_dirty_worktree_strips_git_object_env_from_write_path(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dirty-worktree writes never inherit alternate object stores."""
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "awf-agent@example.invalid")
        workspace_id = "ws_object_env"
        fake = FakeCommandRunner()
        fixed_path = "src/awf/foo.py"
        dirty = f" M {fixed_path}\n"
        hook_stderr = (
            "fix end of files................................................Failed\n"
            "- hook id: end-of-file-fixer\n"
            "- exit code: 1\n"
            "- files were modified by this hook\n\n"
            f"Fixing {fixed_path}\n"
        )
        for result in (
            {"returncode": 0, "stdout": dirty},  # status --porcelain
            {"returncode": 0, "stdout": dirty},  # status --untracked-files=all
            {"returncode": 0},  # add -A -- <stage_paths>
            {"returncode": 1},  # diff --cached --quiet (staged changes present)
            {"returncode": 1, "stderr": hook_stderr},  # commit runs an autofixing hook
            {"returncode": 0, "stdout": dirty},  # retry status
            {"returncode": 0},  # retry add
            {"returncode": 0},  # retry commit
        ):
            fake.queue_result(**result)
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)
        repair_reasons: list[str] = []

        async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
            repair_reasons.append(str(kwargs["reason"]))
            return True

        monkeypatch.setattr(
            remote_repair,
            "repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
        )

        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="awf: monitor dirty worktree",
        )

        assert committed is True
        git_calls = [
            call
            for call in fake.calls
            if {"status", "add", "diff", "commit"}.intersection(call.args)
        ]
        assert len(git_calls) == 8
        for call in git_calls:
            assert call.env is not None
            assert "GIT_OBJECT_DIRECTORY" not in call.env
            assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in call.env
            assert call.env["GIT_AUTHOR_EMAIL"] == "awf-agent@example.invalid"
        assert repair_reasons == [
            "dirty_worktree_pre_commit",
            "dirty_worktree_post_commit_failed",
            "dirty_worktree_post_commit_succeeded",
        ]

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
    async def test_commit_dirty_worktree_missing_head_recovery_same_head_returns_false(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No-op missing-HEAD recovery is not a committed fix."""
        workspace_id = "ws_missing_head_noop"
        operation_start_head = "1" * 40
        fake = FakeCommandRunner()
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

        async def _verify_head_object_exists(_worktree_path: Path) -> bool:
            return False

        async def _recover_missing_head_object_from_filesystem(
            *_args: object,
            **_kwargs: object,
        ) -> str:
            return operation_start_head

        monkeypatch.setattr(
            remote_repair,
            "verify_head_object_exists",
            _verify_head_object_exists,
        )
        monkeypatch.setattr(
            remote_repair,
            "_recover_missing_head_object_from_filesystem",
            _recover_missing_head_object_from_filesystem,
        )

        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="awf: monitor dirty worktree",
            operation_start_head=operation_start_head,
        )

        assert committed is False

    @pytest.mark.unit
    async def test_commit_dirty_worktree_missing_head_falls_back_from_stale_start_head(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dangling operation-start SHA does not block candidate-head recovery."""
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
        workspace_id = "ws_missing_head_stale_anchor"
        stale_operation_start_head = "1" * 40
        candidate_head = "2" * 40
        mirror_path = tmp_path / "mirror.git"
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="missing")
        fake.queue_result(returncode=0)
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)
        captured_recovery_heads: list[str] = []

        async def _verify_head_object_exists(_worktree_path: Path) -> bool:
            return False

        async def _repair_mirror_hooks_path(_mirror_path: Path) -> None:
            return None

        async def _open_merge_candidate_head_sha(*_args: object) -> str:
            return candidate_head

        async def _recover_missing_head_object_from_filesystem(
            *_args: object,
            **kwargs: object,
        ) -> str:
            recovery_head = str(kwargs["operation_start_head"])
            captured_recovery_heads.append(recovery_head)
            return recovery_head

        monkeypatch.setattr(
            remote_repair,
            "verify_head_object_exists",
            _verify_head_object_exists,
        )
        monkeypatch.setattr(
            remote_repair,
            "repair_mirror_hooks_path",
            _repair_mirror_hooks_path,
        )
        monkeypatch.setattr(
            remote_repair,
            "mirror_path_for_worktree",
            lambda _worktree_path: mirror_path,
        )
        monkeypatch.setattr(
            remote_repair,
            "_open_merge_candidate_head_sha",
            _open_merge_candidate_head_sha,
        )
        monkeypatch.setattr(
            remote_repair,
            "_recover_missing_head_object_from_filesystem",
            _recover_missing_head_object_from_filesystem,
        )

        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="awf: monitor dirty worktree",
            operation_start_head=stale_operation_start_head,
            task_tag=None,
        )

        assert committed is False
        assert captured_recovery_heads == [candidate_head]
        assert fake.calls[0].args[-3:] == [
            "cat-file",
            "-e",
            f"{stale_operation_start_head}^{{commit}}",
        ]
        assert fake.calls[1].args[-3:] == ["cat-file", "-e", f"{candidate_head}^{{commit}}"]
        assert fake.calls[0].env is not None
        assert "GIT_OBJECT_DIRECTORY" not in fake.calls[0].env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in fake.calls[0].env
        assert fake.calls[1].env is not None
        assert "GIT_OBJECT_DIRECTORY" not in fake.calls[1].env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in fake.calls[1].env

    @pytest.mark.unit
    async def test_commit_dirty_worktree_no_mirror_prefers_verified_operation_start_before_candidate(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No-mirror missing-HEAD recovery does not skip a valid start anchor."""
        workspace_id = "ws_missing_head_no_mirror_prefers_start"
        operation_start_head = "1" * 40
        candidate_head = "2" * 40
        fake = FakeCommandRunner()
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)
        checked_anchors: list[str] = []
        opened_candidates: list[str] = []
        captured_recovery_heads: list[str] = []

        async def _verify_head_object_exists(_worktree_path: Path) -> bool:
            return False

        async def _open_merge_candidate_head_sha(_self: object, opened_workspace_id: str) -> str:
            opened_candidates.append(opened_workspace_id)
            return candidate_head

        async def _worktree_commit_object_exists(
            _self: object,
            _worktree_path: Path,
            commit_sha: str,
        ) -> bool:
            checked_anchors.append(commit_sha)
            return commit_sha == operation_start_head

        async def _recover_missing_head_object_from_filesystem(
            *_args: object,
            **kwargs: object,
        ) -> str:
            recovery_head = str(kwargs["operation_start_head"])
            captured_recovery_heads.append(recovery_head)
            return recovery_head

        monkeypatch.setattr(
            remote_repair,
            "verify_head_object_exists",
            _verify_head_object_exists,
        )
        monkeypatch.setattr(remote_repair, "mirror_path_for_worktree", lambda _worktree_path: None)
        monkeypatch.setattr(
            remote_repair,
            "_open_merge_candidate_head_sha",
            _open_merge_candidate_head_sha,
        )
        monkeypatch.setattr(
            remote_repair,
            "_worktree_commit_object_exists",
            _worktree_commit_object_exists,
        )
        monkeypatch.setattr(
            remote_repair,
            "_recover_missing_head_object_from_filesystem",
            _recover_missing_head_object_from_filesystem,
        )

        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="awf: monitor dirty worktree",
            operation_start_head=operation_start_head,
            task_tag=None,
        )

        assert committed is False
        assert checked_anchors == [operation_start_head]
        assert opened_candidates == []
        assert captured_recovery_heads == [operation_start_head]

    @pytest.mark.unit
    async def test_commit_dirty_worktree_mirror_rejects_unverified_candidate_head(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A mirror-missing candidate SHA must not be used for filesystem recovery."""
        workspace_id = "ws_missing_head_mirror_unverified"
        stale_operation_start_head = "1" * 40
        mirror_path = tmp_path / "mirror.git"
        fake = FakeCommandRunner()
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)
        checked_anchors: list[str] = []

        async def _verify_head_object_exists(_worktree_path: Path) -> bool:
            return False

        async def _repair_mirror_hooks_path(_mirror_path: Path) -> None:
            return None

        async def _open_merge_candidate_head_sha(*_args: object) -> str:
            return stale_operation_start_head

        async def _mirror_commit_object_exists(
            _self: object,
            _mirror_path: Path,
            commit_sha: str,
        ) -> bool:
            checked_anchors.append(commit_sha)
            return False

        async def _recover_missing_head_object_from_filesystem(
            *_args: object,
            **_kwargs: object,
        ) -> str:
            raise AssertionError("unverified mirror candidate must not be recovered")

        monkeypatch.setattr(
            remote_repair,
            "verify_head_object_exists",
            _verify_head_object_exists,
        )
        monkeypatch.setattr(
            remote_repair,
            "repair_mirror_hooks_path",
            _repair_mirror_hooks_path,
        )
        monkeypatch.setattr(
            remote_repair,
            "mirror_path_for_worktree",
            lambda _worktree_path: mirror_path,
        )
        monkeypatch.setattr(
            remote_repair,
            "_open_merge_candidate_head_sha",
            _open_merge_candidate_head_sha,
        )
        monkeypatch.setattr(
            remote_repair,
            "_mirror_commit_object_exists",
            _mirror_commit_object_exists,
        )
        monkeypatch.setattr(
            remote_repair,
            "_recover_missing_head_object_from_filesystem",
            _recover_missing_head_object_from_filesystem,
        )

        with pytest.raises(_MonitorHeadObjectMissingError):
            await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message="awf: monitor dirty worktree",
                operation_start_head=stale_operation_start_head,
                task_tag=None,
            )

        assert checked_anchors == [stale_operation_start_head]
        assert fake.calls == []

    @pytest.mark.unit
    async def test_commit_dirty_worktree_no_mirror_rejects_unverified_candidate_head(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No-mirror missing-HEAD recovery must prove the selected candidate exists."""
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
        workspace_id = "ws_missing_head_no_mirror_unverified"
        stale_operation_start_head = "1" * 40
        candidate_head = "2" * 40
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="stale operation start missing")
        fake.queue_result(returncode=1, stderr="candidate missing")
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

        async def _verify_head_object_exists(_worktree_path: Path) -> bool:
            return False

        async def _open_merge_candidate_head_sha(*_args: object) -> str:
            return candidate_head

        async def _recover_missing_head_object_from_filesystem(
            *_args: object,
            **_kwargs: object,
        ) -> str:
            raise AssertionError("unverified no-mirror candidate must not be recovered")

        monkeypatch.setattr(
            remote_repair,
            "verify_head_object_exists",
            _verify_head_object_exists,
        )
        monkeypatch.setattr(remote_repair, "mirror_path_for_worktree", lambda _worktree_path: None)
        monkeypatch.setattr(
            remote_repair,
            "_open_merge_candidate_head_sha",
            _open_merge_candidate_head_sha,
        )
        monkeypatch.setattr(
            remote_repair,
            "_recover_missing_head_object_from_filesystem",
            _recover_missing_head_object_from_filesystem,
        )

        with pytest.raises(_MonitorHeadObjectMissingError):
            await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message="awf: monitor dirty worktree",
                operation_start_head=stale_operation_start_head,
                task_tag=None,
            )

        assert len(fake.calls) == 2
        assert fake.calls[0].args[-3:] == [
            "cat-file",
            "-e",
            f"{stale_operation_start_head}^{{commit}}",
        ]
        assert fake.calls[1].args[-3:] == ["cat-file", "-e", f"{candidate_head}^{{commit}}"]
        for call in fake.calls:
            assert call.env is not None
            assert "GIT_OBJECT_DIRECTORY" not in call.env
            assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in call.env

    @pytest.mark.unit
    async def test_commit_dirty_worktree_missing_head_recovery_runtime_only_returns_false(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Runtime-only recovered diffs are not PR-worthy committed fixes."""
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
        workspace_id = "ws_missing_head_runtime_only"
        operation_start_head = "1" * 40
        recovered_head = "2" * 40
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="M\0.claude/agent-memory/reviewer/bug.md\0")
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

        async def _verify_head_object_exists(_worktree_path: Path) -> bool:
            return False

        async def _recover_missing_head_object_from_filesystem(
            *_args: object,
            **_kwargs: object,
        ) -> str:
            return recovered_head

        async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
            raise AssertionError("runtime-only recovery should skip ownership repair")

        monkeypatch.setattr(
            remote_repair,
            "verify_head_object_exists",
            _verify_head_object_exists,
        )
        monkeypatch.setattr(
            remote_repair,
            "_recover_missing_head_object_from_filesystem",
            _recover_missing_head_object_from_filesystem,
        )
        monkeypatch.setattr(
            remote_repair,
            "repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
        )

        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="awf: monitor dirty worktree",
            operation_start_head=operation_start_head,
        )

        assert committed is False
        assert len(fake.calls) == 1
        assert fake.calls[0].args[-5:] == [
            "diff",
            "--name-status",
            "-z",
            f"{operation_start_head}..{recovered_head}",
            "--",
        ]
        assert fake.calls[0].env is not None
        assert "GIT_OBJECT_DIRECTORY" not in fake.calls[0].env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in fake.calls[0].env

    @pytest.mark.unit
    async def test_commit_dirty_worktree_missing_head_recovery_blocks_protected_commit(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recovered commits validate protected scope as committed diffs.

        Regression for PRRT_kwDOSJAM6s6KzfXh: after filesystem recovery advances
        HEAD, dirty-status diff loading compares the new HEAD to the matching
        worktree and cannot see protected-file changes in the recovered commit.
        This must block even when no compose context exists for a repair pass.
        """
        operation_start_head = "1" * 40
        workspace_id = await seed_monitoring_workspace(factory, head_sha=operation_start_head)
        recovered_head = "2" * 40
        protected_path = ".github/workflows/ci.yml"
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=f"M\0{protected_path}\0")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="name: ci\npermissions: read-all\n")
        fake.queue_result(returncode=0)
        fake.queue_result(
            returncode=0,
            stdout="name: ci\npermissions: write-all\n",
        )
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

        async def _verify_head_object_exists(_worktree_path: Path) -> bool:
            return False

        async def _recover_missing_head_object_from_filesystem(
            *_args: object,
            **_kwargs: object,
        ) -> str:
            return recovered_head

        async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
            return True

        monkeypatch.setattr(
            remote_repair,
            "verify_head_object_exists",
            _verify_head_object_exists,
        )
        monkeypatch.setattr(
            remote_repair,
            "_recover_missing_head_object_from_filesystem",
            _recover_missing_head_object_from_filesystem,
        )
        monkeypatch.setattr(
            remote_repair,
            "repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
        )

        with pytest.raises(_MonitorPolicyBlockedError) as exc_info:
            await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message="awf: monitor dirty worktree",
                operation_start_head=operation_start_head,
            )

        assert protected_path in str(exc_info.value)
        assert len(fake.calls) == 6
        assert fake.calls[0].args[-5:] == [
            "diff",
            "--name-status",
            "-z",
            f"{operation_start_head}..{recovered_head}",
            "--",
        ]
        assert any(f"{operation_start_head}:{protected_path}" in call.args for call in fake.calls)
        assert any(f"HEAD:{protected_path}" in call.args for call in fake.calls)
        assert fake.calls[-1].args[-3:] == ["reset", "--hard", operation_start_head]

    @pytest.mark.unit
    async def test_commit_dirty_worktree_missing_head_recovery_includes_rename_sources(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recovered missing-HEAD diffs preserve rename sources for policy gates."""
        workspace_id = "ws_missing_head_rename_source"
        operation_start_head = "1" * 40
        recovered_head = "2" * 40
        source_path = ".github/workflows/ci.yml"
        destination_path = "docs/ci.yml"
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=f"R100\0{source_path}\0{destination_path}\0",
        )
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)
        captured_changed_paths: list[tuple[str, ...]] = []

        async def _verify_head_object_exists(_worktree_path: Path) -> bool:
            return False

        async def _recover_missing_head_object_from_filesystem(
            *_args: object,
            **_kwargs: object,
        ) -> str:
            return recovered_head

        async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
            return True

        async def _protected_scope_violations_for_recovered_dirty_commit(
            *_args: object,
            changed_paths: tuple[str, ...],
            **_kwargs: object,
        ) -> list[object]:
            captured_changed_paths.append(changed_paths)
            return []

        monkeypatch.setattr(
            remote_repair,
            "verify_head_object_exists",
            _verify_head_object_exists,
        )
        monkeypatch.setattr(
            remote_repair,
            "_recover_missing_head_object_from_filesystem",
            _recover_missing_head_object_from_filesystem,
        )
        monkeypatch.setattr(
            remote_repair,
            "repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
        )
        monkeypatch.setattr(
            remote_repair,
            "_protected_scope_violations_for_recovered_dirty_commit",
            _protected_scope_violations_for_recovered_dirty_commit,
        )

        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="awf: monitor dirty worktree",
            operation_start_head=operation_start_head,
        )

        assert committed is True
        assert captured_changed_paths == [(source_path, destination_path)]
        assert fake.calls[0].args[-5:] == [
            "diff",
            "--name-status",
            "-z",
            f"{operation_start_head}..{recovered_head}",
            "--",
        ]
