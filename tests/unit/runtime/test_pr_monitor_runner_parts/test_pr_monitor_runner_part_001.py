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
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_mock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import (
    BITBUCKET_PIPELINE_NOT_RERUNNABLE,
    BitbucketClientError,
)
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.control.protected_file_diffs import git_show_text
from awf.db.enums import (
    OperationStatus,
    WorkspaceStatus,
)
from awf.db.models import ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON, Operation, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    StaleReasonCreate,
    StaleReasonRepository,
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
    RerunTransientCI,
    ReviewComment,
    WaitForCI,
    _ci_transient_rerun_state_key,
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult, _owned_paths_for_prompt
from awf.runtime.pr_monitor_runner.helpers import (
    _ci_transient_rerun_attempt,
    _initial_review_grace_started_key,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
)
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
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


@pytest.mark.unit
async def test_owned_paths_for_prompt_propagates_session_factory_type_error() -> None:
    """Verify owned-path prompt loading preserves session factory TypeError."""

    def _broken_session_factory() -> object:
        raise TypeError("session factory contract changed")

    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=_broken_session_factory))

    with pytest.raises(TypeError, match="session factory contract changed"):
        await _owned_paths_for_prompt(runner, "ws_1")  # type: ignore[arg-type]


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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_test_commands", "expected"),
    [
        (None, ()),
        ("pytest -q", ()),
        ({"command": "pytest -q"}, ()),
        (["ruff check .", 123, "pytest -q"], ("ruff check .", "pytest -q")),
        (("mypy src/awf", object()), ("mypy src/awf",)),
        (_CommandIterable(), ("pytest -q", "ruff check .")),
    ],
)
async def test_workspace_test_commands_ignores_null_and_malformed_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_test_commands: object,
    expected: tuple[str, ...],
) -> None:
    class _SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    class _WorkspaceRepository:
        def __init__(self, session: object) -> None:
            del session

        async def get(self, workspace_id: str) -> object:
            del workspace_id
            return SimpleNamespace(test_commands=raw_test_commands)

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.provider_ops.WorkspaceRepository",
        _WorkspaceRepository,
    )
    runner = _monitor_runner(tmp_path, FakeCommandRunner(), session_factory=_SessionContext)

    assert await runner._workspace_test_commands("ws_1") == expected


@pytest.mark.unit
async def test_git_show_text_marks_worktree_safe_directory(tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="old text")
    worktree = tmp_path / "worktree"

    show_text = await git_show_text(cmd, worktree_path=worktree, refspec="HEAD:README.md")

    assert show_text == "old text"
    assert [call.args for call in cmd.calls] == [
        [
            "git",
            "-c",
            f"safe.directory={worktree}",
            "-C",
            str(worktree),
            "cat-file",
            "-e",
            "HEAD:README.md",
        ],
        [
            "git",
            "-c",
            f"safe.directory={worktree}",
            "-C",
            str(worktree),
            "show",
            "HEAD:README.md",
        ],
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: path 'pyproject.toml' does not exist in 'HEAD'",
        "fatal: Path '.github/workflows/ci.yml' exists on disk, but not in 'HEAD'",
    ],
)
async def test_git_show_text_returns_none_for_missing_path(
    tmp_path: Path,
    stderr: str,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr=stderr)
    cmd.queue_result(returncode=0)
    worktree = tmp_path / "worktree"

    show_text = await git_show_text(cmd, worktree_path=worktree, refspec="HEAD:pyproject.toml")

    assert show_text is None


@pytest.mark.unit
async def test_git_show_text_raises_for_unexpected_git_failure(tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: bad revision 'bad-ref:pyproject.toml'")
    cmd.queue_result(returncode=1)
    worktree = tmp_path / "worktree"

    with pytest.raises(RuntimeError) as exc_info:
        await git_show_text(cmd, worktree_path=worktree, refspec="bad-ref:pyproject.toml")

    message = str(exc_info.value)
    assert "bad-ref:pyproject.toml" in message
    assert "bad revision" in message


@pytest.mark.unit
async def test_changed_paths_between_ref_and_head_includes_rename_sources(
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=0,
        stdout=(
            "M\0src/fix.py\0"
            "R100\0.github/workflows/ci.yml\0docs/ci.yml\0"
            "D\0pyproject.toml\0"
            "M\0src/fix.py\0"
        ),
    )
    runner = _monitor_runner(tmp_path, cmd)
    worktree = tmp_path / "worktree"

    paths = await runner._changed_paths_between_ref_and_head(
        worktree_path=worktree,
        ref="merge-base-sha",
        error_context="against the remote PR branch",
    )

    assert paths == (
        "src/fix.py",
        ".github/workflows/ci.yml",
        "docs/ci.yml",
        "pyproject.toml",
    )
    assert cmd.calls[0].args == [
        "git",
        "-c",
        f"safe.directory={worktree}",
        "-C",
        str(worktree),
        "diff",
        "--name-status",
        "-z",
        "merge-base-sha..HEAD",
        "--",
    ]


@pytest.mark.unit
async def test_protected_status_diff_for_deleted_file_keeps_head_text(
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout='[project]\nname = "demo"\n')
    runner = _monitor_runner(tmp_path, cmd)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    diffs = await runner._protected_file_diffs_for_status_paths(
        worktree_path=worktree,
        changed_paths=["pyproject.toml"],
    )

    diff = diffs["pyproject.toml"]
    assert diff.old_text == '[project]\nname = "demo"\n'
    assert diff.new_text is None
    assert cmd.calls[0].args == [
        "git",
        "-c",
        f"safe.directory={worktree}",
        "-C",
        str(worktree),
        "cat-file",
        "-e",
        "HEAD:pyproject.toml",
    ]
    assert cmd.calls[1].args == [
        "git",
        "-c",
        f"safe.directory={worktree}",
        "-C",
        str(worktree),
        "show",
        "HEAD:pyproject.toml",
    ]


@pytest.mark.unit
async def test_protected_status_diff_for_unreadable_file_fails_closed(
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout='[project]\nname = "demo"\n')
    runner = _monitor_runner(tmp_path, cmd)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "pyproject.toml").write_bytes(b"\xff")

    with pytest.raises(
        ProtectedScopeDiffError,
        match="Could not read protected worktree file 'pyproject.toml' as UTF-8",
    ):
        await runner._protected_file_diffs_for_status_paths(
            worktree_path=worktree,
            changed_paths=["pyproject.toml"],
        )


@pytest.mark.unit
async def test_address_review_comment_prompt_receives_workspace_runtime_context(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify review-comment prompts receive workspace runtime context."""
    context = "Workspace runtime context\n- Use `$AWF_TEST_DATABASE_URL`."
    runner = _monitor_runner(
        tmp_path,
        FakeCommandRunner(),
        session_factory=factory,
        workspace_runtime_context=context,
    )
    captured: dict[str, str] = {}

    async def _capture_verdict(**kwargs: object) -> VerdictResult:
        captured["prompt"] = str(kwargs["prompt"])
        return VerdictResult(verdict="false_positive", reason="covered")

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _capture_verdict)

    await runner._address_review_comment_result(
        workspace_id="ws_1",
        repo=RepoRef(owner="acme", name="repo"),
        pr_number=12,
        comment=ReviewComment(comment_id="issue:1", body_excerpt="please check DB test"),
        compose_project="awf_ws_1",
        compose_file=tmp_path / "compose.yml",
    )

    assert "Workspace runtime context" in captured["prompt"]
    assert "$AWF_TEST_DATABASE_URL" in captured["prompt"]


@pytest.mark.unit
async def test_rerun_transient_ci_action_requests_failed_job_rerun_and_records_attempt(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    failure = CheckFailure(
        name="python-full-coverage",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="25655330295",
    )
    status = _status(
        check_state=CheckState.FAILURE,
        ci_failures=(failure,),
        head_sha="abc1234567890def",
    )
    state_key = _ci_transient_rerun_state_key(status.head_sha, status.ci_failures)
    sleep_fn = PersistCheckingSleep(
        factory=factory,
        workspace_id=workspace_id,
        state_key=state_key,
        expected_value="1",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=RerunTransientCI(failures=(failure,)),
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
    assert adapter.calls == []
    assert cmd.calls[0].args == [
        "gh",
        "run",
        "rerun",
        "25655330295",
        "--repo",
        "dimileeh/aira-web",
        "--failed",
    ]
    assert sleep_fn.calls == [60]
    assert state.threads_addressed_ids[state_key] == "1"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.monitor_threads_addressed[state_key] == "1"
        events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.monitor_ci_transient_rerun_requested"
        ]
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["run_ids"] == ["25655330295"]
        operations = list((await session.execute(select(Operation))).scalars())
        rerun_operations = [
            op for op in operations if (op.payload or {}).get("action") == "ci_transient_rerun"
        ]
        assert len(rerun_operations) == 1
        assert rerun_operations[0].status == OperationStatus.succeeded.value


@pytest.mark.unit
async def test_rerun_transient_ci_operation_identity_includes_failure_signature(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    head_sha = "abc1234567890def"
    first_failure = CheckFailure(
        name="python-full-coverage",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="25655330295",
    )
    second_failure = CheckFailure(
        name="console-build",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="25655330295",
    )
    state = MonitorState()

    for failure in (first_failure, second_failure):
        terminal = await runner._execute(
            action=RerunTransientCI(failures=(failure,)),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_status(
                check_state=CheckState.FAILURE,
                ci_failures=(failure,),
                head_sha=head_sha,
            ),
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

        assert terminal is False

    first_state_key = _ci_transient_rerun_state_key(head_sha, (first_failure,))
    second_state_key = _ci_transient_rerun_state_key(head_sha, (second_failure,))
    assert state.threads_addressed_ids[first_state_key] == "1"
    assert state.threads_addressed_ids[second_state_key] == "1"
    assert [call.args[3] for call in cmd.calls] == ["25655330295", "25655330295"]
    assert sleep_fn.calls == [60, 60]

    async with factory() as session:
        operations = list((await session.execute(select(Operation))).scalars())

    rerun_operations = [
        op for op in operations if (op.payload or {}).get("action") == "ci_transient_rerun"
    ]
    wait_operations = [
        op for op in operations if (op.payload or {}).get("action") == "ci_transient_rerun_wait"
    ]

    assert len(rerun_operations) == 2
    assert len(wait_operations) == 2
    assert {op.idempotency_key for op in rerun_operations} != {None}
    assert len({op.idempotency_key for op in rerun_operations}) == 2
    assert len({op.idempotency_key for op in wait_operations}) == 2
    assert {
        tuple(failure_payload["name"] for failure_payload in (op.payload or {})["failures"])
        for op in rerun_operations
    } == {("python-full-coverage",), ("console-build",)}


@pytest.mark.unit
async def test_rerun_transient_ci_action_persists_attempt_before_github_rerun(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    failure = CheckFailure(
        name="python-full-coverage",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="25655330295",
    )
    status = _status(
        check_state=CheckState.FAILURE,
        ci_failures=(failure,),
        head_sha="abc1234567890def",
    )
    state_key = _ci_transient_rerun_state_key(status.head_sha, status.ci_failures)
    cmd = PersistCheckingCommandRunner(
        factory=factory,
        workspace_id=workspace_id,
        state_key=state_key,
        expected_value="1",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=RerunTransientCI(failures=(failure,)),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert cmd.calls[0].args == [
        "gh",
        "run",
        "rerun",
        "25655330295",
        "--repo",
        "dimileeh/aira-web",
        "--failed",
    ]


@pytest.mark.unit
async def test_rerun_transient_ci_action_records_failed_rerun_request(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="HTTP 403: workflow scope required")
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    failure = CheckFailure(
        name="python-full-coverage",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="25655330295",
    )
    status = _status(
        check_state=CheckState.FAILURE,
        ci_failures=(failure,),
        head_sha="abc1234567890def",
    )
    state_key = _ci_transient_rerun_state_key(status.head_sha, status.ci_failures)
    state = MonitorState()

    terminal = await runner._execute(
        action=RerunTransientCI(failures=(failure,)),
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
    assert adapter.calls == []
    assert state.iter_count == 1
    assert state.threads_addressed_ids[state_key] == "1"
    assert cmd.calls[0].args == [
        "gh",
        "run",
        "rerun",
        "25655330295",
        "--repo",
        "dimileeh/aira-web",
        "--failed",
    ]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.monitor_threads_addressed[state_key] == "1"
        events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.monitor_ci_transient_rerun_failed"
        ]
        assert len(events) == 1
        assert events[0].reason_code == "CI_TRANSIENT_RERUN_FAILED"
        assert events[0].payload is not None
        assert events[0].payload["run_ids"] == ["25655330295"]
        assert "workflow scope required" in events[0].payload["error"]
        operations = list((await session.execute(select(Operation))).scalars())
        rerun_operations = [
            op for op in operations if (op.payload or {}).get("action") == "ci_transient_rerun"
        ]
        assert len(rerun_operations) == 1
        assert rerun_operations[0].status == OperationStatus.failed.value
        assert rerun_operations[0].error_code == "CI_TRANSIENT_RERUN_FAILED"


@pytest.mark.unit
async def test_rerun_transient_ci_records_failed_request_on_bitbucket_not_rerunnable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A Bitbucket non-rerunnable pipeline logs a failed rerun, not a terminate.

    Regression for issue:4640573294: on Bitbucket ``rerun_failed_workflow_jobs``
    raises ``BitbucketClientError(BITBUCKET_PIPELINE_NOT_RERUNNABLE)`` when it
    cannot reconstruct a custom/manual pipeline target. The transient-rerun call
    site previously caught only ``GitHubClientError``, so the Bitbucket error
    escaped ``_execute`` and the runner's non-transient handler permanently
    terminated the workspace. It must instead be recorded as a failed transient
    rerun and the workspace must keep monitoring.
    """

    class NotRerunnableBitbucketGh(DefaultMergeMethodGitHubClient):
        async def rerun_failed_workflow_jobs(self, *, repo: RepoRef, run_id: str) -> None:
            del repo, run_id
            raise BitbucketClientError(
                operation="bitbucket rerun_failed_workflow_jobs",
                status=None,
                body="Bitbucket PR pipeline target could not be reconstructed.",
                reason_code=BITBUCKET_PIPELINE_NOT_RERUNNABLE,
            )

    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=NotRerunnableBitbucketGh(cmd),
    )
    failure = CheckFailure(
        name="pipeline",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="bb-pipeline-uuid",
    )
    status = _status(
        check_state=CheckState.FAILURE,
        ci_failures=(failure,),
        head_sha="abc1234567890def",
    )
    state_key = _ci_transient_rerun_state_key(status.head_sha, status.ci_failures)
    state = MonitorState()

    terminal = await runner._execute(
        action=RerunTransientCI(failures=(failure,)),
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

    # The non-rerunnable pipeline is recorded as a failed rerun and the monitor
    # keeps polling (terminal is False) rather than terminating the workspace.
    assert terminal is False
    assert adapter.calls == []
    assert state.iter_count == 1
    assert state.threads_addressed_ids[state_key] == "1"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        # The workspace must remain in monitoring_pr, not be terminated.
        assert workspace.status == WorkspaceStatus.monitoring_pr.value
        events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.monitor_ci_transient_rerun_failed"
        ]
        assert len(events) == 1
        assert events[0].reason_code == "CI_TRANSIENT_RERUN_FAILED"
        assert events[0].payload is not None
        assert events[0].payload["run_ids"] == ["bb-pipeline-uuid"]
        assert "could not be reconstructed" in events[0].payload["error"]
        operations = list((await session.execute(select(Operation))).scalars())
        rerun_operations = [
            op for op in operations if (op.payload or {}).get("action") == "ci_transient_rerun"
        ]
        assert len(rerun_operations) == 1
        assert rerun_operations[0].status == OperationStatus.failed.value
        assert rerun_operations[0].error_code == "CI_TRANSIENT_RERUN_FAILED"


@pytest.mark.unit
async def test_rerun_transient_ci_waits_after_partial_rerun_acceptance(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1, stderr="HTTP 502: try again")
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    first_failure = CheckFailure(
        name="python-full-coverage",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="25655330295",
    )
    second_failure = CheckFailure(
        name="console-build",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="25655330301",
    )
    status = _status(
        check_state=CheckState.FAILURE,
        ci_failures=(first_failure, second_failure),
        head_sha="abc1234567890def",
    )
    state_key = _ci_transient_rerun_state_key(status.head_sha, status.ci_failures)
    state = MonitorState()

    terminal = await runner._execute(
        action=RerunTransientCI(failures=(first_failure, second_failure)),
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
    assert adapter.calls == []
    assert state.iter_count == 1
    assert state.threads_addressed_ids[state_key] == "1"
    assert [call.args[3] for call in cmd.calls] == ["25655330295", "25655330301"]
    assert sleep_fn.calls == [60]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        partial_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.monitor_ci_transient_rerun_partially_requested"
        ]
        assert len(partial_events) == 1
        assert partial_events[0].reason_code == "CI_TRANSIENT_RERUN"
        assert partial_events[0].payload is not None
        assert partial_events[0].payload["run_ids"] == ["25655330295", "25655330301"]
        assert partial_events[0].payload["accepted_run_ids"] == ["25655330295"]
        assert partial_events[0].payload["failed_run_id"] == "25655330301"
        assert "try again" in partial_events[0].payload["error"]
        failed_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.monitor_ci_transient_rerun_failed"
        ]
        assert failed_events == []
        operations = list((await session.execute(select(Operation))).scalars())
        rerun_operations = [
            op for op in operations if (op.payload or {}).get("action") == "ci_transient_rerun"
        ]
        assert len(rerun_operations) == 1
        assert rerun_operations[0].status == OperationStatus.succeeded.value


@pytest.mark.unit
async def test_rerun_transient_ci_without_run_ids_dispatches_agent_repair(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    failure = CheckFailure(
        name="python-full-coverage",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        test_node_ids=("tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage",),
        suggested_repro_commands=(
            "uv run --python 3.12 --extra dev pytest "
            "tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage -q",
        ),
        error_summaries=("Missing reason catalog entries: ARTIFACT_BLOCKED",),
    )
    status = _status(
        check_state=CheckState.FAILURE,
        ci_failures=(failure,),
        head_sha="abc1234567890def",
    )
    state = MonitorState()
    state_key = _ci_transient_rerun_state_key(status.head_sha, status.ci_failures)
    ci_fix_calls: list[dict[str, object]] = []

    async def _record_ci_fix(**kwargs: object) -> _GitPushResult:
        ci_fix_calls.append(kwargs)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    mocker.patch.object(runner, "_run_ci_fix", _record_ci_fix)

    terminal = await runner._execute(
        action=RerunTransientCI(failures=(failure,)),
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
    assert cmd.calls == []
    assert ci_fix_calls[0]["failures"] == (failure,)
    assert state_key not in state.threads_addressed_ids
    async with factory() as session:
        operations = list((await session.execute(select(Operation))).scalars())
    assert [(op.payload or {}).get("action") for op in operations] == ["ci_repair"]
    assert operations[0].status == OperationStatus.succeeded.value
    failures_payload = (operations[0].payload or {})["failures"]
    assert failures_payload == [
        {
            "name": "python-full-coverage",
            "conclusion": "FAILURE",
            "run_id": None,
            "test_node_ids": [
                "tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage",
            ],
            "suggested_repro_commands": [
                "uv run --python 3.12 --extra dev pytest "
                "tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage -q",
            ],
            "failing_commands": [],
            "assertion_snippets": [],
            "error_summaries": ["Missing reason catalog entries: ARTIFACT_BLOCKED"],
            "evidence_warnings": [],
        }
    ]


@pytest.mark.unit
def test_ci_transient_rerun_attempt_treats_corrupt_count_as_zero() -> None:
    failure = CheckFailure(
        name="python-full-coverage",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="25655330295",
    )
    state = MonitorState()
    state.threads_addressed_ids[_ci_transient_rerun_state_key("head", (failure,))] = "corrupt"

    attempt = _ci_transient_rerun_attempt(
        state,
        head_sha="head",
        failures=(failure,),
    )

    assert attempt == 1
    assert state.threads_addressed_ids[_ci_transient_rerun_state_key("head", (failure,))] == "1"


@pytest.mark.unit
def test_ci_transient_rerun_attempt_carries_legacy_rollup_count_forward() -> None:
    failure = CheckFailure(
        name="python-full-coverage",
        conclusion="FAILURE",
        log_excerpt="HTTP status server error (502 Bad Gateway)",
        run_id="25655330295",
    )
    rollup_failure = CheckFailure(
        name="ci-required",
        conclusion="FAILURE",
        log_excerpt="A required CI job did not pass.",
        run_id="25655330295",
    )
    state = MonitorState()
    legacy_key = _ci_transient_rerun_state_key("head", (failure, rollup_failure))
    current_key = _ci_transient_rerun_state_key("head", (failure,))
    state.threads_addressed_ids[legacy_key] = "1"

    attempt = _ci_transient_rerun_attempt(
        state,
        head_sha="head",
        failures=(failure,),
        legacy_failures=(failure, rollup_failure),
    )

    assert attempt == 2
    assert state.threads_addressed_ids[current_key] == "2"
    assert legacy_key not in state.threads_addressed_ids


@pytest.mark.unit
async def test_advisory_plan_artifact_stale_reason_does_not_dispatch_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        assert candidate is not None
        await StaleReasonRepository(session).replace_active_findings(
            workspace_id=workspace_id,
            candidate_id=candidate.id,
            attempt_id=candidate.attempt_id,
            task_id=candidate.task_id,
            findings=[
                StaleReasonCreate(
                    reason_code=ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON,
                    trigger_type="path_overlap",
                    trigger_ref="docs/awf-plans/ws_other.md",
                    explanation=("Target branch changed another workspace's AWF plan artifact."),
                )
            ],
        )
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    gate = await runner._merge_gate_for_workspace(workspace_id)
    handled = await runner._handle_merge_gate_blocker(
        gate=gate,
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        assert candidate is not None
        stale_reasons = await StaleReasonRepository(session).list_active_for_candidate(candidate.id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert gate.stale_reason is None
    assert gate.req_action is None
    assert handled is None
    assert candidate.stale is False
    assert candidate.stale_reason is None
    assert [(r.reason_code, r.blocks_merge, r.severity) for r in stale_reasons] == [
        (ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON, False, "advisory")
    ]
    assert operations == []
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_gate_clears_stale_state_when_computed_reason_changes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        assert candidate is not None
        candidate.stale = False
        candidate.stale_reason = "legacy_blocking_stale_reason"
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    gate = await runner._merge_gate_for_workspace(workspace_id)

    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        assert candidate is not None

    assert gate.stale_reason is None
    assert gate.req_action is None
    assert candidate.stale is False
    assert candidate.stale_reason is None


@pytest.mark.unit
async def test_merge_gate_resyncs_stale_flag_when_reason_unchanged(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        assert candidate is not None
        candidate.stale = True
        candidate.stale_reason = None
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    gate = await runner._merge_gate_for_workspace(workspace_id)

    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        assert candidate is not None

    assert gate.stale_reason == "stale"
    assert gate.stale_reason == candidate.stale_reason
    assert candidate.stale is True
    assert candidate.stale_reason == "stale"


@pytest.mark.unit
async def test_auto_merge_waits_for_initial_review_grace_before_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_green_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert state.threads_addressed_ids[_initial_review_grace_started_key(42)]
    assert _gh_pr_merge_calls(cmd) == []
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert [(operation.type, operation.status) for operation in operations] == [
        ("monitor_state", OperationStatus.succeeded.value)
    ]
    assert operations[0].payload["action"] == "grace_wait"
    assert operations[0].payload["reason_code"] == "INITIAL_REVIEW_GRACE"
    assert operations[0].payload["wait_seconds"] == 60
    assert operations[0].result == {
        "status": "succeeded",
        "outcome": "wait_elapsed",
        "slept_seconds": 60,
    }


@pytest.mark.unit
async def test_wait_for_ci_records_check_wait_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    state = MonitorState()
    status = replace(_green_status(), check_state=CheckState.PENDING)

    terminal = await runner._execute(
        action=WaitForCI(reason="pending_checks"),
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

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert len(operations) == 1
    operation = operations[0]
    assert operation.type == "monitor_state"
    assert operation.status == OperationStatus.succeeded.value
    assert operation.payload["action"] == "check_wait"
    assert operation.payload["requested_action"] == "wait_for_ci"
    assert operation.payload["reason_code"] == "CHECK_WAIT"
    assert operation.payload["wait_seconds"] == 60
    assert operation.result == {
        "status": "succeeded",
        "outcome": "wait_elapsed",
        "slept_seconds": 60,
    }
