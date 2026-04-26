"""Out-of-scope change policy tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    PolicyFindingRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_engine, make_session_factory
from awf.service.scope_policy import (
    OUT_OF_SCOPE_CHANGE_CODE,
    OutOfScopeChangePolicy,
    ScopePolicyRefreshService,
    evaluate_out_of_scope_changes,
    out_of_scope_policy_for_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_open_candidate(
    factory: async_sessionmaker[AsyncSession],
    *,
    owned_paths: list[str],
    resolved_profile: dict | None = None,
) -> tuple[str, str]:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/svc.git",
            branch_base="development",
            task_title="Scope policy fixture",
            task_prompt="Implement scoped change.",
            task_external_id="TICKET-SCOPE",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            owned_paths=owned_paths,
            resolved_profile=resolved_profile,
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=None,
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
        workspace.base_commit = "a" * 40
        workspace.pr_url = "https://github.com/example/svc/pull/41"
        workspace.pr_number = 41
        await repo.transition(
            workspace,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="PR_OPENED",
        )
        attempt.is_canonical_for_merge = True
        candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha="h" * 40,
            base_sha="a" * 40,
        )
        await session.commit()
        return workspace.id, candidate.id


@pytest.mark.unit
def test_changed_files_outside_owned_paths_produce_out_of_scope_findings() -> None:
    findings = evaluate_out_of_scope_changes(
        changed_paths=("src/app/service.py", "tests/unit/test_service.py"),
        owned_paths=("tests/unit/**",),
        policy=OutOfScopeChangePolicy(),
    )

    assert [(finding.reason_code, finding.path, finding.severity) for finding in findings] == [
        (OUT_OF_SCOPE_CHANGE_CODE, "src/app/service.py", "warning")
    ]


@pytest.mark.unit
def test_allowlist_patterns_suppress_expected_generated_and_docs_files() -> None:
    findings = evaluate_out_of_scope_changes(
        changed_paths=("docs/generated/client.md", "schema/generated/types.py"),
        owned_paths=("src/app/**",),
        policy=OutOfScopeChangePolicy(
            allowlist_patterns=("docs/generated/**", "schema/generated/**")
        ),
    )

    assert findings == []


@pytest.mark.unit
def test_default_mode_is_warn_but_profile_or_task_policy_can_block() -> None:
    default_policy = out_of_scope_policy_for_workspace(Workspace(resolved_profile=None))
    profile_policy = out_of_scope_policy_for_workspace(
        Workspace(
            resolved_profile={
                "quality": {"out_of_scope_changes": {"mode": "block"}},
            }
        )
    )
    task_policy = out_of_scope_policy_for_workspace(
        Workspace(
            task_policy={"out_of_scope_changes": {"mode": "block"}},
        )
    )

    assert default_policy.mode == "warn"
    assert profile_policy.mode == "block"
    assert task_policy.mode == "block"


@pytest.mark.unit
async def test_refresh_records_structured_out_of_scope_policy_findings(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, candidate_id = await _seed_open_candidate(
        factory,
        owned_paths=["src/owned/**"],
    )

    async with factory() as session:
        result = await ScopePolicyRefreshService(session).refresh_candidate(
            candidate_id,
            changed_paths=("src/unowned.py", "src/owned/module.py"),
        )
        await session.commit()

    async with factory() as session:
        findings = await PolicyFindingRepository(session).list_active_for_workspace(workspace_id)
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.policy_finding",
        )

    assert [finding.reason_code for finding in result.findings] == [OUT_OF_SCOPE_CHANGE_CODE]
    assert len(findings) == 1
    assert findings[0].reason_code == OUT_OF_SCOPE_CHANGE_CODE
    assert findings[0].severity == "warning"
    assert findings[0].subject_path == "src/unowned.py"
    assert events[0].reason_code == OUT_OF_SCOPE_CHANGE_CODE
    assert events[0].payload["path"] == "src/unowned.py"
