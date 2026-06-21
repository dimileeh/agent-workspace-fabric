"""Executor blocked-resume grant-consumption tests (FakeCommandRunner + PostgreSQL).

Split out of ``test_executor_part_002`` to keep first-party files under the
maintainability line limit. These exercise the single-use operator-grant
lifecycle on the ``resume_blocked_execution`` path: a grant is consumed only
after the validating→pushing CAS commits the validated change to push.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor.constants import (
    POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
)
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_post_agent_commit_parts.test_executor_post_agent_commit_part_001 import (
    _failed_state_event,
    _precommit_mypy_output,
)

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
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
    )


def _queue_pre_push_diagnostics(fake: FakeCommandRunner, *, head: str = "deadbeef01") -> None:
    """Queue executor's committed-diff policy check plus the three canned
    git results ``PullRequestCreator`` reads for its pre-push diagnostic log line
    (``rev-parse HEAD``, ``rev-parse --abbrev-ref HEAD``, ``git log
    origin/<base>..HEAD``)."""
    fake.queue_result(
        returncode=0, stdout="src/fix.py\n"
    )  # final plan-only gate: committed base..HEAD --name-only
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref
    fake.queue_result(returncode=0, stdout="abc1234 commit\n")  # log ahead-of-base


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


async def _seed_resumable_blocked_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    block_epoch: int = 1,
    grant_path: str | None = "pyproject.toml",
    directive: str | None = None,
    reclaim_to_running: bool = True,
    execution_claimed_by: str | None = None,
    block_baseline_coverage: dict[str, Any] | None = None,
    block_planning_conformance_handoff: dict[str, Any] | None = None,
) -> str:
    """Insert a workspace the worker has already re-claimed ``blocked → running``
    after an operator resolved the pause, ready for ``resume_blocked_execution``
    to drive to ``pushing``.

    An approve-and-keep grant is seeded when ``grant_path`` is set; a revert/redo
    ``directive`` is armed in ``pending_operator_hint`` when provided. Set
    ``reclaim_to_running=False`` to leave the row ``blocked`` (the worker's resume
    CAS never landed), so ``resume_blocked_execution`` short-circuits on its
    ``running`` recheck. ``execution_claimed_by`` stamps the re-acquired execution
    claim so resume entry can be gated on claim ownership."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="trivial",
            task_prompt="Add a docstring.",
            agent="codex",
            test_commands=["pytest -q"],
            task_policy={},
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.blocked, reason_code="X")
        ws.block_epoch = block_epoch
        # The worker re-claims the operator-cleared workspace back to ``running``
        # before handing it to ``resume_blocked_execution``.
        if reclaim_to_running:
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        if execution_claimed_by is not None:
            ws.execution_claimed_by = execution_claimed_by
        if block_baseline_coverage is not None:
            ws.block_baseline_coverage = block_baseline_coverage
        if block_planning_conformance_handoff is not None:
            ws.block_planning_conformance_handoff = block_planning_conformance_handoff
        if directive is not None:
            ws.pending_operator_hint = {
                "status": "pending",
                "directive": directive,
                "reason": "operator asked for a redo",
            }
        if grant_path is not None:
            s.add(
                OperatorGrantAuditRecord(
                    id=f"grant_{ws.id}",
                    workspace_id=ws.id,
                    operator="op@example.com",
                    reason="approved benign config split",
                    normalized_path=grant_path,
                    block_epoch=block_epoch,
                )
            )
        await s.commit()
        (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _active_grant(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> OperatorGrantAuditRecord:
    async with factory() as s:
        row = await s.execute(
            select(OperatorGrantAuditRecord).where(
                OperatorGrantAuditRecord.workspace_id == workspace_id
            )
        )
        return row.scalars().one()


def _queue_blocked_resume_to_push(fake: FakeCommandRunner, *, ws_id: str) -> None:
    # Approve-and-keep resume skips the agent; the post-agent commit, the
    # validation, the pre-push policy gates, and the push all still run, so
    # the queue mirrors the happy path MINUS the leading ``adapter.run``.
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
    fake.queue_result(returncode=0)  # git add
    fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff (non-empty)
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
    _queue_pre_push_diagnostics(fake)
    fake.queue_result(returncode=0)  # git push
    fake.queue_result(
        returncode=0,
        stdout="https://github.com/dimileeh/aira-agent/pull/777\n",
    )  # gh pr create


@pytest.mark.unit
async def test_blocked_resume_consumes_grants_after_push_transition(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # On a successful resume the validating→pushing CAS commits the validated
    # change to push, so the single-use operator grant is consumed.
    ws_id = await _seed_resumable_blocked_workspace(factory)
    _queue_blocked_resume_to_push(fake, ws_id=ws_id)

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
    grant = await _active_grant(factory, ws_id)
    assert grant.consumed_at is not None


@pytest.mark.unit
async def test_blocked_resume_keeps_grants_when_push_transition_loses(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for PRRT_kwDOSJAM6s6J2gLd: if the validating→pushing CAS
    # loses (concurrent cancel, version race, stale status) the workspace
    # never enters ``pushing``, so the grant MUST stay active — consuming it
    # before the transition would strand a later protected check on the same
    # resume with no usable grant.
    ws_id = await _seed_resumable_blocked_workspace(factory)
    _queue_blocked_resume_to_push(fake, ws_id=ws_id)

    real_transition = executor._transition_if_current

    async def _transition_or_lose_pushing(
        workspace_id: str, *, from_status: Any, to: Any, reason: str, action: str
    ) -> bool:
        if to is WorkspaceStatus.pushing:
            return False  # simulate a lost CAS at the push transition
        return await real_transition(
            workspace_id,
            from_status=from_status,
            to=to,
            reason=reason,
            action=action,
        )

    monkeypatch.setattr(executor, "_transition_if_current", _transition_or_lose_pushing)

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status != WorkspaceStatus.completed.value
        assert ws.status != WorkspaceStatus.pushing.value
    grant = await _active_grant(factory, ws_id)
    assert grant.consumed_at is None


@pytest.mark.unit
async def test_blocked_resume_with_directive_reinvokes_agent_with_directive(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Revert/redo resume: the operator armed a directive (no approve-and-keep
    # grant), so the agent is re-invoked with the directive appended to the
    # task prompt (NOT skipped as on the grant path) and the run proceeds.
    ws_id = await _seed_resumable_blocked_workspace(
        factory, grant_path=None, directive="revert the protected change"
    )
    fake.queue_result(returncode=0, stdout="codex finished")  # adapter re-invoked
    _queue_blocked_resume_to_push(fake, ws_id=ws_id)

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
    # The agent received the operator directive appended to its prompt.
    agent_calls = [
        call
        for call in fake.calls
        if call.input_bytes is not None and b"Operator directive" in call.input_bytes
    ]
    assert agent_calls, "expected the agent to be re-invoked with the directive"
    assert b"revert the protected change" in agent_calls[0].input_bytes


@pytest.mark.unit
async def test_blocked_resume_grant_does_not_invoke_agent_on_precommit_repair(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Regression for PRRT_kwDOSJAM6s6J5SDf: an approve-and-keep grant resume
    # skips the agent task ("no tokens") and keeps the approved protected change
    # verbatim. If the post-agent commit then trips a semantic pre-commit hook
    # (here: mypy), the repair gate MUST stay closed — re-invoking the agent
    # would spend tokens and could rewrite the approved change. The commit step
    # records ``agent_skipped`` and fails instead of running semantic repair.
    ws_id = await _seed_resumable_blocked_workspace(factory)
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff (non-empty)
    fake.queue_result(returncode=1, stdout=_precommit_mypy_output())  # git commit fails

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert event.payload is not None
    details = event.payload["details"]
    assert isinstance(details, dict)
    commit_details = details["post_agent_commit"]
    # The agent semantic repair was gated off, not run.
    assert commit_details["repair_strategy"] == "agent_skipped"
    assert commit_details["precommit_repair_attempted"] is False
    # The agent CLI was never invoked: only the single failed commit ran, with
    # no targeted-repair agent run and no retry commit.
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1
    agent_calls = [call for call in fake.calls if call.input_bytes is not None]
    assert agent_calls == []


@pytest.mark.unit
async def test_blocked_resume_grant_does_not_invoke_fix_agent_on_validation_failure(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for PRRT_kwDOSJAM6s6J7EUX: an approve-and-keep grant resume
    # skips the agent ("no tokens") and keeps the approved protected change
    # verbatim, then re-runs setup + validation. If that validation FAILS, the
    # fix cycle MUST NOT fire a fix pass — re-invoking the coding adapter would
    # spend tokens and could rewrite the approved protected change. A grant
    # resume runs validation with zero fix passes, so a failure marks the
    # workspace FAILED for operator triage instead of calling the agent.
    ws_id = await _seed_resumable_blocked_workspace(factory)

    # The post-agent commit captures the approved change verbatim; queue it
    # through to a real commit so execution reaches the validation step.
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff (non-empty)
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok

    stdout_path = tmp_path / "validate.stdout"
    stderr_path = tmp_path / "validate.stderr"
    stdout_path.write_text("FAILED tests/test_app.py::test_flow\n", encoding="utf-8")
    stderr_path.write_text("AssertionError: expected ok\n", encoding="utf-8")

    failing_validation = ValidationResult(
        commands=[
            ValidationCommandResult(
                command="pytest -q",
                returncode=1,
                duration_seconds=0.1,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                reason_code="COMMAND_FAILED",
            )
        ]
    )

    validate_phase_calls = 0
    real_run_profile_phases = executor._validation.run_profile_phases

    async def _fail_validate_phase(
        *, phase_names: tuple[str, ...] | list[str], **kwargs: Any
    ) -> ValidationResult:
        nonlocal validate_phase_calls
        if tuple(phase_names) == ("post_agent", "validate"):
            validate_phase_calls += 1
            return failing_validation
        return await real_run_profile_phases(phase_names=phase_names, **kwargs)

    monkeypatch.setattr(executor._validation, "run_profile_phases", _fail_validate_phase)

    async def _head_sha(*, workspace_id: str, worktree_path: Path) -> str:
        return "deadbeef01"

    monkeypatch.setattr(executor, "_capture_workspace_head_sha", _head_sha)

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "validation_failure"

    # Validation ran exactly once: zero fix passes means no fix-pass re-validate.
    assert validate_phase_calls == 1
    # The coding adapter was never invoked — no fix-pass agent run (which would
    # carry a fix prompt as ``input_bytes``) spent tokens or touched the change.
    agent_calls = [call for call in fake.calls if call.input_bytes is not None]
    assert agent_calls == []


_PENDING_CONFORMANCE_HANDOFF = {
    "plan_path": "docs/awf-plans/ws.md",
    "report_path": "docs/awf-plans/ws.conformance.json",
    "iteration": 1,
    "max_iterations": 3,
    "report": {
        "status": "needs_iteration",
        "summary": "AWF validation evidence is required.",
        "gaps": ["rerun pytest under AWF"],
        "reason_code": "CONFORMANCE_REQUIRES_AWF_VALIDATION",
    },
}


@pytest.mark.unit
async def test_blocked_resume_grant_runs_conformance_check_from_persisted_handoff(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for PRRT_kwDOSJAM6s6KAbBL: an approve-and-keep grant resume skips
    # the agent/planning block — the ONLY place the in-memory planning handoff is
    # produced — so without the block-time persistence the post-validation plan
    # conformance check (gated on a non-None handoff) would be skipped entirely,
    # letting a planning-required workspace whose conformance was still pending
    # AWF-validation evidence be revalidated and pushed without the required gate.
    # The handoff persisted at block time is reconstructed on resume so the
    # conformance check still runs.
    ws_id = await _seed_resumable_blocked_workspace(
        factory,
        block_planning_conformance_handoff=_PENDING_CONFORMANCE_HANDOFF,
    )
    _queue_blocked_resume_to_push(fake, ws_id=ws_id)

    seen_handoffs: list[Any] = []

    async def _record_conformance(*, handoff: Any, **_kwargs: Any) -> None:
        # Returning None (implicitly) reports conformance satisfied, so the
        # success path proceeds to push.
        seen_handoffs.append(handoff)

    monkeypatch.setattr(executor, "_run_post_validation_conformance_check", _record_conformance)

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value

    assert len(seen_handoffs) == 1, "conformance check must run on the grant resume"
    handoff = seen_handoffs[0]
    assert handoff is not None
    assert handoff.report_path == Path("docs/awf-plans/ws.conformance.json")
    assert handoff.plan_path == Path("docs/awf-plans/ws.md")


@pytest.mark.unit
async def test_blocked_resume_grant_skips_conformance_check_without_persisted_handoff(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The complement of the regression above: when the original run satisfied
    # conformance inline (no handoff persisted), an approve-and-keep resume must
    # NOT invent a pending conformance requirement — the check stays skipped, just
    # as it was on the run that blocked.
    ws_id = await _seed_resumable_blocked_workspace(factory)
    _queue_blocked_resume_to_push(fake, ws_id=ws_id)

    conformance_calls = 0

    async def _record_conformance(**_kwargs: Any) -> None:
        nonlocal conformance_calls
        conformance_calls += 1

    monkeypatch.setattr(executor, "_run_post_validation_conformance_check", _record_conformance)

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value

    assert conformance_calls == 0


@pytest.mark.unit
async def test_blocked_resume_missing_workspace_is_a_noop(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
) -> None:
    # The resume target vanished (e.g. concurrent destroy) before dispatch:
    # ``_begin_execution`` loads nothing and returns without touching the agent.
    await executor.resume_blocked_execution("ws_does_not_exist")

    assert fake.calls == []


@pytest.mark.unit
async def test_blocked_resume_short_circuits_when_not_running(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The worker's blocked→running resume CAS never landed (lost race), so the
    # row is still ``blocked``. ``_begin_execution`` fails its ``running``
    # recheck and returns early: no agent run, status unchanged.
    ws_id = await _seed_resumable_blocked_workspace(factory, reclaim_to_running=False)

    await executor.resume_blocked_execution(ws_id)

    assert fake.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value


@pytest.mark.unit
async def test_blocked_resume_skips_when_claim_owned_by_other_worker(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Regression for PRRT_kwDOSJAM6s6J4E8B: a stale worker whose execution claim
    # was transferred to a newer claimant (e.g. worker-restart recovery after a
    # lease lapse) must NOT resume — resume entry is gated on claim ownership,
    # mirroring the CAS-guarded paths. Otherwise two workers could drive the same
    # warm stack (duplicate resume execution). The row is left ``running`` with
    # the rightful claimant's ownership intact.
    ws_id = await _seed_resumable_blocked_workspace(factory, execution_claimed_by="worker-new")

    await executor.resume_blocked_execution(ws_id, execution_owner_id="worker-stale")

    assert fake.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.running.value
        assert ws.execution_claimed_by == "worker-new"
    grant = await _active_grant(factory, ws_id)
    assert grant.consumed_at is None


@pytest.mark.unit
async def test_blocked_resume_proceeds_when_claim_owner_matches(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The rightful claimant (execution_owner_id == execution_claimed_by) resumes
    # normally: the ownership gate passes and the run drives to completion.
    ws_id = await _seed_resumable_blocked_workspace(factory, execution_claimed_by="worker-A")
    _queue_blocked_resume_to_push(fake, ws_id=ws_id)

    await executor.resume_blocked_execution(ws_id, execution_owner_id="worker-A")

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value


_BELOW_THRESHOLD_BASELINE = {
    "provider": "python",
    "percent": 87.5,
    "minimum_percent": 99.0,
    "enforce": True,
    "status": "below_threshold",
    "reason_code": "COVERAGE_BELOW_THRESHOLD",
}


@pytest.mark.unit
async def test_begin_execution_reuses_persisted_baseline_on_directive_resume(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Regression for PRRT_kwDOSJAM6s6J4G5G: a revert/redo directive resume must
    # reuse the pre-agent baseline captured at block time (so the coverage
    # ratchet compares against the original base) instead of recomputing it
    # against the already-mutated blocked worktree.
    ws_id = await _seed_resumable_blocked_workspace(
        factory,
        grant_path=None,
        directive="revert the protected change",
        block_baseline_coverage=_BELOW_THRESHOLD_BASELINE,
    )

    begin = await executor._begin_execution(
        ws_id,
        resume_reason="blocked",
        execution_owner_id=None,
        execution_lease_expires_at=None,
    )

    assert begin is not None
    _ws, resume_skip_agent, resume_disable_fix_passes, baseline = begin
    assert resume_skip_agent is False  # directive re-invokes the agent
    # Directive-only resume (no grant): fix passes stay enabled.
    assert resume_disable_fix_passes is False
    assert baseline is not None
    assert baseline.percent == 87.5
    assert baseline.reason_code == "COVERAGE_BELOW_THRESHOLD"


@pytest.mark.unit
async def test_begin_execution_reuses_persisted_baseline_on_approve_and_keep(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Approve-and-keep resume skips the agent block entirely, so the only path
    # that surfaces the original baseline is this reuse — without it the coverage
    # gate would drop to ``None`` and fail a no-regression sub-threshold repo.
    ws_id = await _seed_resumable_blocked_workspace(
        factory,
        grant_path="pyproject.toml",
        block_baseline_coverage=_BELOW_THRESHOLD_BASELINE,
    )

    begin = await executor._begin_execution(
        ws_id,
        resume_reason="blocked",
        execution_owner_id=None,
        execution_lease_expires_at=None,
    )

    assert begin is not None
    _ws, resume_skip_agent, resume_disable_fix_passes, baseline = begin
    assert resume_skip_agent is True  # grant present -> agent skipped
    # Grant-only resume also disables the secondary fix passes / pre-commit repair.
    assert resume_disable_fix_passes is True
    assert baseline is not None
    assert baseline.percent == 87.5


@pytest.mark.unit
async def test_begin_execution_combined_directive_grant_disables_fix_passes(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Regression for PRRT_kwDOSJAM6s6J-nwp: a combined ``--directive ... --grant
    # ...`` guide arms BOTH a directive (the agent must re-run) and active grants.
    # The directive branch must keep ``resume_skip_agent`` False (so the directive
    # runs) while still setting ``resume_disable_fix_passes`` True because grants
    # are active — otherwise a secondary validation fix pass / pre-commit repair
    # could rewrite the granted protected file and have the new violation
    # suppressed by the same single-use grant (the danger PRRT_kwDOSJAM6s6J7EUX /
    # PRRT_kwDOSJAM6s6J5SDf closed for grant-only resumes).
    ws_id = await _seed_resumable_blocked_workspace(
        factory,
        grant_path="pyproject.toml",
        directive="also tidy up the changelog",
        block_baseline_coverage=_BELOW_THRESHOLD_BASELINE,
    )

    begin = await executor._begin_execution(
        ws_id,
        resume_reason="blocked",
        execution_owner_id=None,
        execution_lease_expires_at=None,
    )

    assert begin is not None
    _ws, resume_skip_agent, resume_disable_fix_passes, baseline = begin
    assert resume_skip_agent is False  # the directive must re-invoke the agent
    assert resume_disable_fix_passes is True  # grants active -> fix passes off
    assert baseline is not None
    assert baseline.percent == 87.5


@pytest.mark.unit
async def test_begin_execution_without_persisted_baseline_returns_none(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # No baseline captured on the original run (coverage skipped/absent): the
    # resume faithfully reproduces the missing baseline rather than inventing one.
    ws_id = await _seed_resumable_blocked_workspace(factory, grant_path="pyproject.toml")

    begin = await executor._begin_execution(
        ws_id,
        resume_reason="blocked",
        execution_owner_id=None,
        execution_lease_expires_at=None,
    )

    assert begin is not None
    _ws, _resume_skip_agent, _resume_disable_fix_passes, baseline = begin
    assert baseline is None


@pytest.mark.unit
async def test_measure_and_persist_baseline_coverage_fresh_run(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fresh run measures the pre-agent baseline and durably persists it so a
    # later pause into ``blocked`` can hand the original base to the resume path.
    ws_id = await _seed_resumable_blocked_workspace(factory, grant_path=None)
    measured = ValidationCoverageResult(
        provider="python",
        percent=90.0,
        minimum_percent=99.0,
        enforce=True,
        status="below_threshold",
        reason_code="COVERAGE_BELOW_THRESHOLD",
    )

    async def _preflight(**_kwargs: Any) -> ValidationCoverageResult:
        return measured

    monkeypatch.setattr(executor, "_run_baseline_coverage_preflight", _preflight)

    result = await executor._measure_and_persist_baseline_coverage(
        workspace_id=ws_id,
        compose_project="proj",
        compose_file=Path("compose.yml"),
        profile=object(),  # type: ignore[arg-type]
    )

    assert result is measured
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.block_baseline_coverage is not None
        assert ws.block_baseline_coverage["percent"] == 90.0


@pytest.mark.unit
async def test_measure_and_persist_baseline_coverage_skip_reuses_without_measuring(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A directive resume passes skip_measure=True: the reused base is returned
    # verbatim, the preflight is never run, and nothing is re-persisted.
    ws_id = await _seed_resumable_blocked_workspace(factory, grant_path=None)
    reuse = ValidationCoverageResult(
        provider="python",
        percent=87.5,
        minimum_percent=99.0,
        enforce=True,
        status="below_threshold",
        reason_code="COVERAGE_BELOW_THRESHOLD",
    )
    preflight_calls: list[dict[str, Any]] = []

    async def _preflight(**kwargs: Any) -> ValidationCoverageResult | None:
        preflight_calls.append(kwargs)
        return None

    monkeypatch.setattr(executor, "_run_baseline_coverage_preflight", _preflight)

    result = await executor._measure_and_persist_baseline_coverage(
        workspace_id=ws_id,
        compose_project="proj",
        compose_file=Path("compose.yml"),
        profile=object(),  # type: ignore[arg-type]
        reuse=reuse,
        skip_measure=True,
    )

    assert result is reuse
    assert preflight_calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.block_baseline_coverage is None


@pytest.mark.unit
async def test_persist_block_planning_conformance_handoff_round_trips(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The pending handoff is serialized to a JSON column at the single point it is
    # known so a later pause into ``blocked`` can reconstruct it on an
    # approve-and-keep resume (PRRT_kwDOSJAM6s6KAbBL).
    from awf.control.executor.recovery_payloads import (
        _planning_validation_handoff_from_metadata,
    )
    from awf.control.executor.types import _PlanningValidationHandoff
    from awf.runtime.planning import (
        CONFORMANCE_REQUIRES_AWF_VALIDATION,
        PlanConformanceReport,
        PlanConformanceStatus,
    )

    ws_id = await _seed_resumable_blocked_workspace(factory, grant_path=None)
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is required.",
            gaps=("rerun pytest under AWF",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws.md"),
        report_path=Path("docs/awf-plans/ws.conformance.json"),
        iteration=1,
        max_iterations=3,
    )

    await executor._persist_block_planning_conformance_handoff(ws_id, handoff=handoff)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert (
            _planning_validation_handoff_from_metadata(ws.block_planning_conformance_handoff)
            == handoff
        )


@pytest.mark.unit
async def test_persist_block_planning_conformance_handoff_clears_when_none(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # No pending handoff (conformance satisfied inline) persists ``None`` so a
    # resume faithfully reproduces the original run rather than inventing one.
    ws_id = await _seed_resumable_blocked_workspace(
        factory,
        grant_path=None,
        block_planning_conformance_handoff=_PENDING_CONFORMANCE_HANDOFF,
    )

    await executor._persist_block_planning_conformance_handoff(ws_id, handoff=None)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.block_planning_conformance_handoff is None


@pytest.mark.unit
async def test_persist_block_planning_conformance_handoff_missing_workspace_is_a_noop(
    executor: WorkspaceExecutor,
) -> None:
    # The row vanished (e.g. concurrent destroy) before the persist landed: the
    # write is a best-effort no-op rather than raising, mirroring the baseline
    # persist guard.
    await executor._persist_block_planning_conformance_handoff("ws_does_not_exist", handoff=None)
