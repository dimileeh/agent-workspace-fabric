"""Additional PR monitor runner persistence regressions."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.repositories import PRFeedbackResolutionRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_pr_feedback_resolution_body_change_creates_new_comment_identity(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        repo = PRFeedbackResolutionRepository(session)
        await repo.record_resolution(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
            pull_request_url="https://github.com/dimileeh/aira-web/pull/42",
            head_sha="old-head",
            feedback_kind="review_comment",
            feedback_id="issue:4391271818",
            feedback_body="old body",
            feedback_author="chatgpt-codex-connector[bot]",
            feedback_url="https://github.example/comment/4391271818",
            verdict="false_positive",
            reason="old comment body",
            source_workspace_id=workspace_id,
        )
        await repo.record_resolution(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
            pull_request_url="https://github.com/dimileeh/aira-web/pull/42",
            head_sha="new-head",
            feedback_kind="review_comment",
            feedback_id="issue:4391271818",
            feedback_body="new body with new actionable content",
            feedback_author="chatgpt-codex-connector[bot]",
            feedback_url="https://github.example/comment/4391271818",
            verdict="defer",
            reason="body changed, so the monitor must re-evaluate it",
            source_workspace_id=workspace_id,
        )
        await session.commit()

        rows = await repo.list_for_pr(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
        )

    assert len(rows) == 2
    assert {row.reason for row in rows} == {
        "old comment body",
        "body changed, so the monitor must re-evaluate it",
    }
