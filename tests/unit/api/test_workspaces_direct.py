"""Direct-call coverage for ``awf.api.routes.workspaces``.

The existing ``test_workspaces.py`` file covers the ASGI layer via
``httpx.AsyncClient`` — that verifies the REST contract but
coverage.py doesn't instrument the async handler bodies correctly in
that path (known limitation: httpx's ASGITransport runs the coroutine
in a context that ``sys.settrace`` doesn't fully follow).

This file calls the route functions DIRECTLY with a PostgreSQL-backed
session, which instruments cleanly. Same business logic, different
coverage path. Tests in both files together give us end-to-end
confidence plus instrumented line coverage."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.routes.workspaces import (
    _get_workspace_response,
    _list_workspace_responses,
    _payloads_match,
    create_workspace,
    get_workspace_secret_leases,
)
from awf.api.schemas import (
    WorkspaceAcceptedResponse,
    WorkspaceCreateRequest,
)
from awf.db.enums import AgentRuntime
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as s:
            yield s
            await s.commit()


def _payload(**overrides: object) -> WorkspaceCreateRequest:
    defaults: dict[str, object] = {
        "repo_url": "git@github.com:dimileeh/aira-web.git",
        "branch_base": "development",
        "task_title": "direct test",
        "task_prompt": "do a thing",
        "agent": AgentRuntime.codex,
        "test_commands": ["pytest -q"],
        "requires_database": False,
    }
    defaults.update(overrides)
    return WorkspaceCreateRequest(**defaults)  # type: ignore[arg-type]


class TestCreateDirect:
    @pytest.mark.unit
    async def test_creates_new_workspace_without_idempotency(self, session: AsyncSession) -> None:
        result = await create_workspace(
            payload=_payload(),
            idempotency_key=None,
            session=session,
        )
        assert isinstance(result, WorkspaceAcceptedResponse)
        assert result.workspace_id.startswith("ws_")
        assert result.version == 1

    @pytest.mark.unit
    async def test_secret_lease_route_missing_workspace_raises_structured_404(
        self,
        session: AsyncSession,
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await get_workspace_secret_leases("ws_missing", session=session)

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == {
            "error_code": "NOT_FOUND",
            "message": "No workspace with id ws_missing",
        }

    @pytest.mark.unit
    async def test_replays_idempotent_match(self, session: AsyncSession) -> None:
        """Same key + same body: the second call returns the SAME
        workspace row (covers the replay path at line 60)."""
        payload = _payload(task_title="idem")
        first = await create_workspace(
            payload=payload,
            idempotency_key="IDEM-OK",
            session=session,
        )
        assert isinstance(first, WorkspaceAcceptedResponse)

        second = await create_workspace(
            payload=payload,
            idempotency_key="IDEM-OK",
            session=session,
        )
        assert isinstance(second, WorkspaceAcceptedResponse)
        assert second.workspace_id == first.workspace_id

    @pytest.mark.unit
    async def test_idempotent_conflict_returns_409(self, session: AsyncSession) -> None:
        """Same key + different body: returns a 409 JSONResponse (covers
        lines 50-58)."""
        await create_workspace(
            payload=_payload(task_title="first"),
            idempotency_key="IDEM-CONFLICT",
            session=session,
        )
        result = await create_workspace(
            payload=_payload(task_title="second"),
            idempotency_key="IDEM-CONFLICT",
            session=session,
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 409
        import json

        body = json.loads(result.body)
        assert body["error_code"] == "IDEMPOTENCY_CONFLICT"


class TestGetDirect:
    @pytest.mark.unit
    async def test_returns_200_for_existing(self, session: AsyncSession) -> None:
        created = await create_workspace(
            payload=_payload(task_title="look-me-up"),
            idempotency_key=None,
            session=session,
        )
        assert isinstance(created, WorkspaceAcceptedResponse)
        result = await _get_workspace_response(created.workspace_id, session)
        assert result.id == created.workspace_id
        assert result.task_title == "look-me-up"

    @pytest.mark.unit
    async def test_raises_404_for_missing(self, session: AsyncSession) -> None:
        with pytest.raises(HTTPException) as exc:
            await _get_workspace_response("ws_missing_id", session)
        assert exc.value.status_code == 404
        assert exc.value.detail["error_code"] == "NOT_FOUND"


class TestListDirect:
    @pytest.mark.unit
    async def test_returns_rows(self, session: AsyncSession) -> None:
        await create_workspace(
            payload=_payload(task_title="a"),
            idempotency_key=None,
            session=session,
        )
        await create_workspace(
            payload=_payload(task_title="b"),
            idempotency_key=None,
            session=session,
        )
        # Flush so the query sees the inserts in the same session.
        await session.flush()
        results = await _list_workspace_responses(session, limit=10)
        assert len(results) == 2


class TestPayloadsMatch:
    @pytest.mark.unit
    async def test_match_on_identical_fields(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="r",
            branch_base="b",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=["x"],
            requires_database=False,
            idempotency_key="k",
        )
        payload = _payload(
            repo_url="r",
            branch_base="b",
            task_title="t",
            task_prompt="p",
            test_commands=["x"],
        )
        assert _payloads_match(ws, payload) is True

    @pytest.mark.unit
    async def test_mismatch_on_repo_url(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="r1",
            branch_base="b",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            requires_database=False,
        )
        assert _payloads_match(ws, _payload(repo_url="r2")) is False

    @pytest.mark.unit
    async def test_mismatch_on_task_external_id(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="r",
            branch_base="b",
            task_title="t",
            task_prompt="p",
            task_external_id="TASK-1",
            agent="codex",
            test_commands=[],
            requires_database=False,
        )
        assert (
            _payloads_match(
                ws,
                _payload(
                    repo_url="r",
                    branch_base="b",
                    task_title="t",
                    task_prompt="p",
                    task_external_id="TASK-2",
                    test_commands=[],
                ),
            )
            is False
        )

    @pytest.mark.unit
    async def test_mismatch_on_env_profile(self, session: AsyncSession) -> None:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="r",
            branch_base="b",
            task_title="t",
            task_prompt="p",
            agent="codex",
            env_profile="profile-a",
            test_commands=[],
            requires_database=False,
        )
        assert (
            _payloads_match(
                ws,
                _payload(
                    repo_url="r",
                    branch_base="b",
                    task_title="t",
                    task_prompt="p",
                    env_profile="profile-b",
                    test_commands=[],
                ),
            )
            is False
        )
