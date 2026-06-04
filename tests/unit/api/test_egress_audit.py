"""API response tests for egress audit evidence.

TDD suite: these tests must fail before egress audit API wiring lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.health as health_route
from awf.common.audit import redact_audit_value
from awf.common.config import Settings
from awf.common.ids import new_egress_audit_record_id
from awf.db.enums import EgressDecision, WorkspaceStatus
from awf.db.models import EgressAuditRecord
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


async def _workspace_with_audit(
    engine: AsyncEngine,
    *,
    posture: str = "open",
    decision: str = "allow",
    details: dict | None = None,
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/egress-api.git",
            branch_base="main",
            task_title="api egress audit",
            task_prompt="test egress audit api",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.ready.value
        ws.resolved_profile = {
            "name": "test-profile",
            "version": 1,
            "security": {"egress": {"mode": posture}},
        }
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/{ws.id}/compose.yml"
        await session.flush()

        record_details = details or {
            "internet_access": "unrestricted" if posture == "open" else "internal"
        }
        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=ws.id,
            policy_posture=posture,
            decision=decision,
            destination_category="public_internet" if posture == "open" else "internal_only",
            reason_code=f"LOCAL_EGRESS_{posture.upper()}_TEST",
            details=record_details,
        )
        session.add(record)
        await session.commit()
        return ws.id


# --- Workspace detail includes egress audit ---


@pytest.mark.unit
async def test_workspace_detail_includes_egress_audit_field(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    """``WorkspaceResponse`` via HTTP carries ``egress_audit`` for a workspace that has one."""
    wid = await _workspace_with_audit(engine, posture="open")

    resp = await client.get(f"/v1/workspaces/{wid}")
    assert resp.status_code == 200
    body = resp.json()

    assert "egress_audit" in body
    audit = body["egress_audit"]
    assert audit is not None
    assert audit["policy_posture"] == "open"
    assert audit["decision"] == "allow"
    assert audit["destination_category"] == "public_internet"


@pytest.mark.unit
async def test_workspace_detail_egress_audit_contains_enforcement_data(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    """A workspace with a restricted audit record exposes complete enforcement evidence."""
    factory = make_session_factory(engine)
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/egress-api.git",
            branch_base="main",
            task_title="egress enforcement",
            task_prompt="verify egress audit fields",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.ready.value
        ws.resolved_profile = {
            "name": "test-profile",
            "version": 1,
            "security": {"egress": {"mode": "restricted"}},
        }
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/{ws.id}/compose.yml"
        await session.flush()

        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=ws.id,
            policy_posture="restricted",
            decision=EgressDecision.deferred.value,
            destination_category="internal_only",
            reason_code="LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY",
            details={"destination_filtering": "deferred"},
        )
        session.add(record)
        await session.commit()
        wid = ws.id

    resp = await client.get(f"/v1/workspaces/{wid}")
    assert resp.status_code == 200
    body = resp.json()

    audit = body.get("egress_audit")
    assert audit is not None
    assert audit["policy_posture"] == "restricted"
    assert audit["decision"] == "deferred"
    assert audit["destination_category"] == "internal_only"
    assert audit["reason_code"] == "LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY"
    assert audit["enforced_at"] is not None


@pytest.mark.unit
async def test_workspace_detail_egress_audit_null_when_no_audit(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    """``egress_audit`` must be ``None`` for a workspace with no audit record."""
    factory = make_session_factory(engine)
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/egress-api.git",
            branch_base="main",
            task_title="no audit record",
            task_prompt="no egress audit evidence",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.ready.value
        ws.resolved_profile = {
            "name": "test-profile",
            "version": 1,
            "security": {"egress": {"mode": "restricted"}},
        }
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/{ws.id}/compose.yml"
        await session.flush()
        await session.commit()
        wid = ws.id

    resp = await client.get(f"/v1/workspaces/{wid}")
    assert resp.status_code == 200
    body = resp.json()

    assert body.get("egress_audit") is None


@pytest.mark.unit
async def test_workspace_detail_egress_audit_redacts_secrets(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    """API responses must never leak tokens in egress audit evidence."""
    factory = make_session_factory(engine)
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/egress-api.git",
            branch_base="main",
            task_title="no secret leaks",
            task_prompt="prove egress audit redaction",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.ready.value
        ws.resolved_profile = {
            "name": "test-profile",
            "version": 1,
            "security": {"egress": {"mode": "restricted"}},
        }
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/{ws.id}/compose.yml"
        await session.flush()

        details = redact_audit_value(
            {
                "safe_field": "normal",
            }
        )

        record = EgressAuditRecord(
            id=new_egress_audit_record_id(),
            workspace_id=ws.id,
            policy_posture="restricted",
            decision="deferred",
            destination_category="internal_only",
            reason_code="LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY",
            details=details,
        )
        session.add(record)
        await session.commit()
        wid = ws.id

    resp = await client.get(f"/v1/workspaces/{wid}")
    assert resp.status_code == 200
    body = resp.json()

    serialized = json.dumps(body)
    assert "ghp_" not in serialized
    assert "sk-ant-" not in serialized


# --- readyz includes egress audit check ---


@pytest.mark.unit
async def test_readyz_includes_egress_audit_check(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``/readyz`` must include an ``egress_audit`` check key."""
    from awf.common.commands import FakeCommandRunner
    from tests.unit.api.test_health_parts.test_health_part_001 import _queue_all_ok

    original_get_settings = health_route.get_settings
    original_get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        host_home=str(Path.home()),
        work_dir=str(tmp_path / "readyz-work"),
    )
    monkeypatch.setattr(health_route, "get_settings", lambda: test_settings)
    monkeypatch.setattr(
        health_route,
        "collect_agent_readiness",
        lambda *_args, **_kwargs: {"status": "ok", "providers": {}},
    )

    async def _worker_ok(*_args: object, **_kwargs: object) -> health_route.CheckResult:
        return health_route.CheckResult(
            ok=True,
            status="ok",
            reason="WORKER_HEARTBEAT_FRESH",
        )

    monkeypatch.setattr(health_route, "_check_worker_heartbeat", _worker_ok)
    app = client._transport.app  # noqa: SLF001
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    try:
        resp = await client.get("/readyz")
        assert resp.status_code == 200, resp.json()
        body = resp.json()

        checks = body.get("checks", {})
        assert "egress_audit" in checks
    finally:
        original_get_settings.cache_clear()
