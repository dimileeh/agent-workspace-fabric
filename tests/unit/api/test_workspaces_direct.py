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

import json
from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.api.routes.workspaces as workspaces_route
from awf.api.routes.workspaces import (
    _list_workspace_responses,
    _payloads_match,
    create_workspace,
    get_workspace,
    get_workspace_secret_leases,
)
from awf.api.schemas import (
    WorkspaceAcceptedResponse,
    WorkspaceCreateRequest,
)
from awf.common.config import Settings
from awf.db.enums import AgentRuntime
from awf.db.repositories import EgressAuditRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.workspace_observability import list_workspace_overview_response
from tests.postgres import postgres_test_engine


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        try:
            yield s
        finally:
            await s.commit()


def _payload(
    *,
    provider_readiness_override: bool = False,
    **overrides: object,
) -> WorkspaceCreateRequest:
    defaults: dict[str, object] = {
        "repo_url": "git@github.com:dimileeh/aira-web.git",
        "branch_base": "development",
        "task_title": "direct test",
        "task_prompt": "do a thing",
        "agent": AgentRuntime.codex,
        "test_commands": ["pytest -q"],
        "requires_database": False,
    }
    if provider_readiness_override:
        defaults["preflight"] = {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "direct workspace route fixture",
        }
    defaults.update(overrides)
    return WorkspaceCreateRequest(**defaults)  # type: ignore[arg-type]


def _route_settings() -> Settings:
    # Direct route tests are not disk-pressure tests; keep them independent of
    # host runner free space while exercising workspace route behavior.
    return Settings(_env_file=None, min_free_disk_bytes=0)


def _closed_connection_error() -> InterfaceError:
    return InterfaceError("SELECT 1", {}, RuntimeError("connection is closed"))


@pytest.mark.unit
def test_route_settings_do_not_inherit_host_disk_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_MIN_FREE_DISK_BYTES", "999999999999999")

    assert _route_settings().min_free_disk_bytes == 0


class TestCreateDirect:
    @pytest.mark.unit
    async def test_creates_new_workspace_without_idempotency(self, session: AsyncSession) -> None:
        result = await create_workspace(
            payload=_payload(provider_readiness_override=True),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(result, WorkspaceAcceptedResponse)
        assert result.workspace_id.startswith("ws_")
        assert result.version == 1

    @pytest.mark.unit
    async def test_direct_route_settings_do_not_inherit_host_disk_floor(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session: AsyncSession,
    ) -> None:
        monkeypatch.setenv("AWF_MIN_FREE_DISK_BYTES", str(10**18))

        result = await create_workspace(
            payload=_payload(
                provider_readiness_override=True, task_title="direct disk independent"
            ),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )

        assert isinstance(result, WorkspaceAcceptedResponse)

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
        payload = _payload(provider_readiness_override=True, task_title="idem")
        first = await create_workspace(
            payload=payload,
            idempotency_key="IDEM-OK",
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(first, WorkspaceAcceptedResponse)

        second = await create_workspace(
            payload=payload,
            idempotency_key="IDEM-OK",
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(second, WorkspaceAcceptedResponse)
        assert second.workspace_id == first.workspace_id

    @pytest.mark.unit
    async def test_idempotent_conflict_returns_409(self, session: AsyncSession) -> None:
        """Same key + different body: returns a 409 JSONResponse (covers
        lines 50-58)."""
        first = await create_workspace(
            payload=_payload(provider_readiness_override=True, task_title="first"),
            idempotency_key="IDEM-CONFLICT",
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(first, WorkspaceAcceptedResponse)
        result = await create_workspace(
            payload=_payload(provider_readiness_override=True, task_title="second"),
            idempotency_key="IDEM-CONFLICT",
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 409

        body = json.loads(result.body)
        assert body["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.unit
    async def test_create_persists_companions_and_uses_them_for_idempotency(
        self,
        session: AsyncSession,
    ) -> None:
        companion = {
            "name": "backend",
            "repo_url": "git@github.com:example/backend.git",
            "build_context": ".",
            "env_file": "config/dev.env",
            "depends_on": ["docker"],
        }
        first = await create_workspace(
            payload=_payload(
                provider_readiness_override=True,
                task_title="companion contract",
                companions=[companion],
            ),
            idempotency_key="IDEM-COMPANION",
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(first, WorkspaceAcceptedResponse)

        repo = WorkspaceRepository(session)
        workspace = await repo.get(first.workspace_id)
        assert workspace is not None
        stored_companion = workspace.task_policy["companions"][0]
        assert stored_companion["name"] == "backend"
        assert stored_companion["base_branch"] == "development"
        assert stored_companion["env_file"] == "config/dev.env"

        replay = await create_workspace(
            payload=_payload(
                provider_readiness_override=True,
                task_title="companion contract",
                companions=[companion],
            ),
            idempotency_key="IDEM-COMPANION",
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(replay, WorkspaceAcceptedResponse)
        assert replay.workspace_id == first.workspace_id

        conflict = await create_workspace(
            payload=_payload(
                provider_readiness_override=True,
                task_title="companion contract",
                companions=[{**companion, "build_context": "service"}],
            ),
            idempotency_key="IDEM-COMPANION",
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(conflict, JSONResponse)
        assert conflict.status_code == 409
        assert json.loads(conflict.body)["error_code"] == "IDEMPOTENCY_CONFLICT"


class TestGetDirect:
    @pytest.mark.unit
    async def test_returns_200_for_existing(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        created = await create_workspace(
            payload=_payload(provider_readiness_override=True, task_title="look-me-up"),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(created, WorkspaceAcceptedResponse)
        await session.commit()

        result = await get_workspace(created.workspace_id, session_factory=session_factory)

        assert result.id == created.workspace_id
        assert result.task_title == "look-me-up"

    @pytest.mark.unit
    async def test_route_uses_primary_workspace_response_helper(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        created = await create_workspace(
            payload=_payload(
                provider_readiness_override=True,
                task_title="route helper coverage",
            ),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(created, WorkspaceAcceptedResponse)
        await session.commit()

        original = workspaces_route._get_workspace_response
        calls = 0

        async def _tracked_workspace_response(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            workspaces_route, "_get_workspace_response", _tracked_workspace_response
        )

        result = await get_workspace(created.workspace_id, session_factory=session_factory)

        assert calls == 1
        assert result.id == created.workspace_id
        assert result.task_title == "route helper coverage"

    @pytest.mark.unit
    async def test_returns_materialized_response_after_transient_egress_retry_failure(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        created = await create_workspace(
            payload=_payload(
                provider_readiness_override=True,
                task_title="transient audit cleanup",
            ),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(created, WorkspaceAcceptedResponse)
        await session.commit()

        calls = 0

        async def _fail_audit_lookup(
            self: EgressAuditRepository,
            workspace_id: str,
        ) -> object:
            nonlocal calls
            calls += 1
            assert workspace_id == created.workspace_id
            raise _closed_connection_error()

        monkeypatch.setattr(
            EgressAuditRepository,
            "get_latest_for_workspace",
            _fail_audit_lookup,
        )

        result = await get_workspace(created.workspace_id, session_factory=session_factory)

        assert result.id == created.workspace_id
        assert result.task_title == "transient audit cleanup"
        assert result.egress_audit is None
        assert calls == 2

    @pytest.mark.unit
    async def test_returns_materialized_response_after_egress_error(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        created = await create_workspace(
            payload=_payload(
                provider_readiness_override=True,
                task_title="non-transient audit cleanup",
            ),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(created, WorkspaceAcceptedResponse)
        await session.commit()

        calls = 0

        async def _fail_audit_lookup(
            self: EgressAuditRepository,
            workspace_id: str,
        ) -> object:
            nonlocal calls
            calls += 1
            assert workspace_id == created.workspace_id
            raise RuntimeError("audit lookup failed")

        monkeypatch.setattr(
            EgressAuditRepository,
            "get_latest_for_workspace",
            _fail_audit_lookup,
        )

        result = await get_workspace(created.workspace_id, session_factory=session_factory)

        assert result.id == created.workspace_id
        assert result.task_title == "non-transient audit cleanup"
        assert result.egress_audit is None
        assert calls == 1

    @pytest.mark.unit
    async def test_returns_materialized_response_after_invalid_egress_audit_payload(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        created = await create_workspace(
            payload=_payload(
                provider_readiness_override=True,
                task_title="invalid audit payload",
            ),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(created, WorkspaceAcceptedResponse)
        await session.commit()

        async def _invalid_audit_lookup(
            workspace_id: str,
            session_factory: async_sessionmaker[AsyncSession],
        ) -> dict[str, object] | None:
            del session_factory
            assert workspace_id == created.workspace_id
            return {"id": "audit_missing_required_fields"}

        monkeypatch.setattr(
            workspaces_route,
            "_retry_optional_egress_audit_lookup",
            _invalid_audit_lookup,
        )
        caplog.set_level("WARNING", logger=workspaces_route.__name__)

        result = await get_workspace(created.workspace_id, session_factory=session_factory)

        assert result.id == created.workspace_id
        assert result.task_title == "invalid audit payload"
        assert result.egress_audit is None
        assert "egress audit model validation failed" in caplog.text

    @pytest.mark.unit
    async def test_raises_404_for_missing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await get_workspace("ws_missing_id", session_factory=session_factory)
        assert exc.value.status_code == 404
        assert exc.value.detail["error_code"] == "NOT_FOUND"

    @pytest.mark.unit
    async def test_pr_number_in_workspace_response(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """WorkspaceResponse must include pr_number so the console can show it for BitBucket PRs."""
        created = await create_workspace(
            payload=_payload(
                provider_readiness_override=True,
                task_title="bitbucket pr number",
            ),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(created, WorkspaceAcceptedResponse)
        repo = WorkspaceRepository(session)
        ws = await repo.get(created.workspace_id)
        assert ws is not None
        ws.pr_url = "https://bitbucket.org/org/repo/pull-requests/2"
        ws.pr_number = 2
        await session.commit()

        result = await get_workspace(created.workspace_id, session_factory=session_factory)

        assert result.pr_number == 2


class TestListDirect:
    @pytest.mark.unit
    async def test_returns_rows(self, session: AsyncSession) -> None:
        first = await create_workspace(
            payload=_payload(provider_readiness_override=True, task_title="a"),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        second = await create_workspace(
            payload=_payload(provider_readiness_override=True, task_title="b"),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(first, WorkspaceAcceptedResponse)
        assert isinstance(second, WorkspaceAcceptedResponse)
        # Flush so the query sees the inserts in the same session.
        await session.flush()
        results = await _list_workspace_responses(session, limit=10)
        assert len(results) == 2

    @pytest.mark.unit
    async def test_pr_number_in_overview_response(self, session: AsyncSession) -> None:
        """WorkspaceOverviewResponse must include pr_number so the console list view can show it."""
        created = await create_workspace(
            payload=_payload(
                provider_readiness_override=True,
                task_title="overview pr number",
            ),
            idempotency_key=None,
            settings=_route_settings(),
            session=session,
        )
        assert isinstance(created, WorkspaceAcceptedResponse)
        repo = WorkspaceRepository(session)
        ws = await repo.get(created.workspace_id)
        assert ws is not None
        ws.pr_url = "https://bitbucket.org/org/repo/pull-requests/7"
        ws.pr_number = 7
        await session.flush()

        overview = await list_workspace_overview_response(session, limit=10)

        assert len(overview.items) == 1
        assert overview.items[0].pr_number == 7


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
    async def test_legacy_null_auto_merge_matches_default_true(
        self,
        session: AsyncSession,
    ) -> None:
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

        try:
            ws.auto_merge = None  # type: ignore[assignment]
            assert _payloads_match(ws, payload) is True
        finally:
            ws.auto_merge = True

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
