"""Tests for centralized per-cycle ``task_tag`` resolution (issue #537).

``_resolve_task_tag`` used to open a fresh DB session on *every* call: each repair
path resolved the tag once for its prompt and the shared
``_commit_dirty_worktree`` sink re-resolved it a second time. These tests pin the
new contract — each repair path resolves the tag exactly once per monitor cycle
and threads that value into the commit sink — while guarding against any change to
the tag value that reaches a commit/branch/PR.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import ci_ops, comments, operator_hints
from awf.runtime.pr_monitor_runner import remote_ops as pr_remote_ops
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.constants import _TASK_TAG_UNSET
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


_FAKE_REPO = SimpleNamespace(slug=lambda: "owner/repo")


def _commit_message(cmd: FakeCommandRunner) -> str:
    """Return the ``-m`` subject from the most recent ``git commit`` invocation."""
    commit_calls = [call for call in cmd.calls if "commit" in call.args and "-m" in call.args]
    assert commit_calls, "expected a git commit invocation"
    args = commit_calls[-1].args
    return str(args[args.index("-m") + 1])


def _spy_resolver(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy the module-level ``_resolve_task_tag`` sink helper; record workspace ids."""
    calls: list[str] = []
    original = pr_remote_repair._resolve_task_tag

    async def _spy(self: object, workspace_id: str) -> str | None:
        calls.append(workspace_id)
        return await original(self, workspace_id)

    monkeypatch.setattr(pr_remote_repair, "_resolve_task_tag", _spy)
    return calls


# --------------------------------------------------------------------------- #
# 1. The shared sink honors the threaded value without a DB round-trip.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_commit_dirty_worktree_threaded_tag_skips_resolver(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A threaded ``task_tag`` prefixes the subject and never re-queries the DB."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.task_tag = "ROW-1"  # would be used by a self-resolve; must be ignored
        await session.commit()

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M file.py\n")  # status --porcelain (dirty)
    cmd.queue_result(returncode=0)  # add -A
    cmd.queue_result(returncode=1)  # diff --cached --quiet (staged)
    cmd.queue_result(returncode=0)  # commit
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)
    resolver_calls = _spy_resolver(monkeypatch)

    committed = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: dirty",
        task_tag="PROJ-7",
    )

    assert committed is True
    assert _commit_message(cmd) == "PROJ-7 fix: dirty"
    assert resolver_calls == []


@pytest.mark.unit
async def test_commit_dirty_worktree_threaded_none_is_unprefixed_and_skips_resolver(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threading ``None`` (a real untagged workspace) leaves the subject unprefixed."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.task_tag = "ROW-1"  # a self-resolve would wrongly prefix; must not run
        await session.commit()

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M file.py\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)
    resolver_calls = _spy_resolver(monkeypatch)

    committed = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: dirty",
        task_tag=None,
    )

    assert committed is True
    assert _commit_message(cmd) == "fix: dirty"
    assert resolver_calls == []


@pytest.mark.unit
async def test_commit_dirty_worktree_omitted_tag_self_resolves_once(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no threaded tag the sink self-resolves exactly once (backward compat)."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.task_tag = "ROW-9"
        await session.commit()

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M file.py\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)
    resolver_calls = _spy_resolver(monkeypatch)

    committed = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: dirty",
    )

    assert committed is True
    assert _commit_message(cmd) == "ROW-9 fix: dirty"
    assert resolver_calls == [workspace_id]


@pytest.mark.unit
async def test_commit_dirty_worktree_threaded_tag_truncated_to_72(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The threaded-tag path keeps the ``[:72]`` truncation parity of every AWF commit."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M file.py\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

    long_subject = "fix: address PR review comment " + "x" * 80
    committed = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message=long_subject,
        task_tag="PROJ-123",
    )

    assert committed is True
    message = _commit_message(cmd)
    assert len(message) == 72
    assert message == ("PROJ-123 " + long_subject)[:72]


# --------------------------------------------------------------------------- #
# 2. ``_invoke_cli_for_verdict_result`` forwards the tag to the sink verbatim.
# --------------------------------------------------------------------------- #


def _verdict_runner(sink_task_tags: list[object]) -> SimpleNamespace:
    async def _suppress(workspace_id: str) -> bool:
        return False

    async def _adapter_run(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(returncode=0, stdout="AWF-VERDICT: FIXED: ok", stderr="")

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        sink_task_tags.append(kwargs.get("task_tag"))
        return True

    return SimpleNamespace(
        _provider_recovery_suppresses_cli=_suppress,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _deps=SimpleNamespace(adapter=SimpleNamespace(run=_adapter_run)),
    )


@pytest.mark.unit
async def test_invoke_cli_for_verdict_result_forwards_explicit_tag() -> None:
    sink_task_tags: list[object] = []
    runner = _verdict_runner(sink_task_tags)

    await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_1",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        task_tag="VERD-3",
    )

    assert sink_task_tags == ["VERD-3"]


@pytest.mark.unit
async def test_invoke_cli_for_verdict_result_default_forwards_sentinel() -> None:
    sink_task_tags: list[object] = []
    runner = _verdict_runner(sink_task_tags)

    await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_1",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
    )

    assert sink_task_tags == [_TASK_TAG_UNSET]


def _verdict_wrapper_runner(invoke_tags: list[object]) -> SimpleNamespace:
    async def _invoke(**kwargs: object) -> VerdictResult:
        invoke_tags.append(kwargs.get("task_tag"))
        return VerdictResult(verdict="fix_committed")

    return SimpleNamespace(_invoke_cli_for_verdict_result=_invoke)


@pytest.mark.unit
async def test_invoke_cli_for_verdict_wrapper_forwards_explicit_tag() -> None:
    """The verdict-only wrapper threads ``task_tag`` to the result helper."""
    invoke_tags: list[object] = []
    runner = _verdict_wrapper_runner(invoke_tags)

    verdict = await comments._invoke_cli_for_verdict(
        runner,
        workspace_id="ws_1",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        task_tag="WRAP-9",
    )

    assert verdict == "fix_committed"
    assert invoke_tags == ["WRAP-9"]


@pytest.mark.unit
async def test_invoke_cli_for_verdict_wrapper_default_forwards_sentinel() -> None:
    """Omitting ``task_tag`` forwards the sentinel so the sink self-resolves."""
    invoke_tags: list[object] = []
    runner = _verdict_wrapper_runner(invoke_tags)

    await comments._invoke_cli_for_verdict(
        runner,
        workspace_id="ws_1",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
    )

    assert invoke_tags == [_TASK_TAG_UNSET]


# --------------------------------------------------------------------------- #
# 3. Each repair path resolves the tag once and threads the same value through.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_run_ci_fix_resolves_once_and_threads_to_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls: list[str] = []
    prompt_tags: list[object] = []
    sink_tags: list[object] = []

    async def _resolve_task_tag(workspace_id: str) -> str | None:
        resolve_calls.append(workspace_id)
        return "CI-7"

    async def _pre_existing(**_kwargs: object) -> None:
        return None

    async def _suppress(workspace_id: str) -> bool:
        return False

    async def _head(**_kwargs: object) -> tuple[str, None]:
        return "headsha", None

    async def _adapter_run(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(returncode=0, stdout="", stderr="")

    async def _owned_paths(_self: object, _workspace_id: str) -> list[str]:
        return []

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        sink_tags.append(kwargs.get("task_tag"))
        return True

    async def _psb(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    def _fix_ci_prompt(**kwargs: object) -> str:
        prompt_tags.append(kwargs.get("task_tag"))
        return "PROMPT"

    monkeypatch.setattr(ci_ops, "_owned_paths_for_prompt", _owned_paths)
    monkeypatch.setattr(ci_ops, "fix_ci_prompt", _fix_ci_prompt)

    self = SimpleNamespace(
        _worktrees_root=tmp_path,
        _workspace_runtime_context=None,
        _resolve_task_tag=_resolve_task_tag,
        _pre_existing_dirty_repair_worktree_result=_pre_existing,
        _provider_recovery_suppresses_cli=_suppress,
        _repair_operation_start_head_result=_head,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _protected_scope_push_block=_psb,
        _validated_git_push_result=_validated,
        _deps=SimpleNamespace(adapter=SimpleNamespace(run=_adapter_run)),
    )

    await ci_ops._run_ci_fix(
        self,
        repo=_FAKE_REPO,
        pr_number=1,
        failures=(),
        compose_project="proj",
        compose_file=Path("compose.yml"),
        workspace_id="ws_ci",
        remote_branch="awf/ws_ci",
    )

    assert resolve_calls == ["ws_ci"]
    assert prompt_tags == ["CI-7"]
    assert sink_tags == ["CI-7"]


@pytest.mark.unit
async def test_run_operator_hint_cycle_resolves_once_and_threads_to_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls: list[str] = []
    prompt_tags: list[object] = []
    invoke_tags: list[object] = []

    async def _resolve_task_tag(workspace_id: str) -> str | None:
        resolve_calls.append(workspace_id)
        return "HINT-2"

    async def _pre_existing(**_kwargs: object) -> None:
        return None

    async def _head(**_kwargs: object) -> tuple[str, None]:
        return "headsha", None

    async def _invoke(**kwargs: object) -> VerdictResult:
        invoke_tags.append(kwargs.get("task_tag"))
        return VerdictResult(verdict="fix_committed")

    async def _psb(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "pushedsha"

    def _operator_hint_prompt(**kwargs: object) -> str:
        prompt_tags.append(kwargs.get("task_tag"))
        return "PROMPT"

    monkeypatch.setattr(operator_hints, "operator_hint_prompt", _operator_hint_prompt)
    monkeypatch.setattr(operator_hints, "mark_operator_hint_processed", lambda _state: None)

    self = SimpleNamespace(
        _worktrees_root=tmp_path,
        _workspace_runtime_context=None,
        _resolve_task_tag=_resolve_task_tag,
        _pre_existing_dirty_repair_worktree_result=_pre_existing,
        _repair_operation_start_head_result=_head,
        _invoke_cli_for_verdict_result=_invoke,
        _protected_scope_push_block=_psb,
        _validated_git_push_result=_validated,
        _rev_parse_head=_rev_parse_head,
    )

    state = SimpleNamespace(last_push_sha=None)
    await operator_hints._run_operator_hint_cycle(
        self,
        workspace_id="ws_hint",
        repo=_FAKE_REPO,
        pr_number=2,
        pr_head_sha="headsha",
        hint=OperatorHint(reason="do x", directive="please fix"),
        state=state,
        remote_branch="awf/ws_hint",
        compose_project="proj",
        compose_file=Path("compose.yml"),
    )

    assert resolve_calls == ["ws_hint"]
    assert prompt_tags == ["HINT-2"]
    assert invoke_tags == ["HINT-2"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry",
    ["_address_thread", "_address_review_comment_result"],
)
async def test_comment_paths_resolve_once_and_thread_to_invoke(
    entry: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls: list[str] = []
    prompt_tags: list[object] = []
    invoke_tags: list[object] = []

    async def _resolve_task_tag(workspace_id: str) -> str | None:
        resolve_calls.append(workspace_id)
        return "CMT-5"

    async def _invoke(**kwargs: object) -> VerdictResult:
        invoke_tags.append(kwargs.get("task_tag"))
        return VerdictResult(verdict="fix_committed")

    def _prompt(**kwargs: object) -> str:
        prompt_tags.append(kwargs.get("task_tag"))
        return "PROMPT"

    monkeypatch.setattr(comments, "address_thread_prompt", _prompt)
    monkeypatch.setattr(comments, "address_review_comment_prompt", _prompt)

    runner = SimpleNamespace(
        _workspace_runtime_context=None,
        _resolve_task_tag=_resolve_task_tag,
        _invoke_cli_for_verdict_result=_invoke,
    )

    common_kwargs: dict[str, object] = {
        "workspace_id": "ws_cmt",
        "repo": _FAKE_REPO,
        "pr_number": 3,
        "compose_project": "proj",
        "compose_file": Path("compose.yml"),
        "owned_paths": ["src/"],
    }
    if entry == "_address_thread":
        await comments._address_thread(
            runner,
            thread=SimpleNamespace(thread_id="t1"),
            **common_kwargs,
        )
    else:
        await comments._address_review_comment_result(
            runner,
            comment=SimpleNamespace(comment_id=11),
            **common_kwargs,
        )

    assert resolve_calls == ["ws_cmt"]
    assert prompt_tags == ["CMT-5"]
    assert invoke_tags == ["CMT-5"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry",
    ["_address_thread", "_address_review_comment_result"],
)
@pytest.mark.parametrize("threaded_tag", ["THREAD-9", None])
async def test_comment_paths_thread_supplied_tag_skips_resolver(
    entry: str,
    threaded_tag: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``task_tag`` threaded from the fix cycle is forwarded verbatim — including a
    real untagged ``None`` — to the prompt and commit sink without a per-item DB
    self-resolve (the #537 hot-path fix). A self-resolve here would return the wrong
    value, so the assertion both pins the no-DB contract and the forwarded value."""
    resolve_calls: list[str] = []
    prompt_tags: list[object] = []
    invoke_tags: list[object] = []

    async def _resolve_task_tag(workspace_id: str) -> str | None:
        resolve_calls.append(workspace_id)
        return "DB-NEVER-USED"

    async def _invoke(**kwargs: object) -> VerdictResult:
        invoke_tags.append(kwargs.get("task_tag"))
        return VerdictResult(verdict="fix_committed")

    def _prompt(**kwargs: object) -> str:
        prompt_tags.append(kwargs.get("task_tag"))
        return "PROMPT"

    monkeypatch.setattr(comments, "address_thread_prompt", _prompt)
    monkeypatch.setattr(comments, "address_review_comment_prompt", _prompt)

    runner = SimpleNamespace(
        _workspace_runtime_context=None,
        _resolve_task_tag=_resolve_task_tag,
        _invoke_cli_for_verdict_result=_invoke,
    )

    common_kwargs: dict[str, object] = {
        "workspace_id": "ws_cmt",
        "repo": _FAKE_REPO,
        "pr_number": 3,
        "compose_project": "proj",
        "compose_file": Path("compose.yml"),
        "owned_paths": ["src/"],
        "task_tag": threaded_tag,
    }
    if entry == "_address_thread":
        await comments._address_thread(
            runner,
            thread=SimpleNamespace(thread_id="t1"),
            **common_kwargs,
        )
    else:
        await comments._address_review_comment_result(
            runner,
            comment=SimpleNamespace(comment_id=11),
            **common_kwargs,
        )

    assert resolve_calls == []
    assert prompt_tags == [threaded_tag]
    assert invoke_tags == [threaded_tag]


class _EmptySettleResolveClient(DefaultMergeMethodGitHubClient):
    """gh double: an empty settle status (burst settled) plus a no-op resolve.

    Lets a multi-item ``_run_fix_cycle`` run end-to-end (address → settle → push →
    resolve) so the per-cycle ``_resolve_task_tag`` call count can be asserted.
    """

    def __init__(self, runner: FakeCommandRunner) -> None:
        super().__init__(runner)
        self.resolved: list[str] = []

    async def fetch_pr_status(
        self, *, repo: RepoRef, pr_number: int, base_behind_count: int
    ) -> PRStatus:
        del repo, pr_number, base_behind_count
        return PRStatus(
            number=42,
            head_sha="abc1234567890def",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        )

    async def resolve_thread(self, *, thread_id: str) -> None:
        self.resolved.append(thread_id)


@pytest.mark.unit
async def test_run_fix_cycle_resolves_task_tag_once_for_multiple_items(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The busiest repair path resolves ``task_tag`` exactly once per cycle (#537).

    With two inline threads and two review comments in a single fix cycle, the tag
    is hoisted alongside ``owned_paths`` and threaded into each per-item helper, so
    the workspace is looked up once for the whole cycle — not once per item. The
    two resolved threads confirm both item loops actually ran (so the single
    resolve is not a trivial early-exit)."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD
    adapter = FakeAdapter()
    for _ in range(4):  # 2 threads + 2 review comments
        adapter.queue(stdout="Committed fix locally.")
    threads = tuple(
        ReviewThread(
            thread_id=f"PRRT_{n}",
            path="src/foo.py",
            line=n,
            body_excerpt="please adjust",
            author="review-bot",
        )
        for n in (1, 2)
    )
    reviews = tuple(
        ReviewComment(comment_id=cid, body_excerpt="tweak this", author="review-bot")
        for cid in ("c11", "c12")
    )
    gh = _EmptySettleResolveClient(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    # Spy the instance-bound resolver so every cycle-level + per-item lookup counts.
    original_resolve = runner._resolve_task_tag
    resolve_calls: list[str] = []

    async def _counting_resolve(ws_id: str) -> str | None:
        resolve_calls.append(ws_id)
        return await original_resolve(ws_id)

    runner._resolve_task_tag = _counting_resolve  # type: ignore[method-assign]
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=threads,
        initial_reviews=reviews,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert resolve_calls == [workspace_id]
    assert gh.resolved == ["PRRT_1", "PRRT_2"]


@pytest.mark.unit
async def test_run_sync_base_resolves_once_and_threads_to_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls: list[str] = []
    sink_tags: list[object] = []

    class _FakeRunner:
        def __init__(self, results: list[CommandResult]) -> None:
            self._results = results
            self.calls: list[list[str]] = []

        async def run(self, args: list[str]) -> CommandResult:
            self.calls.append(args)
            return self._results.pop(0)

    fake_runner = _FakeRunner(
        [
            CommandResult(returncode=0, stdout="", stderr=""),  # merge --abort
            CommandResult(returncode=1, stdout="", stderr="conflict"),  # merge origin/base
            CommandResult(returncode=0, stdout="UU src/foo.py\n", stderr=""),  # status
        ]
    )

    async def _resolve_task_tag(workspace_id: str) -> str | None:
        resolve_calls.append(workspace_id)
        return "SYNC-4"

    async def _fetch_base(**_kwargs: object) -> None:
        return None

    async def _suppress(workspace_id: str) -> bool:
        return False

    async def _adapter_run(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(returncode=0, stdout="", stderr="")

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        sink_tags.append(kwargs.get("task_tag"))
        return True

    async def _psb(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _workspace_runtime_context="",
        _resolve_task_tag=_resolve_task_tag,
        _fetch_base=_fetch_base,
        _provider_recovery_suppresses_cli=_suppress,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _protected_scope_push_block=_psb,
        _validated_git_push_result=_validated,
        _deps=SimpleNamespace(runner=fake_runner, adapter=SimpleNamespace(run=_adapter_run)),
    )

    await pr_remote_ops._run_sync_base(
        runner,
        workspace_id="ws_sync",
        repo=_FAKE_REPO,
        pr_number=4,
        base_branch="development",
        remote_branch="awf/ws_sync",
        compose_project="proj",
        compose_file=Path("compose.yml"),
    )

    assert resolve_calls == ["ws_sync"]
    assert sink_tags == ["SYNC-4"]
