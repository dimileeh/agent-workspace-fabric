"""Tests for ``scripts.schedule_release_pr._monitor_already_running``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from scripts.schedule_release_pr import _monitor_already_running, _repo_url_variants
from tests.postgres import postgres_test_url


async def _insert_ws(
    database_url: str,
    *,
    status: str,
    ws_id: str | None = None,
    task_kind: str = "sync_release_pr",
    repo_url: str = "git@github.com:dimileeh/aira-web.git",
    pr_number: int = 278,
    updated_at: datetime | None = None,
) -> str:
    engine = make_engine(database_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url=repo_url,
                branch_base="development",
                task_title="release monitor",
                task_prompt="monitor the release PR",
                agent="codex",
                test_commands=[],
                task_kind=task_kind,
            )
            if ws_id is not None:
                workspace.id = ws_id
            workspace.status = status
            workspace.pr_number = pr_number
            if updated_at is not None:
                workspace.updated_at = updated_at
            await session.commit()
            if updated_at is not None:
                await session.execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id)
                    .values(updated_at=updated_at)
                )
                await session.commit()
            return workspace.id
    finally:
        await engine.dispose()


class TestDbBasedIdempotency:
    @pytest.mark.unit
    async def test_database_unavailable_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv(
            "AWF_DATABASE_URL",
            "postgresql+asyncpg://awf:awf_dev@127.0.0.1:1/awf",
        )

        assert (
            await _monitor_already_running(
                work_dir=tmp_path,
                repo_slug="dimileeh/aira-web",
                pr_number=278,
            )
            is False
        )

    @pytest.mark.unit
    async def test_no_matching_row_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        async with postgres_test_url() as database_url:
            monkeypatch.setenv("AWF_DATABASE_URL", database_url)
            await _insert_ws(database_url, status="provisioning", pr_number=999)
            assert (
                await _monitor_already_running(
                    work_dir=tmp_path,
                    repo_slug="dimileeh/aira-web",
                    pr_number=278,
                )
                is False
            )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        [
            WorkspaceStatus.provisioning.value,
            WorkspaceStatus.ready.value,
            WorkspaceStatus.running.value,
            WorkspaceStatus.validating.value,
            WorkspaceStatus.pushing.value,
            WorkspaceStatus.monitoring_pr.value,
        ],
    )
    async def test_non_terminal_row_returns_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        status: str,
    ) -> None:
        async with postgres_test_url() as database_url:
            monkeypatch.setenv("AWF_DATABASE_URL", database_url)
            await _insert_ws(database_url, status=status)
            assert (
                await _monitor_already_running(
                    work_dir=tmp_path,
                    repo_slug="dimileeh/aira-web",
                    pr_number=278,
                )
                is True
            )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status", [WorkspaceStatus.completed.value, WorkspaceStatus.failed.value]
    )
    async def test_old_terminal_row_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        status: str,
    ) -> None:
        async with postgres_test_url() as database_url:
            monkeypatch.setenv("AWF_DATABASE_URL", database_url)
            await _insert_ws(
                database_url,
                status=status,
                updated_at=datetime.now(UTC) - timedelta(minutes=10),
            )
            assert (
                await _monitor_already_running(
                    work_dir=tmp_path,
                    repo_slug="dimileeh/aira-web",
                    pr_number=278,
                )
                is False
            )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status", [WorkspaceStatus.completed.value, WorkspaceStatus.failed.value]
    )
    async def test_recent_terminal_row_returns_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        status: str,
    ) -> None:
        async with postgres_test_url() as database_url:
            monkeypatch.setenv("AWF_DATABASE_URL", database_url)
            await _insert_ws(database_url, status=status, updated_at=datetime.now(UTC))
            assert (
                await _monitor_already_running(
                    work_dir=tmp_path,
                    repo_slug="dimileeh/aira-web",
                    pr_number=278,
                )
                is True
            )


@pytest.mark.unit
def test_repo_url_variants_cover_ssh_https_and_bare_forms() -> None:
    variants = set(_repo_url_variants("dimileeh/aira-web"))

    assert "git@github.com:dimileeh/aira-web.git" in variants
    assert "https://github.com/dimileeh/aira-web.git" in variants
    assert "https://github.com/dimileeh/aira-web" in variants
