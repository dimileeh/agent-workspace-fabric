"""Protected-scope directive resumes evaluate real ancestry (#1200).

PR #1204: the monitor's agent committed ``X`` touching ``.github/workflows/ci.yml``
and the protected-scope gate preserved it. On the operator's guide the agent
rewrote its unpushed history so ``X`` is no longer an ancestor of HEAD and the
clean head was published. The comment-repair item records still cited ``X``.

These tests run the operator-hint cycle over a real Git worktree and a bare
"remote", so ancestry is Git's answer, not a monkeypatched one:

* (a) ``X`` orphaned and the clean head published (or about to be): no re-block,
  and the stale item record is retired with a reason-coded audit event;
* (b) revert-on-top keeps ``X`` in the push range: it must still re-block, and the
  record (still an ancestor of HEAD) is kept.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import AsyncioSubprocessRunner
from awf.common.github_client import RepoRef
from awf.control.quality_gates import GrantSpec
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner.comment_repair_provenance import (
    COMMENT_REPAIR_ITEM_COMMIT_ORPHANED,
    ITEM_COMMIT_RETIRED_EVENT,
    ItemCommitProvenance,
    chain_from_state,
    encode_item_commit_provenance_chain,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

pytestmark = pytest.mark.unit

_ITEM_ID = "PRRT_kwDOexample0001"
_CI_YML = ".github/workflows/ci.yml"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class _Repo:
    """A worktree at ``<tmp>/<workspace_id>`` with a bare remote PR branch."""

    def __init__(self, tmp_path: Path, workspace_id: str) -> None:
        self.remote = tmp_path / "remote.git"
        self.worktree = tmp_path / workspace_id
        self.branch = f"awf/{workspace_id}"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True
        )
        self.worktree.mkdir()
        _git(self.worktree, "init", "-q")
        _git(self.worktree, "config", "user.email", "awf@example.com")
        _git(self.worktree, "config", "user.name", "AWF Test")
        (self.worktree / ".github" / "workflows").mkdir(parents=True)
        (self.worktree / _CI_YML).write_text(
            "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: pytest -q\n"
        )
        (self.worktree / "app.py").write_text("VALUE = 1\n")
        _git(self.worktree, "add", "-A")
        _git(self.worktree, "commit", "-qm", "feat: initial")
        self.published = self.head()
        self.push()

    def head(self) -> str:
        return _git(self.worktree, "rev-parse", "HEAD")

    def push(self) -> None:
        _git(self.worktree, "push", "-q", str(self.remote), f"HEAD:refs/heads/{self.branch}")

    def remote_head(self) -> str:
        return _git(self.remote, "rev-parse", f"refs/heads/{self.branch}")

    def commit_protected_change(self) -> str:
        with (self.worktree / _CI_YML).open("a") as handle:
            handle.write(
                "  contract:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make contract\n"
            )
        _git(self.worktree, "commit", "-qam", f"fix: address {_ITEM_ID} — add contract job")
        return self.head()

    def commit_clean_change(self) -> str:
        (self.worktree / "app.py").write_text("VALUE = 2\n")
        _git(self.worktree, "commit", "-qam", f"fix: address {_ITEM_ID} — scoped fix")
        return self.head()


async def _seed_protected_block(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    extra_violation_paths: tuple[str, ...] = (),
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.block_violations = [
            {
                "path": _CI_YML,
                "protected_pattern": ".github/workflows/",
                "section": "jobs.contract",
                "reason": "QUALITY_GATE_POLICY_CHANGED",
            },
            *(
                {"path": path, "reason": "QUALITY_GATE_POLICY_CHANGED"}
                for path in extra_violation_paths
            ),
        ]
        await session.commit()


def _hint() -> OperatorHint:
    return OperatorHint(
        reason="discard the workflow change and resume monitoring",
        directive="discard the protected ci.yml commit, keep the scoped fix, resume",
        operation_id="op_discard_protected",
        requested_at="2026-11-10T08:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )


def _state(*, published: str, protected: str) -> MonitorState:
    state = MonitorState(pending_operator_hint=_hint())
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, protected)
    state.mark_addressed(
        _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY,
        encode_item_commit_provenance_chain(
            (
                ItemCommitProvenance(
                    item_id=_ITEM_ID,
                    item_start_head=published,
                    head_sha=protected,
                    operation_id="op_comment_repair_1",
                ),
            )
        ),
    )
    return state


def _runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo: _Repo,
    pushes: list[str],
    prompts: list[str],
) -> object:
    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    start_head = repo.head()

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (start_head, None)

    async def _fixed_without_touching_git(**kwargs: object) -> VerdictResult:
        prompts.append(str(kwargs["prompt"]))
        return VerdictResult(verdict="fixed")

    async def _not_moot(**_kwargs: object) -> None:
        return None

    async def _push(**_kwargs: object) -> _GitPushResult:
        pushes.append(repo.head())
        repo.push()
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_without_touching_git)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _not_moot)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push)
    return runner


async def _run_cycle(
    runner: object, *, workspace_id: str, repo: _Repo, state: MonitorState, tmp_path: Path
) -> _GitPushResult:
    assert state.pending_operator_hint is not None
    return await runner._run_operator_hint_cycle(  # type: ignore[attr-defined, no-any-return]
        workspace_id=workspace_id,
        repo=RepoRef(owner="example", name="service"),
        pr_number=42,
        pr_head_sha=repo.remote_head(),
        hint=state.pending_operator_hint,
        state=state,
        remote_branch=repo.branch,
        remote_push_url=str(repo.remote),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )


async def _events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, event_type: str
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == event_type,
            )
            .order_by(WorkspaceEvent.event_order)
        )
        return list(rows.scalars())


async def _assert_not_reblocked(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.block_epoch == 0
    assert await _events(factory, workspace_id, "workspace.monitor_protected_scope_paused") == []


async def _assert_orphaned_record_retired(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    state: MonitorState,
    protected: str,
    head: str,
) -> None:
    assert all(record.head_sha != protected for record in chain_from_state(state))
    retired = await _events(factory, workspace_id, ITEM_COMMIT_RETIRED_EVENT)
    assert len(retired) == 1
    assert retired[0].reason_code == COMMENT_REPAIR_ITEM_COMMIT_ORPHANED
    payload = retired[0].payload
    assert payload["head_sha"] == head
    assert payload["reason"] == "not_ancestor_of_head"
    assert [record["head_sha"] for record in payload["retired"]] == [protected]
    assert payload["retired"][0]["item_id"] == _ITEM_ID
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    durable_chain = (workspace.monitor_threads_addressed or {}).get(
        _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
    )
    assert durable_chain is None or protected not in durable_chain


async def test_orphaned_protected_commit_with_published_clean_head_does_not_reblock(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) The #1204 sequence: ``X`` orphaned, clean head already published."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_protected_block(factory, workspace_id)
    repo = _Repo(tmp_path, workspace_id)
    protected = repo.commit_protected_change()
    state = _state(published=repo.published, protected=protected)
    # The agent rewrote its unpushed history: X is no longer an ancestor of HEAD.
    _git(repo.worktree, "reset", "-q", "--hard", repo.published)
    clean = repo.commit_clean_change()
    repo.push()
    runner = _runner(factory, tmp_path, monkeypatch, repo=repo, pushes=[], prompts=[])

    result = await _run_cycle(
        runner, workspace_id=workspace_id, repo=repo, state=state, tmp_path=tmp_path
    )

    assert result.paused_into_blocked is False
    assert result.failed is False
    assert repo.head() == clean
    assert repo.remote_head() == clean
    assert state.pending_operator_hint is None
    await _assert_not_reblocked(factory, workspace_id)
    await _assert_orphaned_record_retired(
        factory, workspace_id, state=state, protected=protected, head=clean
    )


async def test_orphaned_protected_commit_with_unpushed_clean_head_pushes_without_reblock(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) variant: the clean rewrite is still local, so AWF pushes it."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_protected_block(factory, workspace_id)
    repo = _Repo(tmp_path, workspace_id)
    protected = repo.commit_protected_change()
    state = _state(published=repo.published, protected=protected)
    _git(repo.worktree, "reset", "-q", "--hard", repo.published)
    clean = repo.commit_clean_change()
    pushes: list[str] = []
    prompts: list[str] = []
    runner = _runner(factory, tmp_path, monkeypatch, repo=repo, pushes=pushes, prompts=prompts)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, repo=repo, state=state, tmp_path=tmp_path
    )

    assert result.pushed is True
    assert result.paused_into_blocked is False
    assert pushes == [clean]
    assert repo.remote_head() == clean
    assert state.pending_operator_hint is None
    # The directive agent was told to reset to the published head, not revert.
    assert len(prompts) == 1
    assert protected in prompts[0]
    assert repo.published in prompts[0]
    assert "Do not add a revert commit on top" in prompts[0]
    await _assert_not_reblocked(factory, workspace_id)
    await _assert_orphaned_record_retired(
        factory, workspace_id, state=state, protected=protected, head=clean
    )


async def test_revert_on_top_keeps_protected_commit_in_push_range_and_reblocks(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) A revert on top leaves ``X`` in the push range: the refusal stays."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_protected_block(factory, workspace_id)
    repo = _Repo(tmp_path, workspace_id)
    protected = repo.commit_protected_change()
    state = _state(published=repo.published, protected=protected)
    _git(repo.worktree, "revert", "--no-edit", protected)
    revert_head = repo.head()
    # The net diff against the published head is clean; only history leaks X.
    assert _git(repo.worktree, "diff", "--name-only", repo.published, "HEAD") == ""
    pushes: list[str] = []
    runner = _runner(factory, tmp_path, monkeypatch, repo=repo, pushes=pushes, prompts=[])

    result = await _run_cycle(
        runner, workspace_id=workspace_id, repo=repo, state=state, tmp_path=tmp_path
    )

    assert result.paused_into_blocked is True
    assert protected in (result.stderr or "")
    assert pushes == []
    assert repo.remote_head() == repo.published
    assert repo.head() == revert_head
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.blocked.value
    # X is still an ancestor of HEAD, so its item record is not retired.
    assert [record.head_sha for record in chain_from_state(state)] == [protected]
    assert await _events(factory, workspace_id, ITEM_COMMIT_RETIRED_EVENT) == []


async def test_reset_inside_directive_agent_retires_orphaned_record_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) variant: the directive agent itself resets ``X`` away mid-cycle.

    The pre-agent retirement still sees ``X`` as an ancestor of HEAD, so the
    record must be rechecked after the agent rewrites history (PRRT_kwDOSJAM6s61n6zY).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_protected_block(factory, workspace_id)
    repo = _Repo(tmp_path, workspace_id)
    protected = repo.commit_protected_change()
    state = _state(published=repo.published, protected=protected)
    pushes: list[str] = []
    runner = _runner(factory, tmp_path, monkeypatch, repo=repo, pushes=pushes, prompts=[])
    clean_heads: list[str] = []

    async def _agent_resets_protected_commit(**_kwargs: object) -> VerdictResult:
        # The pre-agent retirement already ran and kept the record.
        assert [record.head_sha for record in chain_from_state(state)] == [protected]
        _git(repo.worktree, "reset", "-q", "--hard", repo.published)
        clean_heads.append(repo.commit_clean_change())
        return VerdictResult(verdict="fixed")

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _agent_resets_protected_commit)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, repo=repo, state=state, tmp_path=tmp_path
    )

    assert result.pushed is True
    assert result.paused_into_blocked is False
    assert pushes == clean_heads
    assert state.pending_operator_hint is None
    await _assert_not_reblocked(factory, workspace_id)
    await _assert_orphaned_record_retired(
        factory, workspace_id, state=state, protected=protected, head=clean_heads[0]
    )


async def test_directive_with_approve_and_keep_grant_omits_reset_guidance(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``--directive`` + ``--grant`` resume keeps the preserved commit.

    The operator approved the protected change, so the directive prompt must not
    tell the agent to reset it away before the grant-aware push
    (PRRT_kwDOSJAM6s61n9xW).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_protected_block(factory, workspace_id)
    repo = _Repo(tmp_path, workspace_id)
    protected = repo.commit_protected_change()
    state = _state(published=repo.published, protected=protected)
    prompts: list[str] = []
    runner = _runner(factory, tmp_path, monkeypatch, repo=repo, pushes=[], prompts=prompts)

    async def _active_grant(_workspace_id: str) -> list[GrantSpec]:
        return [GrantSpec(path=_CI_YML)]

    monkeypatch.setattr(runner, "_active_operator_grant_specs", _active_grant)

    await _run_cycle(runner, workspace_id=workspace_id, repo=repo, state=state, tmp_path=tmp_path)

    assert len(prompts) == 1
    assert "Protected-scope block" not in prompts[0]
    assert "Do not add a revert commit on top" not in prompts[0]
    assert repo.head() == protected


async def test_directive_with_partial_grant_keeps_reset_guidance(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant covering only some blocked paths does not approve the preserved commit.

    The push leak guard still re-blocks a revert-on-top while any blocked path is
    ungranted, so the directive prompt must keep the reset guidance
    (PRRT_kwDOSJAM6s61o81S).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_protected_block(factory, workspace_id, extra_violation_paths=("pyproject.toml",))
    repo = _Repo(tmp_path, workspace_id)
    protected = repo.commit_protected_change()
    state = _state(published=repo.published, protected=protected)
    prompts: list[str] = []
    runner = _runner(factory, tmp_path, monkeypatch, repo=repo, pushes=[], prompts=prompts)

    async def _partial_grant(_workspace_id: str) -> list[GrantSpec]:
        return [GrantSpec(path="pyproject.toml")]

    monkeypatch.setattr(runner, "_active_operator_grant_specs", _partial_grant)

    await _run_cycle(runner, workspace_id=workspace_id, repo=repo, state=state, tmp_path=tmp_path)

    assert len(prompts) == 1
    assert "Protected-scope block" in prompts[0]
    assert f"`{protected}`" in prompts[0]
    assert "Do not add a revert commit on top" in prompts[0]
