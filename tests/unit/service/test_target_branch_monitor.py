"""Target-branch integration monitor tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    StaleReasonRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.alembic_resolver import (
    AlembicResolveResult,
    AlembicResolveStatus,
)
from awf.service.staleness import TargetBranchState
from awf.service.target_branch_monitor import (
    CandidateRefreshSummary,
    GitCheckoutTargetBranchStateProvider,
    ReconcileAndRefreshResult,
    TargetBranchMonitorError,
    TargetBranchMonitorResult,
    TargetBranchMonitorStatus,
    TargetBranchReconcileMonitor,
    _generated_relative_path,
    reconcile_and_refresh_stale_candidates,
    run_target_branch_reconcile_once,
)
from tests.postgres import postgres_test_engine


def async_lambda(value: TargetBranchState) -> Any:
    async def _fn(_base_sha: str) -> TargetBranchState:
        return value

    return _fn


class _StubResolver:
    def __init__(self, result: AlembicResolveResult) -> None:
        self.result = result
        self.calls: list[Path] = []

    def resolve(self, repo_path: Path) -> AlembicResolveResult:
        self.calls.append(repo_path)
        return self.result


class _ResolvingStubResolver:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def resolve(self, repo_path: Path) -> AlembicResolveResult:
        self.calls.append(repo_path)
        return _resolved(repo_path)


def _resolved(path: Path) -> AlembicResolveResult:
    return AlembicResolveResult(
        status=AlembicResolveStatus.resolved,
        reason_code="ALEMBIC_HEADS_MERGED",
        heads=("left001", "right001"),
        generated_revision="merge001",
        generated_path=path / "migrations" / "versions" / "merge001_merge_alembic_heads.py",
    )


@pytest.mark.unit
async def test_monitor_commits_and_pushes_follow_up_merge_revision(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    runner.queue_result()  # git add
    runner.queue_result(returncode=1)  # diff --cached --quiet means staged changes
    runner.queue_result()  # commit
    runner.queue_result(stdout="abc123\n")  # rev-parse HEAD
    runner.queue_result()  # push
    resolver = _ResolvingStubResolver()
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(resolver,),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
    )

    assert result.status == TargetBranchMonitorStatus.committed
    assert result.commit_sha == "abc123"
    assert result.pushed is True
    assert resolver.calls == [result.checkout_path]
    assert runner.calls[0].args[:4] == ["git", "clone", "--branch", "development"]
    assert any(call.args[:3] == ["git", "-C", str(result.checkout_path)] for call in runner.calls)
    commit_call = next(call for call in runner.calls if "commit" in call.args)
    assert "fix(migrations): merge Alembic heads on development" in commit_call.args
    add_call = next(call for call in runner.calls if call.args[3] == "add")
    assert add_call.args[-1] == "migrations/versions/merge001_merge_alembic_heads.py"
    assert result.to_dict()["changed_paths"] == [
        "migrations/versions/merge001_merge_alembic_heads.py"
    ]
    assert runner.calls[-1].args == [
        "git",
        "-C",
        str(result.checkout_path),
        "push",
        "origin",
        "HEAD:development",
    ]


@pytest.mark.unit
async def test_monitor_noops_when_resolvers_find_no_target_branch_issue(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    resolver = _StubResolver(
        AlembicResolveResult(
            status=AlembicResolveStatus.not_needed,
            reason_code="ALEMBIC_SINGLE_HEAD",
            heads=("head001",),
        )
    )
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(resolver,),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
    )

    assert result.status == TargetBranchMonitorStatus.clean
    assert result.to_dict()["status"] == "clean"
    assert result.pushed is False
    assert [call.args for call in runner.calls] == [
        [
            "git",
            "clone",
            "--branch",
            "development",
            "--single-branch",
            "git@github.com:example/repo.git",
            str(result.checkout_path),
        ]
    ]


@pytest.mark.unit
async def test_monitor_dry_run_reports_would_commit_without_git_write(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(_ResolvingStubResolver(),),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
        dry_run=True,
    )

    assert result.status == TargetBranchMonitorStatus.would_commit
    assert result.pushed is False
    assert result.to_dict()["dry_run"] is True
    assert result.to_dict()["policy_reason_code"] == "TARGET_BRANCH_DRY_RUN"
    assert len(runner.calls) == 1


@pytest.mark.unit
async def test_monitor_policy_blocked_result_does_not_stage_commit_or_push(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(_ResolvingStubResolver(),),
        allow_commits=False,
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
    )

    assert result.status == TargetBranchMonitorStatus.policy_blocked
    assert result.pushed is False
    assert len(runner.calls) == 1
    payload = result.to_dict()
    assert payload["commit_allowed"] is False
    assert payload["policy_reason_code"] == "TARGET_BRANCH_COMMIT_POLICY_DENIED"
    assert payload["changed_paths"] == ["migrations/versions/merge001_merge_alembic_heads.py"]


@pytest.mark.unit
async def test_monitor_refreshes_existing_checkout_before_resolving(tmp_path: Path) -> None:
    checkout = tmp_path / "target-branches" / "repo-3f11352a998e-development"
    (checkout / ".git").mkdir(parents=True)
    runner = FakeCommandRunner()
    runner.queue_result()  # fetch
    runner.queue_result()  # checkout
    runner.queue_result()  # reset
    runner.queue_result()  # clean
    resolver = _StubResolver(
        AlembicResolveResult(
            status=AlembicResolveStatus.unsupported,
            reason_code="ALEMBIC_NOT_CONFIGURED",
            heads=(),
        )
    )
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(resolver,),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
    )

    assert result.status == TargetBranchMonitorStatus.clean
    assert resolver.calls == [checkout]
    assert [call.args for call in runner.calls] == [
        ["git", "-C", str(checkout), "fetch", "origin", "development", "--prune"],
        ["git", "-C", str(checkout), "checkout", "development"],
        ["git", "-C", str(checkout), "reset", "--hard", "origin/development"],
        ["git", "-C", str(checkout), "clean", "-fd"],
    ]


@pytest.mark.unit
async def test_existing_non_git_checkout_raises_before_resolver_or_git(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "target-branches" / "repo-3f11352a998e-development"
    checkout.mkdir(parents=True)
    runner = FakeCommandRunner()
    resolver = _StubResolver(_resolved(tmp_path))
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(resolver,),
    )

    with pytest.raises(RuntimeError, match="not a git checkout"):
        await monitor.reconcile(
            repo_url="git@github.com:example/repo.git",
            branch="development",
        )

    assert runner.calls == []
    assert resolver.calls == []


@pytest.mark.unit
async def test_monitor_returns_clean_when_resolver_change_has_no_staged_diff(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    runner.queue_result()  # add generated file
    runner.queue_result(returncode=0)  # diff --cached --quiet: no staged changes
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(_ResolvingStubResolver(),),
    )

    result = await monitor.reconcile(
        repo_url="git@github.com:example/repo.git",
        branch="development",
    )

    assert result.status == TargetBranchMonitorStatus.clean
    assert result.pushed is False
    assert runner.calls[0].args[1] == "clone"
    assert runner.calls[1].args[3] == "add"
    assert runner.calls[2].args[3:6] == ["diff", "--cached", "--quiet"]


@pytest.mark.unit
async def test_monitor_raises_when_cached_diff_returns_unexpected_code(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    runner.queue_result()  # add
    runner.queue_result(returncode=2, stderr="diff failed")
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(_ResolvingStubResolver(),),
    )

    with pytest.raises(TargetBranchMonitorError) as exc_info:
        await monitor.reconcile(
            repo_url="git@github.com:example/repo.git",
            branch="development",
        )

    assert exc_info.value.operation == "target_branch.git_diff_cached"
    assert "diff failed" in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("existing_checkout", "operation"),
    [
        (False, "target_branch.git_clone"),
        (True, "target_branch.git_fetch"),
    ],
)
async def test_monitor_command_failures_report_operation_names(
    tmp_path: Path,
    existing_checkout: bool,
    operation: str,
) -> None:
    checkout = tmp_path / "target-branches" / "repo-3f11352a998e-development"
    if existing_checkout:
        (checkout / ".git").mkdir(parents=True)
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="git failed")
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(_StubResolver(_resolved(tmp_path)),),
    )

    with pytest.raises(TargetBranchMonitorError) as exc_info:
        await monitor.reconcile(
            repo_url="git@github.com:example/repo.git",
            branch="development",
        )

    assert exc_info.value.operation == operation
    assert "git failed" in str(exc_info.value)


@pytest.mark.unit
async def test_checkout_path_slug_fallbacks_and_convenience_entrypoint(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result()  # clone
    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=tmp_path,
        resolvers=(
            _StubResolver(
                AlembicResolveResult(
                    status=AlembicResolveStatus.not_needed,
                    reason_code="ALEMBIC_SINGLE_HEAD",
                    heads=("head",),
                )
            ),
        ),
    )
    fallback_path = monitor.checkout_path(repo_url="///", branch="///")

    result = await run_target_branch_reconcile_once(
        runner=runner,
        work_dir=tmp_path,
        repo_url="git@github.com:example/repo.git",
        branch="development",
        dry_run=True,
    )

    assert fallback_path.name.startswith("repo-")
    assert fallback_path.name.endswith("-branch")
    assert result.status == TargetBranchMonitorStatus.clean


# ── Reconcile + staleness refresh integration ────────────────────────────

_REPO_URL = "git@github.com:example/svc.git"
_BASE_BRANCH = "development"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


_pr_counter = 0


async def _seed_open_candidate(
    factory: async_sessionmaker[AsyncSession],
    *,
    owned_paths: list[str],
    task_class: str | None = None,
    base_sha: str = "a" * 40,
    repo_url: str = _REPO_URL,
    base_branch: str = _BASE_BRANCH,
) -> tuple[str, str, str]:
    global _pr_counter
    _pr_counter += 1
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url=repo_url,
            branch_base=base_branch,
            task_title="Reconcile fixture",
            task_prompt="Implement.",
            task_external_id=f"TICKET-RECONCILE-{_pr_counter}",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            owned_paths=owned_paths,
            task_class=task_class,
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=f"TICKET-RECONCILE-{_pr_counter}",
            idempotency_key=None,
            task_class=task_class,
            owned_paths=owned_paths,
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
        ):
            await repo.transition(workspace, to=target, reason_code="TEST")
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = base_sha
        pr_num = 1000 + _pr_counter
        workspace.pr_url = f"https://github.com/example/svc/pull/{pr_num}"
        workspace.pr_number = pr_num
        await repo.transition(
            workspace,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="PR_OPENED",
        )
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
        assert attempt is not None
        candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha="h" * 40,
            base_sha=base_sha,
        )
        await session.commit()
        return workspace.id, attempt.id, candidate.id


def _fake_reconcile_result(
    *,
    checkout_path: Path,
    status: TargetBranchMonitorStatus = TargetBranchMonitorStatus.clean,
) -> TargetBranchMonitorResult:
    return TargetBranchMonitorResult(
        repo_url=_REPO_URL,
        branch=_BASE_BRANCH,
        checkout_path=checkout_path,
        status=status,
        resolver_results=(),
    )


class TestReconcileAndRefreshStaleCandidates:
    @pytest.mark.unit
    async def test_reconcile_and_refresh_refreshes_open_candidates(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id_1, _attempt_id_1, cand_id_1 = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )
        _ws_id_2, _attempt_id_2, cand_id_2 = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/db/**"],
            task_class="refactor_task",
        )

        async def _reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            return _fake_reconcile_result(checkout_path=tmp_path / "checkout")

        target_state = TargetBranchState(
            branch=_BASE_BRANCH,
            head_sha="c" * 40,
            changed_paths=("src/awf/api/routes/health.py", "src/awf/db/models.py"),
            advanced_commits=3,
        )

        result = await reconcile_and_refresh_stale_candidates(
            reconcile_fn=_reconcile,
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            session_factory=factory,
            target_state_for_base_sha=async_lambda(target_state),
        )

        assert isinstance(result, ReconcileAndRefreshResult)
        assert result.reconcile.status == TargetBranchMonitorStatus.clean
        assert len(result.candidate_refreshes) == 2
        refreshed_ids = {s.candidate_id for s in result.candidate_refreshes}
        assert cand_id_1 in refreshed_ids
        assert cand_id_2 in refreshed_ids
        assert all(s.error is None for s in result.candidate_refreshes)
        assert all(s.stale is True for s in result.candidate_refreshes)

        async with factory() as session:
            c1 = await MergeCandidateRepository(session).get_by_attempt_id(_attempt_id_1)
        assert c1 is not None
        assert c1.stale is True

    @pytest.mark.unit
    async def test_reconcile_and_refresh_summary_reports_sensitive_stale_reason(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        _ws_id, _attempt_id, cand_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/service/**"],
            task_class="dependency_task",
        )

        async def _reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            return _fake_reconcile_result(checkout_path=tmp_path / "checkout")

        result = await reconcile_and_refresh_stale_candidates(
            reconcile_fn=_reconcile,
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            session_factory=factory,
            target_state_for_base_sha=async_lambda(
                TargetBranchState(
                    branch=_BASE_BRANCH,
                    head_sha="c" * 40,
                    changed_paths=("uv.lock",),
                    advanced_commits=1,
                )
            ),
        )

        assert len(result.candidate_refreshes) == 1
        summary = result.candidate_refreshes[0]
        assert summary.candidate_id == cand_id
        assert summary.stale is True
        assert summary.findings_count == 1
        assert summary.stale_reason == "STALE_DEPENDENCY"

    @pytest.mark.unit
    async def test_reconcile_and_refresh_isolates_candidate_refresh_failure(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id_1, attempt_id_1, cand_id_1 = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )
        _ws_id_2, _attempt_id_2, cand_id_2 = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/db/**"],
            task_class="refactor_task",
        )

        async def _reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            return _fake_reconcile_result(checkout_path=tmp_path / "checkout")

        call_count = 0

        async def _target_state_partial(base_sha: str) -> TargetBranchState:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated provider failure")
            return TargetBranchState(
                branch=_BASE_BRANCH,
                head_sha="c" * 40,
                changed_paths=("src/awf/db/models.py",),
                advanced_commits=2,
            )

        result = await reconcile_and_refresh_stale_candidates(
            reconcile_fn=_reconcile,
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            session_factory=factory,
            target_state_for_base_sha=_target_state_partial,
        )

        assert result.reconcile.status == TargetBranchMonitorStatus.clean
        errors = [s for s in result.candidate_refreshes if s.error is not None]
        successes = [s for s in result.candidate_refreshes if s.error is None]
        assert len(errors) == 1
        assert len(successes) == 1
        assert "simulated provider failure" in errors[0].error

    @pytest.mark.unit
    async def test_reconcile_and_refresh_excludes_just_merged_workspace(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id_1, _attempt_id_1, cand_id_1 = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )
        ws_id_2, _attempt_id_2, cand_id_2 = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/db/**"],
            task_class="refactor_task",
        )

        async def _reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            return _fake_reconcile_result(checkout_path=tmp_path / "checkout")

        target_state = TargetBranchState(
            branch=_BASE_BRANCH,
            head_sha="c" * 40,
            changed_paths=("src/awf/api/routes/health.py",),
            advanced_commits=1,
        )

        result = await reconcile_and_refresh_stale_candidates(
            reconcile_fn=_reconcile,
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            session_factory=factory,
            target_state_for_base_sha=async_lambda(target_state),
            exclude_workspace_ids={ws_id_1},
        )

        refreshed_ids = {s.candidate_id for s in result.candidate_refreshes}
        assert cand_id_1 not in refreshed_ids
        assert cand_id_2 in refreshed_ids

    @pytest.mark.unit
    async def test_reconcile_after_merge_marks_only_overlapping_open_candidate_stale(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service_ws_id, service_attempt_id, service_candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/service/**"],
            task_class="test_task",
        )
        docs_ws_id, docs_attempt_id, docs_candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["docs/**"],
            task_class="docs_task",
        )
        merged_ws_id, _merged_attempt_id, merged_candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/service/staleness.py"],
            task_class="test_task",
        )

        async def _reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            return _fake_reconcile_result(checkout_path=tmp_path / "checkout")

        target_state = TargetBranchState(
            branch=_BASE_BRANCH,
            head_sha="c" * 40,
            changed_paths=("src/awf/service/staleness.py",),
            advanced_commits=1,
        )

        result = await reconcile_and_refresh_stale_candidates(
            reconcile_fn=_reconcile,
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            session_factory=factory,
            target_state_for_base_sha=async_lambda(target_state),
            exclude_workspace_ids={merged_ws_id},
        )

        summaries = {summary.candidate_id: summary for summary in result.candidate_refreshes}
        assert set(summaries) == {service_candidate_id, docs_candidate_id}
        assert merged_candidate_id not in summaries
        assert summaries[service_candidate_id].stale is True
        assert summaries[service_candidate_id].stale_reason == "STALE_OVERLAP"
        assert summaries[service_candidate_id].findings_count == 1
        assert summaries[docs_candidate_id].stale is False
        assert summaries[docs_candidate_id].stale_reason is None
        assert summaries[docs_candidate_id].findings_count == 0

        async with factory() as session:
            service_candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                service_attempt_id,
            )
            docs_candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                docs_attempt_id,
            )
            service_reasons = await StaleReasonRepository(session).list_active_for_candidate(
                service_candidate_id,
            )
            docs_reasons = await StaleReasonRepository(session).list_active_for_candidate(
                docs_candidate_id,
            )
            service_events = await WorkspaceEventRepository(session).list(
                workspace_id=service_ws_id,
                event_type="merge_candidate.stale_detected",
            )
            docs_events = await WorkspaceEventRepository(session).list(
                workspace_id=docs_ws_id,
                event_type="merge_candidate.stale_detected",
            )

        assert service_candidate is not None
        assert service_candidate.stale is True
        assert service_candidate.stale_reason == "STALE_OVERLAP"
        assert docs_candidate is not None
        assert docs_candidate.stale is False
        assert docs_candidate.stale_reason is None
        assert [
            (reason.reason_code, reason.trigger_type, reason.trigger_ref)
            for reason in service_reasons
        ] == [
            (
                "STALE_OVERLAP",
                "path_overlap",
                "src/awf/service/staleness.py",
            )
        ]
        assert docs_reasons == []
        assert len(service_events) == 1
        assert service_events[0].reason_code == "STALE_OVERLAP"
        assert service_events[0].payload is not None
        assert service_events[0].payload["trigger_ref"] == "src/awf/service/staleness.py"
        assert docs_events == []

    @pytest.mark.unit
    async def test_reconcile_after_merge_non_overlap_leaves_candidate_without_overlap_reason(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        _docs_ws_id, docs_attempt_id, docs_candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["docs/**"],
            task_class="docs_task",
        )

        async def _reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            return _fake_reconcile_result(checkout_path=tmp_path / "checkout")

        result = await reconcile_and_refresh_stale_candidates(
            reconcile_fn=_reconcile,
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            session_factory=factory,
            target_state_for_base_sha=async_lambda(
                TargetBranchState(
                    branch=_BASE_BRANCH,
                    head_sha="c" * 40,
                    changed_paths=("src/awf/service/staleness.py",),
                    advanced_commits=1,
                )
            ),
        )

        assert len(result.candidate_refreshes) == 1
        summary = result.candidate_refreshes[0]
        assert summary.candidate_id == docs_candidate_id
        assert summary.stale is False
        assert summary.stale_reason is None
        assert summary.findings_count == 0

        async with factory() as session:
            docs_candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                docs_attempt_id,
            )
            docs_reasons = await StaleReasonRepository(session).list_active_for_candidate(
                docs_candidate_id,
            )

        assert docs_candidate is not None
        assert docs_candidate.stale is False
        assert docs_candidate.stale_reason is None
        assert docs_reasons == []

    @pytest.mark.unit
    async def test_reconcile_and_refresh_returns_structured_result_data(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        _ws_id, _attempt_id, _cand_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async def _reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            return _fake_reconcile_result(checkout_path=tmp_path / "checkout")

        target_state = TargetBranchState(
            branch=_BASE_BRANCH,
            head_sha="c" * 40,
            changed_paths=("src/awf/api/routes/health.py",),
            advanced_commits=1,
        )

        result = await reconcile_and_refresh_stale_candidates(
            reconcile_fn=_reconcile,
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            session_factory=factory,
            target_state_for_base_sha=async_lambda(target_state),
        )

        assert isinstance(result, ReconcileAndRefreshResult)
        d = result.to_dict()
        assert "reconcile" in d
        assert "candidate_refreshes" in d
        assert d["reconcile"]["status"] == "clean"
        assert len(d["candidate_refreshes"]) == 1
        summary = d["candidate_refreshes"][0]
        assert "candidate_id" in summary
        assert "workspace_id" in summary
        assert "stale" in summary
        assert "findings_count" in summary
        assert "error" in summary

    @pytest.mark.unit
    async def test_reconcile_and_refresh_skips_refresh_when_reconcile_fails(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        _ws_id, _attempt_id, _cand_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async def _failing_reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            raise TargetBranchMonitorError(
                operation="target_branch.git_clone",
                result=MagicMock(returncode=128, stderr="clone failed", stdout=""),
            )

        with pytest.raises(TargetBranchMonitorError, match="clone failed"):
            await reconcile_and_refresh_stale_candidates(
                reconcile_fn=_failing_reconcile,
                repo_url=_REPO_URL,
                branch=_BASE_BRANCH,
                session_factory=factory,
                target_state_for_base_sha=async_lambda(
                    TargetBranchState(
                        branch=_BASE_BRANCH,
                        head_sha="c" * 40,
                        changed_paths=(),
                        advanced_commits=0,
                    )
                ),
            )

    @pytest.mark.unit
    async def test_reconcile_and_refresh_handles_null_base_sha(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        _ws_id, _attempt_id, cand_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )
        async with factory() as session:
            mc_repo = MergeCandidateRepository(session)
            candidates = await mc_repo.list_queue(repo_url=_REPO_URL, base_branch=_BASE_BRANCH)
            for c in candidates:
                c.base_sha = None
            await session.commit()

        async def _reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            return _fake_reconcile_result(checkout_path=tmp_path / "checkout")

        result = await reconcile_and_refresh_stale_candidates(
            reconcile_fn=_reconcile,
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            session_factory=factory,
            target_state_for_base_sha=async_lambda(
                TargetBranchState(
                    branch=_BASE_BRANCH,
                    head_sha="c" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=1,
                )
            ),
        )

        assert len(result.candidate_refreshes) == 1
        summary = result.candidate_refreshes[0]
        assert summary.candidate_id == cand_id
        assert summary.error is None
        assert summary.findings_count == 0

    @pytest.mark.unit
    async def test_reconcile_and_refresh_no_candidates_returns_empty_refreshes(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        async def _reconcile(
            *, repo_url: str, branch: str, dry_run: bool = False
        ) -> TargetBranchMonitorResult:
            return _fake_reconcile_result(checkout_path=tmp_path / "checkout")

        result = await reconcile_and_refresh_stale_candidates(
            reconcile_fn=_reconcile,
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            session_factory=factory,
            target_state_for_base_sha=async_lambda(
                TargetBranchState(
                    branch=_BASE_BRANCH,
                    head_sha="c" * 40,
                    changed_paths=(),
                    advanced_commits=0,
                )
            ),
        )

        assert result.candidate_refreshes == ()


class TestGitCheckoutTargetBranchStateProvider:
    @pytest.mark.unit
    async def test_fetch_uses_git_commands_to_build_target_branch_state(
        self,
        tmp_path: Path,
    ) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(stdout="c" * 40 + "\n")
        runner.queue_result(stdout="3\n")
        runner.queue_result(stdout="src/awf/api/routes/health.py\nsrc/awf/db/models.py\n")

        provider = GitCheckoutTargetBranchStateProvider(
            runner=runner,
            checkout_path=tmp_path,
        )
        state = await provider.fetch(
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            base_sha="a" * 40,
        )

        assert state.branch == _BASE_BRANCH
        assert state.head_sha == "c" * 40
        assert state.advanced_commits == 3
        assert state.changed_paths == (
            "src/awf/api/routes/health.py",
            "src/awf/db/models.py",
        )

    @pytest.mark.unit
    async def test_fetch_raises_on_git_failure(
        self,
        tmp_path: Path,
    ) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=128, stderr="rev-parse failed")

        provider = GitCheckoutTargetBranchStateProvider(
            runner=runner,
            checkout_path=tmp_path,
        )

        with pytest.raises(TargetBranchMonitorError, match="rev-parse failed"):
            await provider.fetch(
                repo_url=_REPO_URL,
                branch=_BASE_BRANCH,
                base_sha="a" * 40,
            )

    @pytest.mark.unit
    async def test_fetch_caches_head_sha_across_calls(
        self,
        tmp_path: Path,
    ) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(stdout="c" * 40 + "\n")
        runner.queue_result(stdout="3\n")
        runner.queue_result(stdout="a.py\n")
        runner.queue_result(stdout="5\n")
        runner.queue_result(stdout="b.py\n")

        provider = GitCheckoutTargetBranchStateProvider(
            runner=runner,
            checkout_path=tmp_path,
        )

        first = await provider.fetch(
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            base_sha="a" * 40,
        )
        second = await provider.fetch(
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            base_sha="b" * 40,
        )

        assert first.head_sha == "c" * 40
        assert second.head_sha == "c" * 40
        assert len(runner.calls) == 5
        assert runner.calls[0].args == ["git", "-C", str(tmp_path), "rev-parse", "HEAD"]
        assert runner.calls[1].args == [
            "git",
            "-C",
            str(tmp_path),
            "rev-list",
            "--count",
            "a" * 40 + "..HEAD",
        ]
        assert runner.calls[2].args == [
            "git",
            "-C",
            str(tmp_path),
            "diff",
            "--name-only",
            "a" * 40 + "..HEAD",
        ]
        assert runner.calls[3].args == [
            "git",
            "-C",
            str(tmp_path),
            "rev-list",
            "--count",
            "b" * 40 + "..HEAD",
        ]
        assert runner.calls[4].args == [
            "git",
            "-C",
            str(tmp_path),
            "diff",
            "--name-only",
            "b" * 40 + "..HEAD",
        ]


class TestCandidateRefreshSummary:
    @pytest.mark.unit
    def test_to_dict_includes_all_fields(self) -> None:
        summary = CandidateRefreshSummary(
            candidate_id="mc-1",
            workspace_id="ws-1",
            stale=True,
            stale_reason="branch_behind_target",
            findings_count=2,
            error=None,
        )
        d = summary.to_dict()
        assert d == {
            "candidate_id": "mc-1",
            "workspace_id": "ws-1",
            "stale": True,
            "stale_reason": "branch_behind_target",
            "findings_count": 2,
            "error": None,
        }

    @pytest.mark.unit
    def test_to_dict_with_error(self) -> None:
        summary = CandidateRefreshSummary(
            candidate_id="mc-2",
            workspace_id="ws-2",
            stale=False,
            findings_count=0,
            error="provider failed",
        )
        d = summary.to_dict()
        assert d["error"] == "provider failed"


class TestTargetBranchMonitorResult:
    @pytest.mark.unit
    def test_to_dict_exposes_operator_reconcile_details(self, tmp_path: Path) -> None:
        generated = tmp_path / "migrations" / "versions" / "merge001_merge_alembic_heads.py"
        result = TargetBranchMonitorResult(
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            checkout_path=tmp_path,
            status=TargetBranchMonitorStatus.committed,
            resolver_results=(
                AlembicResolveResult(
                    status=AlembicResolveStatus.resolved,
                    reason_code="ALEMBIC_HEADS_MERGED",
                    heads=("left001", "right001"),
                    generated_revision="merge001",
                    generated_path=generated,
                    generated_path_relative="migrations/versions/merge001_merge_alembic_heads.py",
                    message="Generated Alembic merge revision for 2 heads.",
                ),
            ),
            commit_sha="abc123",
            pushed=True,
            changed_paths=("migrations/versions/merge001_merge_alembic_heads.py",),
        )

        payload = result.to_dict()

        assert payload["status"] == "committed"
        assert payload["commit_sha"] == "abc123"
        assert payload["pushed"] is True
        assert payload["changed_paths"] == ["migrations/versions/merge001_merge_alembic_heads.py"]
        resolver = payload["resolver_results"][0]
        assert resolver["reason_code"] == "ALEMBIC_HEADS_MERGED"
        assert resolver["heads"] == ["left001", "right001"]
        assert resolver["generated_revision"] == "merge001"
        assert resolver["generated_path_relative"] == (
            "migrations/versions/merge001_merge_alembic_heads.py"
        )

    @pytest.mark.unit
    def test_generated_relative_path_rejects_missing_generated_path(self, tmp_path: Path) -> None:
        result = AlembicResolveResult(
            status=AlembicResolveStatus.resolved,
            reason_code="ALEMBIC_HEADS_MERGED",
            heads=("left001", "right001"),
            generated_revision="merge001",
            generated_path=None,
            generated_path_relative=None,
        )

        with pytest.raises(RuntimeError, match="generated path"):
            _generated_relative_path(result, tmp_path)


class TestReconcileAndRefreshResult:
    @pytest.mark.unit
    def test_to_dict_nests_reconcile_and_summaries(self) -> None:
        reconcile = TargetBranchMonitorResult(
            repo_url=_REPO_URL,
            branch=_BASE_BRANCH,
            checkout_path=Path("/tmp/checkout"),
            status=TargetBranchMonitorStatus.committed,
            resolver_results=(),
            commit_sha="abc123",
            pushed=True,
        )
        summary = CandidateRefreshSummary(
            candidate_id="mc-1",
            workspace_id="ws-1",
            stale=True,
            findings_count=1,
        )
        result = ReconcileAndRefreshResult(
            reconcile=reconcile,
            candidate_refreshes=(summary,),
        )
        d = result.to_dict()
        assert d["reconcile"]["status"] == "committed"
        assert d["reconcile"]["commit_sha"] == "abc123"
        assert len(d["candidate_refreshes"]) == 1
        assert d["candidate_refreshes"][0]["candidate_id"] == "mc-1"
