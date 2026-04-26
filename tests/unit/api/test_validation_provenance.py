"""Validation provenance API contract tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceLogStreamRepository, WorkspaceRepository
from awf.db.session import make_session_factory

_V2_PROFILE_BODY = {
    "repo": {
        "url": "git@github.com:example/provenance.git",
        "base_branch": "main",
    },
    "task": {
        "title": "Expose validation provenance",
        "prompt": "Add validation provenance API.",
        "kind": "feature_branch_pr",
        "agent": "codex",
    },
    "workspace": {
        "profile_ref": "auto",
        "profile": {
            "name": "api-provenance-test",
            "phases": {
                "setup": ["uv sync"],
                "validate": ["pytest -q"],
            },
        },
    },
    "validation": {"commands": ["ruff check"], "requested_tier": 1},
    "resources": {},
}


_V1_BODY = {
    "repo_url": "git@github.com:example/provenance.git",
    "branch_base": "main",
    "task_title": "Expose validation provenance",
    "task_prompt": "Add validation provenance API.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


async def _create_v2_profile_workspace(client: AsyncClient) -> str:
    response = await client.post("/v2/workspaces", json=_V2_PROFILE_BODY)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_v2_workspace_with_body(client: AsyncClient, body: dict) -> str:
    response = await client.post("/v2/workspaces", json=body)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_v1_workspace(client: AsyncClient) -> str:
    response = await client.post("/v1/workspaces", json=_V1_BODY)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_v1_workspace_with_commands(
    client: AsyncClient,
    commands: list[str],
) -> str:
    response = await client.post(
        "/v1/workspaces",
        json={**_V1_BODY, "test_commands": commands},
    )
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_stream_pair(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    base_stream_id: str,
    phase: str,
    stdout_bytes: int,
    stdout_lines: int,
    stderr_bytes: int,
    stderr_lines: int,
    closed: bool = True,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceLogStreamRepository(session)
        stdout = await repo.create_or_get(
            workspace_id=workspace_id,
            stream_id=f"{base_stream_id}.stdout",
            source="validation",
            name=f"{phase} {base_stream_id.removeprefix('validation.')} stdout",
            kind="stdout",
            path=f"/tmp/{base_stream_id}.stdout.log",
        )
        stderr = await repo.create_or_get(
            workspace_id=workspace_id,
            stream_id=f"{base_stream_id}.stderr",
            source="validation",
            name=f"{phase} {base_stream_id.removeprefix('validation.')} stderr",
            kind="stderr",
            path=f"/tmp/{base_stream_id}.stderr.log",
        )
        opened_at = datetime(2026, 4, 26, 12, 0, tzinfo=UTC) + timedelta(
            minutes=len(base_stream_id)
        )
        stdout.opened_at = opened_at
        stderr.opened_at = opened_at
        await repo.append_metadata(
            workspace_id=workspace_id,
            stream_id=f"{base_stream_id}.stdout",
            byte_delta=stdout_bytes,
            line_delta=stdout_lines,
        )
        await repo.append_metadata(
            workspace_id=workspace_id,
            stream_id=f"{base_stream_id}.stderr",
            byte_delta=stderr_bytes,
            line_delta=stderr_lines,
        )
        if closed:
            await repo.close(workspace_id=workspace_id, stream_id=f"{base_stream_id}.stdout")
            await repo.close(workspace_id=workspace_id, stream_id=f"{base_stream_id}.stderr")
        await session.commit()


async def _mark_workspace_completed(engine: AsyncEngine, workspace_id: str) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.completed.value
        workspace.branch_name = "codex/validation-provenance"
        workspace.base_commit = "abc123def456"
        await session.commit()


async def _mark_workspace_validation_failed(
    engine: AsyncEngine,
    workspace_id: str,
    message: str = "validation failed: ruff check",
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.failed.value
        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = message
        await session.commit()


async def _insert_validation_run(
    engine: AsyncEngine,
    *,
    run_id: str,
    workspace_id: str,
    attempt_id: str | None = None,
    tier: int = 1,
    command_set_hash: str = "0" * 64,
    commands: list[dict] | None = None,
    base_commit: str | None = "base-persisted",
    target_branch: str | None = "awf/persisted-validation",
    target_head_sha: str | None = "target-persisted",
    status: str = "succeeded",
    reason_code: str | None = "VALIDATION_OK",
    started_at: datetime = datetime(2026, 4, 26, 13, 0, tzinfo=UTC),
    finished_at: datetime | None = datetime(2026, 4, 26, 13, 2, tzinfo=UTC),
    log_stream_refs: dict | None = None,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO validation_runs (
                    id,
                    workspace_id,
                    attempt_id,
                    tier,
                    command_set_hash,
                    commands,
                    base_commit,
                    target_branch,
                    target_head_sha,
                    status,
                    reason_code,
                    started_at,
                    finished_at,
                    log_stream_refs
                )
                VALUES (
                    :id,
                    :workspace_id,
                    :attempt_id,
                    :tier,
                    :command_set_hash,
                    :commands,
                    :base_commit,
                    :target_branch,
                    :target_head_sha,
                    :status,
                    :reason_code,
                    :started_at,
                    :finished_at,
                    :log_stream_refs
                )
                """
            ),
            {
                "id": run_id,
                "workspace_id": workspace_id,
                "attempt_id": attempt_id,
                "tier": tier,
                "command_set_hash": command_set_hash,
                "commands": json.dumps(commands or []),
                "base_commit": base_commit,
                "target_branch": target_branch,
                "target_head_sha": target_head_sha,
                "status": status,
                "reason_code": reason_code,
                "started_at": started_at,
                "finished_at": finished_at,
                "log_stream_refs": json.dumps(log_stream_refs or {}),
            },
        )
        await session.commit()


@pytest.mark.unit
async def test_validation_provenance_groups_streams_and_resolves_profile_commands(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v2_profile_workspace(client)
    await _mark_workspace_completed(engine, workspace_id)
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.01_setup",
        phase="setup",
        stdout_bytes=120,
        stdout_lines=4,
        stderr_bytes=0,
        stderr_lines=0,
    )
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.01_validate",
        phase="validate",
        stdout_bytes=2048,
        stdout_lines=32,
        stderr_bytes=18,
        stderr_lines=1,
    )
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.02_validate",
        phase="validate",
        stdout_bytes=4096,
        stdout_lines=64,
        stderr_bytes=0,
        stderr_lines=0,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation")

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    assert [(item["phase"], item["command_index"], item["command"]) for item in body["items"]] == [
        ("setup", 1, "uv sync"),
        ("validate", 1, "pytest -q"),
        ("validate", 2, "ruff check"),
    ]
    first = body["items"][0]
    assert first["workspace_id"] == workspace_id
    assert first["stream_ids"] == {
        "stdout": "validation.01_setup.stdout",
        "stderr": "validation.01_setup.stderr",
    }
    assert first["stdout_byte_count"] == 120
    assert first["stdout_line_count"] == 4
    assert first["stderr_byte_count"] == 0
    assert first["stderr_line_count"] == 0
    assert first["opened_at"] == "2026-04-26T12:19:00Z"
    assert first["closed_at"] is not None
    assert first["status"] == "succeeded"
    assert first["base_commit"] == "abc123def456"
    assert first["branch_name"] == "codex/validation-provenance"


@pytest.mark.unit
async def test_validation_provenance_prefers_persisted_validation_runs(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _mark_workspace_completed(engine, workspace_id)
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.cmd_01",
        phase="validate",
        stdout_bytes=999,
        stdout_lines=99,
        stderr_bytes=0,
        stderr_lines=0,
    )
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.run_01",
        phase="validate",
        stdout_bytes=42,
        stdout_lines=3,
        stderr_bytes=7,
        stderr_lines=1,
    )
    await _insert_validation_run(
        engine,
        run_id="vr_111111111111111111111111",
        workspace_id=workspace_id,
        tier=2,
        command_set_hash="a" * 64,
        commands=[
            {
                "phase": "validate",
                "command_index": 1,
                "command": "npm test",
                "stream_ids": {
                    "stdout": "validation.run_01.stdout",
                    "stderr": "validation.run_01.stderr",
                },
            }
        ],
        log_stream_refs={
            "commands": [
                {
                    "stdout": "validation.run_01.stdout",
                    "stderr": "validation.run_01.stderr",
                }
            ]
        },
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["validation_run_id"] == "vr_111111111111111111111111"
    assert item["tier"] == 2
    assert item["command_set_hash"] == "a" * 64
    assert item["phase"] == "validate"
    assert item["command_index"] == 1
    assert item["command"] == "npm test"
    assert item["stream_ids"] == {
        "stdout": "validation.run_01.stdout",
        "stderr": "validation.run_01.stderr",
    }
    assert item["stdout_byte_count"] == 42
    assert item["stdout_line_count"] == 3
    assert item["stderr_byte_count"] == 7
    assert item["stderr_line_count"] == 1
    assert item["status"] == "succeeded"
    assert item["reason_code"] == "VALIDATION_OK"
    assert item["base_commit"] == "base-persisted"
    assert item["branch_name"] == "awf/persisted-validation"
    assert item["target_branch"] == "awf/persisted-validation"
    assert item["target_head_sha"] == "target-persisted"
    assert item["current_target_head_sha"] is None
    assert item["fresh_for_target"] is None
    assert item["started_at"] == "2026-04-26T13:00:00Z"
    assert item["finished_at"] == "2026-04-26T13:02:00Z"
    assert item["log_stream_refs"] == {
        "commands": [
            {
                "stdout": "validation.run_01.stdout",
                "stderr": "validation.run_01.stderr",
            }
        ]
    }


@pytest.mark.unit
async def test_validation_provenance_resolves_profile_commands_by_phase_index(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v2_workspace_with_body(
        client,
        {
            **_V2_PROFILE_BODY,
            "workspace": {
                "profile_ref": "auto",
                "profile": {
                    "name": "api-provenance-phase-index-test",
                    "phases": {
                        "setup": ["uv sync"],
                        "pre_agent": ["python scripts/preflight.py"],
                        "post_agent": ["ruff format --check"],
                        "validate": ["pytest -q"],
                    },
                    "validation": {
                        "healthchecks": [
                            {
                                "name": "api",
                                "command": "curl -fsS http://localhost:8000/health",
                            }
                        ]
                    },
                },
            },
            "validation": {"commands": ["ruff check"], "requested_tier": 1},
        },
    )
    for phase, base_stream_id in (
        ("setup", "validation.01_setup"),
        ("pre_agent", "validation.01_pre_agent"),
        ("healthcheck", "validation.01_healthcheck"),
        ("post_agent", "validation.01_post_agent"),
        ("validate", "validation.01_validate"),
        ("validate", "validation.02_validate"),
    ):
        await _create_stream_pair(
            engine,
            workspace_id=workspace_id,
            base_stream_id=base_stream_id,
            phase=phase,
            stdout_bytes=1,
            stdout_lines=1,
            stderr_bytes=0,
            stderr_lines=0,
        )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation")

    assert response.status_code == 200
    assert [
        (item["phase"], item["command_index"], item["command"]) for item in response.json()["items"]
    ] == [
        ("setup", 1, "uv sync"),
        ("pre_agent", 1, "python scripts/preflight.py"),
        ("healthcheck", 1, "curl -fsS http://localhost:8000/health"),
        ("post_agent", 1, "ruff format --check"),
        ("validate", 1, "pytest -q"),
        ("validate", 2, "ruff check"),
    ]


@pytest.mark.unit
async def test_validation_provenance_marks_open_streams_running_and_uses_request_commands(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.cmd_01",
        phase="validate",
        stdout_bytes=12,
        stdout_lines=1,
        stderr_bytes=7,
        stderr_lines=1,
        closed=False,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["phase"] == "validate"
    assert item["command_index"] == 1
    assert item["command"] == "pytest -q"
    assert item["status"] == "running"
    assert item["closed_at"] is None


@pytest.mark.unit
async def test_validation_provenance_marks_open_streams_failed_for_failed_workspace(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace_with_commands(client, ["ruff check"])
    await _mark_workspace_validation_failed(engine, workspace_id)
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.cmd_01",
        phase="validate",
        stdout_bytes=12,
        stdout_lines=1,
        stderr_bytes=7,
        stderr_lines=1,
        closed=False,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "failed"
    assert item["closed_at"] is None


@pytest.mark.unit
async def test_validation_provenance_marks_failed_command_from_workspace_failure(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace_with_commands(
        client,
        ["pytest -q", "ruff check"],
    )
    await _mark_workspace_validation_failed(engine, workspace_id)
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.cmd_01",
        phase="validate",
        stdout_bytes=20,
        stdout_lines=1,
        stderr_bytes=0,
        stderr_lines=0,
    )
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.cmd_02",
        phase="validate",
        stdout_bytes=0,
        stdout_lines=0,
        stderr_bytes=15,
        stderr_lines=1,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation")

    assert response.status_code == 200
    assert [(item["command"], item["status"]) for item in response.json()["items"]] == [
        ("pytest -q", "succeeded"),
        ("ruff check", "failed"),
    ]


@pytest.mark.unit
async def test_validation_provenance_phase_failure_uses_last_matching_phase_record(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace_with_commands(
        client,
        ["pytest -q", "ruff check"],
    )
    await _mark_workspace_validation_failed(engine, workspace_id, message="validate phase failed")
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.cmd_01",
        phase="validate",
        stdout_bytes=20,
        stdout_lines=1,
        stderr_bytes=0,
        stderr_lines=0,
    )
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.cmd_02",
        phase="validate",
        stdout_bytes=0,
        stdout_lines=0,
        stderr_bytes=15,
        stderr_lines=1,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation")

    assert response.status_code == 200
    assert [(item["command"], item["status"]) for item in response.json()["items"]] == [
        ("pytest -q", "succeeded"),
        ("ruff check", "failed"),
    ]


@pytest.mark.unit
async def test_validation_provenance_empty_when_workspace_has_no_validation_logs(
    client: AsyncClient,
) -> None:
    workspace_id = await _create_v1_workspace(client)

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None, "has_more": False}


@pytest.mark.unit
async def test_validation_provenance_missing_workspace_returns_404(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/workspaces/ws_missing/validation")

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "NOT_FOUND"
